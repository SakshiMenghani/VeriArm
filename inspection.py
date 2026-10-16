"""Sakshi's part: inspect_box(). Photograph all 5 visible faces of the box and read the QR label.

Uses Sandy's detect.py (box position, size, yaw from the overview-blue photo) and find_pick.py (pick_box).

What it does:
  1. top face, camera straight down
  2. the 4 sides, going AROUND the box (neighbor to neighbor), camera looking down at each face.
     Each side tries a shallow view first (best for reading), then steeper/closer ones; the motion
     planner refuses views that would hit the wall/table. The camera keeps its roll close to the
     current one (no wrist flips) so the arm doesn't swing around.
  3. every reached view gets its own saved image + decode attempt + the real camera pose.
     A face moves on to its next view only if a QR-like pattern was seen but couldn't be read.
  4. stops as soon as a valid VeriArm label (VA1|id|recipient) is read; any other QR code doesn't stop
     it. A contact sheet of the faces visited is written for a human to check.
  Optional (--turn): if no label and a side couldn't be seen, pick the box, turn it 180 deg, re-check.

  python inspection.py --plan              detect the box (arm must be at overview-blue), print views, no motion
  python inspection.py --yes               MOVES THE ARM: photograph + read all 5 faces
  python inspection.py --yes --turn        ... and turn the box if a side couldn't be seen
  python inspection.py --yes --faces +x    only these faces (top, -x, +x, -y, +y)
  python inspection.py --decode FILE...    try QR decoding on saved images, no robot
"""
import argparse
import asyncio
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from viam.components.arm import Arm
from viam.components.camera import Camera
from viam.components.gripper import Gripper
from viam.proto.common import (Geometry, GeometriesInFrame, Pose, PoseInFrame, RectangularPrism, Vector3,
                               WorldState)
from viam.proto.component.arm import JointPositions
from viam.robot.client import RobotClient
from viam.services.motion import MotionClient

import find_pick
from detect import TABLE_TOP_Z, VIEW, BoxDetection, detect_box, point_cloud_to_depth
from inventory import parse_label_text
from poses import get_pose
from robot import ARM, CAMERA, GRIPPER, MOTION, connect

RUNS = Path(__file__).parent / "runs"
LAST_INSPECTION = RUNS / "last_inspection.json"

TOP_STANDOFF = 250  # mm camera -> top face
# (elevation deg above horizontal, standoff mm) per side, tried in order. Shallow = best for reading;
# steep + close = the only way to see a side that faces the wall.
SIDE_VIEWS = ((25, 260), (40, 250), (55, 220), (65, 190), (65, 240), (75, 230), (75, 170))
ROLL_STEPS = (0, 30, -30, 60, -60, 90, -90)  # camera roll changes to try, relative to the current roll
OBSTACLE_PAD = 15       # mm added around the box for planning
SETTLE_S = 0.6
FRAMES_PER_VIEW = 3
PUT_DOWN_MM = 3         # release the turned box this far above the table


# ---------- QR decoding ----------

_detector = cv2.QRCodeDetectorAruco()
_fallback = cv2.QRCodeDetector()


def decode_qr(image: np.ndarray) -> str | None:
    """Return the first QR payload found in a BGR image, or None."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    candidates = [gray, cv2.equalizeHist(gray)]
    if max(gray.shape) < 1500:  # small/far codes decode better upscaled
        candidates.append(cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC))
    for img in candidates:
        for det in (_detector, _fallback):
            try:
                ok, texts, _, _ = det.detectAndDecodeMulti(img)
            except cv2.error:
                continue
            if ok:
                for t in texts:
                    if t:
                        return t
    return None


def qr_shapes(image: np.ndarray) -> int:
    """How many QR-like patterns are visible, even if they can't be decoded (label seen but unreadable)."""
    try:
        ok, pts = _detector.detectMulti(image)
    except cv2.error:
        return 0
    return len(pts) if ok else 0


async def grab(cam: Camera) -> tuple[np.ndarray | None, float | None]:
    """(color image, depth in mm at the image center or None)."""
    images, _ = await cam.get_images()
    bgr = None
    for img in images:
        if img.mime_type in ("image/jpeg", "image/png"):
            bgr = cv2.imdecode(np.frombuffer(img.data, np.uint8), cv2.IMREAD_COLOR)
    if bgr is None:
        return None, None
    cloud, _ = await cam.get_point_cloud()
    depth = point_cloud_to_depth(cloud, bgr.shape[:2])
    center = None
    if depth is not None:
        h, w = depth.shape
        patch = depth[h // 2 - 30: h // 2 + 30, w // 2 - 30: w // 2 + 30]
        vals = patch[patch > 0]
        center = float(np.median(vals)) if vals.size > 50 else None
    return bgr, center


# ---------- views ----------

@dataclass
class View:
    face: str
    target: np.ndarray       # world point the camera looks at
    look: np.ndarray         # unit look direction (camera +z) in world
    standoff: float
    elevation: float | None  # deg, None for top

    def camera_pose(self, theta: float) -> Pose:
        p = self.target - self.look * self.standoff
        return Pose(x=p[0], y=p[1], z=p[2], o_x=self.look[0], o_y=self.look[1], o_z=self.look[2], theta=theta)


def box_geometry(det: BoxDetection):
    """(center of the box volume, long axis, short axis, length, width, height) in world mm."""
    a = math.radians(det.yaw_deg)
    u = np.array([math.cos(a), math.sin(a), 0.0])
    v = np.array([-math.sin(a), math.cos(a), 0.0])
    c = np.array([det.x, det.y, TABLE_TOP_Z + det.height_mm / 2])
    return c, u, v, det.length_mm, det.width_mm, det.height_mm


def face_name(n: np.ndarray) -> str:
    axis = "x" if abs(n[0]) >= abs(n[1]) else "y"
    sign = "+" if (n[0] if axis == "x" else n[1]) > 0 else "-"
    return f"{sign}{axis} side"


def side_faces(det: BoxDetection) -> list[tuple[str, np.ndarray, np.ndarray]]:
    """(name, face center, outward normal) for the 4 sides, in order AROUND the box,
    starting with the side facing the arm base (so consecutive views are neighbors)."""
    c, u, v, length, width, _ = box_geometry(det)
    faces = [(c + v * width / 2, v), (c + u * length / 2, u), (c - v * width / 2, -v), (c - u * length / 2, -u)]
    to_base = -c[:2] / np.linalg.norm(c[:2])
    start = max(range(4), key=lambda i: float(faces[i][1][:2] @ to_base))
    ordered = faces[start:] + faces[:start]
    # go around in the direction whose 2nd face is closer to the base (reach the far side last)
    if float(ordered[1][1][:2] @ to_base) < float(ordered[3][1][:2] @ to_base):
        ordered = [ordered[0]] + ordered[1:][::-1]
    return [(face_name(n), fc, n) for fc, n in ordered]


def plan_views(det: BoxDetection) -> list[list[View]]:
    """Candidate views per face (top first, then around the sides). First candidate = preferred."""
    c, _, _, _, _, h = box_geometry(det)
    views = [[View("top", np.array([c[0], c[1], TABLE_TOP_Z + h]), np.array([0.0, 0, -1]), TOP_STANDOFF, None)]]
    for name, fc, n in side_faces(det):
        cands = []
        for el, standoff in SIDE_VIEWS:
            e = math.radians(el)
            look = -(n * math.cos(e)) + np.array([0, 0, -math.sin(e)])
            cands.append(View(name, fc, look, standoff, el))
        views.append(cands)
    return views


def box_obstacle(det: BoxDetection) -> WorldState:
    c, _, _, length, width, h = box_geometry(det)
    geom = Geometry(
        center=Pose(x=c[0], y=c[1], z=c[2], o_x=0, o_y=0, o_z=1, theta=det.yaw_deg),
        box=RectangularPrism(dims_mm=Vector3(x=length + 2 * OBSTACLE_PAD, y=width + 2 * OBSTACLE_PAD,
                                             z=h + 2 * OBSTACLE_PAD)),
        label="package")
    return WorldState(obstacles=[GeometriesInFrame(reference_frame="world", geometries=[geom])])


# ---------- running ----------

async def go_to_overview(machine: RobotClient) -> None:
    """Joint move to overview-blue (same as find_pick.find_box: detection needs the exact saved pose)."""
    await Arm.from_robot(machine, ARM).move_to_joint_positions(JointPositions(values=get_pose(VIEW)["joints"]))
    await asyncio.sleep(SETTLE_S)


def wrap(deg: float) -> float:
    return (deg + 180) % 360 - 180


async def move_to_view(motion: MotionClient, view: View, world: WorldState) -> float | None:
    """Move the camera to the view, keeping its roll close to the current one. Returns theta or None."""
    current = (await motion.get_pose(CAMERA, "world")).pose.theta
    for step in ROLL_STEPS:
        theta = wrap(current + step)
        try:
            await motion.move(component_name=CAMERA,
                              destination=PoseInFrame(reference_frame="world", pose=view.camera_pose(theta)),
                              world_state=world)
            return theta
        except Exception:
            continue
    return None


async def capture_face(machine: RobotClient, cands: list[View], world: WorldState, out_dir: Path,
                       tag: str) -> dict:
    """Visit the face's views in order. Stops at a decoded label, or at the first reached view that shows
    no QR-like pattern at all. If a pattern is seen but unreadable, tries the next view.
    Returns the face record (best view first in 'views')."""
    motion = MotionClient.from_robot(machine, MOTION)
    cam = Camera.from_robot(machine, CAMERA)
    face = cands[0].face
    rec = {"face": face, "reached": False, "text": None, "qr_shapes": 0, "image": None, "views": []}
    for view in cands:
        label = face if view.elevation is None else f"{face} @{view.elevation:.0f}deg {view.standoff:.0f}mm"
        print(f"[inspect] {label}")
        theta = await move_to_view(motion, view, world)
        if theta is None:
            print("[inspect]   unreachable")
            rec["views"].append({"elevation": view.elevation, "standoff": view.standoff, "reached": False})
            continue
        await asyncio.sleep(SETTLE_S)
        cam_pose = (await motion.get_pose(CAMERA, "world")).pose
        text, frame, center, shapes = None, None, None, 0
        for _ in range(FRAMES_PER_VIEW):
            frame, center = await grab(cam)
            if frame is None:
                continue
            if text := decode_qr(frame):
                break
            shapes = max(shapes, qr_shapes(frame))
        img = f"{tag}_{face.replace(' ', '_')}_{view.elevation or 90:.0f}deg_{view.standoff:.0f}mm.jpg"
        if frame is not None:
            cv2.imwrite(str(out_dir / img), frame)
        v = {"elevation": view.elevation, "standoff": view.standoff, "reached": True, "theta": round(theta, 1),
             "camera": [round(cam_pose.x), round(cam_pose.y), round(cam_pose.z)],
             "center_depth_mm": None if center is None else round(center),
             "text": text, "qr_shapes": shapes, "image": img}
        rec["views"].append(v)
        # the face's headline image: a decoded one, else one with a QR-like pattern, else the first reached
        if text or (shapes and not rec["text"] and not rec["qr_shapes"]) or not rec["reached"]:
            rec.update({"reached": True, "text": text, "qr_shapes": shapes, "image": img,
                        "elevation": view.elevation, "camera": v["camera"]})
        if text:
            print(f"[inspect]   LABEL: {text!r}  ({img})")
            return rec
        if shapes:
            print(f"[inspect]   QR-like pattern seen but not readable ({img}); trying the next view")
            continue
        print(f"[inspect]   no QR ({img})")
        return rec
    return rec


def contact_sheet(out_dir: Path, records: list[dict]) -> Path:
    tiles = []
    for r in records:
        if not r.get("image"):
            continue
        img = cv2.resize(cv2.imread(str(out_dir / r["image"])), (640, 360))
        status = r["text"] or ("QR seen, unreadable" if r.get("qr_shapes") else "no QR")
        cv2.rectangle(img, (0, 0), (640, 34), (0, 0, 0), -1)
        cv2.putText(img, f"{r['face']}: {status}", (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        tiles.append(img)
    if not tiles:
        tiles = [np.zeros((360, 640, 3), np.uint8)]
    while len(tiles) % 2:
        tiles.append(np.zeros_like(tiles[0]))
    sheet = np.vstack([np.hstack(tiles[i:i + 2]) for i in range(0, len(tiles), 2)])
    path = out_dir / "contact.jpg"
    cv2.imwrite(str(path), sheet)
    return path


async def turn_box_around(machine: RobotClient, box_center: Pose) -> None:
    """Pick the box (Sandy's pick_box), turn it 180 deg about its center, put it back, release, back off."""
    await find_pick.pick_box(machine, box_center)
    turned = Pose(x=box_center.x, y=box_center.y, z=box_center.z, o_x=0, o_y=0, o_z=1,
                  theta=wrap(box_center.theta + 180))
    await find_pick.move_tip(machine, find_pick.tip_target(turned, find_pick.LIFT_MM), "turn the box 180 deg")
    await find_pick.move_tip(machine, find_pick.tip_target(turned, PUT_DOWN_MM), "put the box down", straight_line=True)
    find_pick.confirm("turn suction OFF (box is on the table)")
    await Gripper.from_robot(machine, GRIPPER).open()
    await find_pick.move_tip(machine, find_pick.tip_target(turned, find_pick.APPROACH_MM), "back off",
                             straight_line=True)


def best_label(records: list[dict]) -> list[dict]:
    """Faces with a decoded QR, VeriArm labels first."""
    found = [r for r in records if r.get("text")]
    return sorted(found, key=lambda r: parse_label_text(r["text"]) is None)


async def inspect_faces(machine: RobotClient, det: BoxDetection, out_dir: Path, tag: str,
                        only: set[str] | None = None) -> list[dict]:
    """Visit faces in order; stop as soon as one has a valid VeriArm label (other QR codes don't stop it)."""
    world = box_obstacle(det)
    records = []
    for i, cands in enumerate(plan_views(det), start=1):
        if only is None or cands[0].face in only:
            rec = await capture_face(machine, cands, world, out_dir, f"{tag}{i}")
            records.append(rec)
            if rec.get("text") and parse_label_text(rec["text"]):
                break
    return records


async def inspect_box(machine: RobotClient, box_center: Pose, allow_turn: bool = False,
                      only: set[str] | None = None) -> str | None:
    """Photograph + read the visible faces until a valid label is read. Returns its text (VeriArm labels first), or None.
    Saves images, a contact sheet and a report under runs/inspect_<time>/ and runs/last_inspection.json."""
    out_dir = RUNS / f"inspect_{time.strftime('%Y%m%d-%H%M%S')}"
    out_dir.mkdir(parents=True)
    await go_to_overview(machine)
    det = await detect_box(machine)
    report = {"box_center": [box_center.x, box_center.y, box_center.z],
              "box": {"x": det.x, "y": det.y, "size_mm": [det.length_mm, det.width_mm, det.height_mm],
                      "yaw_deg": det.yaw_deg},
              "faces": [], "turned": False, "dir": out_dir.name}

    report["faces"] = await inspect_faces(machine, det, out_dir, "a", only)
    await go_to_overview(machine)

    # faces we couldn't reach, or where a label was seen but not read: turning the box may help
    missed = [r["face"] for r in report["faces"] if not r["reached"] or (r["qr_shapes"] and not r["text"])]
    found = best_label(report["faces"])
    if not found and missed and allow_turn:
        print(f"[inspect] no label; not seen: {', '.join(missed)} -> turning the box 180 deg")
        await turn_box_around(machine, find_pick.box_center_from(det))  # z = box top
        report["turned"] = True
        await go_to_overview(machine)
        det = await detect_box(machine)
        flip = {"+x side": "-x side", "-x side": "+x side", "+y side": "-y side", "-y side": "+y side"}
        report["faces_after_turn"] = await inspect_faces(machine, det, out_dir, "b", {flip[f] for f in missed if f in flip})
        await go_to_overview(machine)
        found = best_label(report["faces_after_turn"])

    sheet = contact_sheet(out_dir, report["faces"] + report.get("faces_after_turn", []))
    report["result"] = found[0]["text"] if found else None
    report["found_on"] = found[0]["face"] if found else None
    report["not_seen"] = missed
    for path in (LAST_INSPECTION, out_dir / "report.json"):
        path.write_text(json.dumps(report, indent=2) + "\n")

    print("\n[inspect] summary:")
    for r in report["faces"] + report.get("faces_after_turn", []):
        if not r["reached"]:
            print(f"  {r['face']:8}  not reachable")
        else:
            what = ("LABEL " + repr(r["text"]) if r["text"]
                    else "label seen, NOT readable" if r["qr_shapes"] else "no label")
            print(f"  {r['face']:8}  {what:40} el={r['elevation'] or 90} cam={r['camera']}")
    print(f"  contact sheet: {sheet}")
    return report["result"]


async def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--plan", action="store_true", help="detect + print planned views, no motion")
    p.add_argument("--yes", action="store_true", help="don't ask before moves (Sandy's confirm prompts)")
    p.add_argument("--turn", action="store_true", help="pick up + turn the box if a side couldn't be seen")
    p.add_argument("--faces", nargs="+", metavar="FACE", help="only these: top -x +x -y +y")
    p.add_argument("--decode", nargs="+", metavar="FILE")
    args = p.parse_args()
    only = None if not args.faces else {f if f == "top" else f"{f} side" for f in args.faces}
    find_pick.ASK_BEFORE_MOVING = not args.yes

    if args.decode:
        for f in args.decode:
            img = cv2.imread(f)
            print(f, "->", decode_qr(img) if img is not None else "unreadable image")
        return

    async with await connect() as machine:
        if args.plan:
            det = await detect_box(machine)
            print(f"box x={det.x:.0f} y={det.y:.0f} {det.length_mm:.0f}x{det.width_mm:.0f}x{det.height_mm:.0f} "
                  f"yaw {det.yaw_deg:.0f}")
            for cands in plan_views(det):
                for v in cands:
                    q = v.camera_pose(0)
                    print(f"  {v.face:8} el={v.elevation or 90:>3} standoff={v.standoff:.0f}  "
                          f"cam=({q.x:.0f}, {q.y:.0f}, {q.z:.0f})")
            return
        box_center = await find_pick.find_box(machine)
        result = await inspect_box(machine, box_center, allow_turn=args.turn, only=only)
    print("RESULT:", result)


if __name__ == "__main__":
    asyncio.run(main())
