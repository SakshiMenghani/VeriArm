"""Find the green (ACCEPTED) and orange (REJECTED) drop zones with the camera; save them in poses.json.

Same idea as the blue zone (detect.py --save-zone): look straight down from high up, find the colored
sheet, convert its corners to table positions, save center + corners as "green_zone" / "orange_zone".

  python zones.py --survey     MOVES THE ARM (no pick): look on both sides of the blue zone, save both zones
  python zones.py --check-drops  MOVES THE ARM (no box): find a reachable drop point in each zone, save it
  python zones.py --show       print the saved zones and the drop targets, no robot

In code:
  from zones import drop_center
  drop_center("green", box_height_mm=61)  ->  Pose(x, y, z = top of the box once it sits in the zone)
  (same convention as box_center, so find_pick.tip_target(drop, h) works for lowering it there)
"""
import argparse
import asyncio
import json
from datetime import datetime

import cv2
import numpy as np
from viam.proto.common import Pose, PoseInFrame
from viam.robot.client import RobotClient
from viam.services.motion import MotionClient

from detect import TABLE_TOP_Z, pixel_to_table, rect_corners_world, side_lengths
from poses import POSES_FILE, capture_current_pose, get_pose, load_poses, save_pose
from robot import CAMERA, MOTION, connect

# OpenCV hue 0-180. Measured on the real sheets: the orange zone is H~9 S~253; a loose pale-orange paper
# on the table is H~15 S~173 and cardboard is S~100, so S >= 210 keeps only the zone.
COLORS = {
    "green": ((40, 120, 60), (85, 255, 255)),
    "orange": ((0, 210, 80), (13, 255, 255)),
}
MIN_AREA_PX = 15_000       # a zone sheet, not a scrap of paper
EDGE_PX = 6                # a blob this close to the border is cut off: re-center and look again
SURVEY_SPOTS = ((350, 470), (350, -240), (150, 430), (150, -200))  # camera x, y (world mm) around the blue zone
# Zones can be beyond the arm's reach straight above them: then look at them at an angle from these spots.
AIM_FROM = ((300, 420, 480), (250, 380, 460), (350, 380, 500), (200, 300, 450), (300, 300, 520))
ROLL_STEPS = (0, 30, -30, 60, -60, 90, -90)
SURVEY_HEIGHTS = (520, 460, 400)  # camera z to try, highest (widest view) first
SETTLE_S = 0.8


def find_sheet(bgr: np.ndarray, color: str):
    """Largest sheet of that color: (minAreaRect, touches_border) or None."""
    lo, hi = COLORS[color]
    mask = cv2.inRange(cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV), lo, hi)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    c = max(contours, key=cv2.contourArea)
    if cv2.contourArea(c) < MIN_AREA_PX:
        return None
    x, y, w, h = cv2.boundingRect(c)
    H, W = mask.shape
    clipped = x < EDGE_PX or y < EDGE_PX or x + w > W - EDGE_PX or y + h > H - EDGE_PX
    return cv2.minAreaRect(c), clipped


async def look_down_at(machine: RobotClient, x: float, y: float) -> tuple[np.ndarray, dict] | None:
    """Camera straight down above (x, y), as high as reachable. Returns (photo, view) or None."""
    motion = MotionClient.from_robot(machine, MOTION)
    theta = (await motion.get_pose(CAMERA, "world")).pose.theta  # keep the roll: no wrist spin
    for z in SURVEY_HEIGHTS:
        try:
            await motion.move(component_name=CAMERA, destination=PoseInFrame(
                reference_frame="world", pose=Pose(x=x, y=y, z=z, o_x=0, o_y=0, o_z=-1, theta=theta)))
        except Exception:
            continue
        await asyncio.sleep(SETTLE_S)
        view = await capture_current_pose(machine)
        bgr = await _photo(machine)
        return bgr, view
    return None


async def look_at(machine: RobotClient, cam: tuple, target: tuple) -> tuple[np.ndarray, dict] | None:
    """Camera at `cam` (x, y, z), aimed at table point `target` (x, y). Returns (photo, view) or None."""
    motion = MotionClient.from_robot(machine, MOTION)
    current = (await motion.get_pose(CAMERA, "world")).pose.theta
    cx, cy, cz = cam
    d = np.array([target[0] - cx, target[1] - cy, TABLE_TOP_Z - cz], float)
    d /= np.linalg.norm(d)
    for step in ROLL_STEPS:
        theta = (current + step + 180) % 360 - 180
        try:
            await motion.move(component_name=CAMERA, destination=PoseInFrame(
                reference_frame="world", pose=Pose(x=cx, y=cy, z=cz, o_x=d[0], o_y=d[1], o_z=d[2], theta=theta)))
        except Exception:
            continue
        await asyncio.sleep(SETTLE_S)
        return await _photo(machine), await capture_current_pose(machine)
    return None


def sheet_area(rect) -> float:
    return rect[1][0] * rect[1][1]


async def best_view_of(machine: RobotClient, color: str, target: tuple):
    """Look at a sheet from above and from every AIM_FROM angle. Angled views each have a few cm of error,
    so the result combines all views where the whole sheet is visible: median center and size.
    Returns (record, photo with the detection drawn) or None."""
    whole, clipped_views = [], []
    tries = [("above", None)] + [("angle", c) for c in AIM_FROM]
    for kind, cam in tries:
        shot = await look_down_at(machine, *target) if kind == "above" else await look_at(machine, cam, target)
        if shot is None:
            continue
        bgr, view = shot
        hit = find_sheet(bgr, color)
        if hit is None:
            continue
        rect, clipped = hit
        rec = zone_record(rect, view, tuple(view["camera_world"][k] for k in ("x", "y", "z")))
        print(f"[zones]     {kind}{'' if cam is None else ' ' + str(cam)}: {'cut off' if clipped else 'whole'}, "
              f"center x={rec['center_world']['x']:.0f} y={rec['center_world']['y']:.0f}, "
              f"{rec['size_mm'][0]} x {rec['size_mm'][1]} mm")
        shown = bgr.copy()
        cv2.drawContours(shown, [cv2.boxPoints(rect).astype(int)], -1, (0, 0, 255), 4)
        (clipped_views if clipped else whole).append((rec, shown))
    views = whole or clipped_views
    if not views:
        return None
    cx = float(np.median([r["center_world"]["x"] for r, _ in views]))
    cy = float(np.median([r["center_world"]["y"] for r, _ in views]))
    # corners/photo from the view whose center is closest to the median
    rep, shown = min(views, key=lambda v: (v[0]["center_world"]["x"] - cx) ** 2 + (v[0]["center_world"]["y"] - cy) ** 2)
    rec = dict(rep)
    rec["center_world"] = {"x": round(cx, 1), "y": round(cy, 1), "z": TABLE_TOP_Z}
    rec["size_mm"] = [round(float(np.median([r["size_mm"][i] for r, _ in views]))) for i in (0, 1)]
    rec["center_spread_mm"] = round(float(max(np.hypot(r["center_world"]["x"] - cx, r["center_world"]["y"] - cy)
                                              for r, _ in views)), 1)
    rec["views_used"] = len(views)
    rec["partial"] = not whole
    return rec, shown


async def _photo(machine: RobotClient) -> np.ndarray:
    from viam.components.camera import Camera
    images, _ = await Camera.from_robot(machine, CAMERA).get_images()
    color = next(i for i in images if i.mime_type in ("image/jpeg", "image/png"))
    return cv2.imdecode(np.frombuffer(color.data, np.uint8), cv2.IMREAD_COLOR)


def zone_record(rect, view: dict, where: tuple) -> dict:
    (u, v), _, _ = rect
    x, y = pixel_to_table(u, v, view)
    corners = rect_corners_world(rect, view)
    sides = side_lengths(corners)
    return {
        "center_world": {"x": round(x, 1), "y": round(y, 1), "z": TABLE_TOP_Z},
        "corners_world": [[round(cx, 1), round(cy, 1)] for cx, cy in corners],
        "size_mm": [round(max(sides)), round(min(sides))],
        "seen_from_camera": [round(where[0]), round(where[1]), round(where[2])],
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
    }


async def survey(machine: RobotClient) -> dict:
    found = {}
    for sx, sy in SURVEY_SPOTS:
        if len(found) == len(COLORS):
            break
        print(f"[zones] looking down at x={sx} y={sy}")
        shot = await look_down_at(machine, sx, sy)
        if shot is None:
            print("[zones]   unreachable")
            continue
        bgr, view = shot
        cv2.imwrite(f"captures/zones_{sx}_{sy}.jpg", bgr)
        hits = {c: find_sheet(bgr, c) for c in COLORS if c not in found}
        for color, hit in hits.items():  # rough table position of each sheet, from THIS photo
            hits[color] = None if hit is None else pixel_to_table(*hit[0][0], view)
        for color, target in hits.items():
            if color in found or target is None:
                continue
            print(f"[zones]   {color} sheet near x={target[0]:.0f} y={target[1]:.0f}; looking for the clearest view")
            best = await best_view_of(machine, color, target)
            if best is None:
                print(f"[zones]   can't see the {color} sheet from anywhere reachable")
                continue
            found[color], shown = best
            cv2.imwrite(f"captures/zones_{color}.jpg", shown)
        for color in COLORS:
            if color in found and "printed" not in found[color]:
                z = found[color]
                print(f"[zones]   FOUND {color}: center x={z['center_world']['x']} y={z['center_world']['y']}, "
                      f"{z['size_mm'][0]} x {z['size_mm'][1]} mm from {z['views_used']} views "
                      f"(centers within {z['center_spread_mm']} mm){' PARTIAL' if z.get('partial') else ''}")
                z["printed"] = True
    for z in found.values():
        z.pop("printed", None)
    return found


def drop_center(color: str, box_height_mm: float) -> Pose:
    """Where the box goes: the zone's checked drop point (reachable; see --check-drops), else its center.
    z = top of the box once it rests there (same convention as box_center), theta = 0."""
    z = get_pose(f"{color}_zone")
    c = z.get("drop_world") or z["center_world"]
    return Pose(x=c["x"], y=c["y"], z=TABLE_TOP_Z + box_height_mm, o_x=0, o_y=0, o_z=1, theta=0)


DROP_SHIFTS_MM = (0, 40, 80, 120, 160, 200)  # how far toward the arm base to move the drop point if needed


async def check_drops(machine: RobotClient) -> None:
    """For each zone: first point (center, then toward the arm base) where the suction tip reaches the drop
    approach for every known box height. Saves it as <color>_zone.drop_world. Moves the arm, no box held."""
    import find_pick
    from detect import KNOWN_BOXES
    poses = load_poses()
    for color in COLORS:
        zone = poses.get(f"{color}_zone")
        if zone is None:
            print(f"[drops] {color}: zone not saved; run --survey first")
            continue
        c = np.array([zone["center_world"]["x"], zone["center_world"]["y"]])
        toward_base = -c / np.linalg.norm(c)
        chosen = None
        for shift in DROP_SHIFTS_MM:
            x, y = c + toward_base * shift
            ok = True
            for name, (_, _, h) in KNOWN_BOXES.items():
                target = find_pick.tip_target(
                    Pose(x=x, y=y, z=TABLE_TOP_Z + h, o_x=0, o_y=0, o_z=1, theta=0), find_pick.APPROACH_MM)
                try:
                    await find_pick.move_tip(machine, target, f"{color} drop check")
                except Exception:
                    ok = False
                    break
            print(f"[drops] {color}: {shift:3d} mm toward the arm -> x={x:.0f} y={y:.0f}: "
                  f"{'reachable' if ok else 'not reachable'}")
            if ok:
                chosen = (x, y, shift)
                break
        if chosen is None:
            print(f"[drops] {color}: NO reachable drop point; move the sheet closer to the arm")
            continue
        x, y, shift = chosen
        half_short = min(zone["size_mm"]) / 2
        zone["drop_world"] = {"x": round(float(x), 1), "y": round(float(y), 1), "shift_toward_arm_mm": shift}
        save_pose(f"{color}_zone", zone)
        print(f"[drops] {color}: drop point saved (x={x:.0f}, y={y:.0f}), {shift} mm from the zone center "
              f"(zone is ~{half_short:.0f} mm from center to its nearest edge)")


def show() -> None:
    poses = load_poses()
    for color in ("blue", *COLORS):
        z = poses.get(f"{color}_zone")
        if z is None:
            print(f"  {color:6} zone: not saved")
            continue
        c = z["center_world"]
        size = z.get("size_mm", "")
        d = z.get("drop_world")
        drop = f"  drop x={d['x']:.1f} y={d['y']:.1f}" if d else ""
        print(f"  {color:6} zone: center x={c['x']:.1f} y={c['y']:.1f}  {size}{drop}  ({z['recorded_at']})")


async def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--survey", action="store_true")
    p.add_argument("--show", action="store_true")
    p.add_argument("--check-drops", action="store_true")
    args = p.parse_args()
    if args.check_drops:
        import find_pick
        from inspection import go_to_overview
        find_pick.ASK_BEFORE_MOVING = False
        async with await connect() as machine:
            await check_drops(machine)
            await go_to_overview(machine)
        show()
        return
    if args.show or not args.survey:
        show()
        return
    async with await connect() as machine:
        found = await survey(machine)
        from inspection import go_to_overview
        await go_to_overview(machine)
    for color, z in found.items():
        save_pose(f"{color}_zone", z)
    for color in COLORS:  # drop stale entries for zones not found this time
        if color not in found and f"{color}_zone" in load_poses():
            poses = load_poses(); poses.pop(f"{color}_zone")
            POSES_FILE.write_text(json.dumps(poses, indent=2, sort_keys=True) + "\n")
    missing = [c for c in COLORS if c not in found]
    print(f"[zones] saved: {', '.join(f'{c}_zone' for c in found) or 'nothing'}"
          + (f"; NOT found: {', '.join(missing)}" if missing else ""))
    show()


if __name__ == "__main__":
    asyncio.run(main())
