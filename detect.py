"""Vision helpers: find the box in the blue zone and turn pixels into table positions.

Pixel -> table works only for photos taken from a saved pose (e.g. overview-blue),
because it needs to know exactly where the camera was.

The blue zone's position is saved once (as "blue_zone" in poses.json) from a photo of the
empty zone. The box is then found by height (depth camera) inside that area, and matched
against the known box sizes, so any color of box works and hands or flat paper are ignored.

Setup commands (to find the box, use `python find_pick.py find`):
    python detect.py --save-zone           # arm at overview-blue, zone EMPTY: save its position as "blue_zone"
    python detect.py --save-photo NAME     # arm at overview-blue: keep the photo (+ depth) in reference/ for tests
    python detect.py --save-zone --image PATH   # save the zone from a saved photo of the empty zone
"""
import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from poses import get_pose, load_poses, save_pose

VIEW = "overview-blue"
REFERENCE_DIR = Path(__file__).parent / "reference"
TABLE_TOP_Z = -23.0  # world frame, from check_frames.py (depth camera agrees within ~5 mm)

# Known boxes: name -> (length, width, height) in mm. Add new sizes here.
KNOWN_BOXES = {
    "large": (272.0, 170.0, 61.0),  # 10.7 x 6.7 x 2.4 in
    "tall": (234.0, 165.0, 140.0),  # ruler; camera sees ~244 x 173 x 154 (taped top may bulge)
}
SIZE_TOLERANCE_MM = 30.0     # length / width
HEIGHT_TOLERANCE_MM = 25.0     # depth reads up to ~15 mm off (bulging tops); our boxes differ by ~80 mm
MIN_OBJECT_HEIGHT_MM = 20.0  # anything taller than this above the paper is an object
ZONE_SEARCH_MARGIN_PX = 60   # boxes may hang a little over the zone's edge

# OpenCV hue runs 0-180. Tuned on reference/overview-blue.jpg.
BLUE_HSV_LOW = (95, 120, 60)
BLUE_HSV_HIGH = (125, 255, 255)
MIN_ZONE_AREA_PX = 50_000
ZONE_SIDE_MM = 322.0
ZONE_SIDE_TOLERANCE_MM = 15.0


class NotAtViewError(RuntimeError):
    """The arm isn't at VIEW, so pixel -> table would be wrong."""


class ZoneBlockedError(LookupError):
    """The camera can't see the blue zone well enough to look for a box."""


class NoBoxError(LookupError):
    """No known box in the blue zone."""


@dataclass
class BoxDetection:
    name: str                # which entry of KNOWN_BOXES matched
    x: float                 # center of the box footprint, world mm
    y: float
    length_mm: float         # measured size of the top face
    width_mm: float
    height_mm: float         # KNOWN height of the matched box (KNOWN_BOXES): use this for positions
    yaw_deg: float           # direction of the long side in the world x-y plane, 0-180
    pixel: tuple             # center in the photo
    corners_px: list         # outline in the photo (4 corners), for drawing
    measured_height_mm: float | None = None  # what the depth camera saw (varies ~5 mm); for recognizing/debugging


# ---------- geometry ----------

def pixel_to_table(u: float, v: float, view: dict, plane_z: float = TABLE_TOP_Z) -> tuple:
    """Where the camera ray through pixel (u, v) hits the horizontal plane z = plane_z (world, mm)."""
    k = view["camera_intrinsics"]
    axes = view["camera_axes"]
    ray_cam = np.array([(u - k["cx"]) / k["fx"], (v - k["cy"]) / k["fy"], 1.0])
    rotation = np.column_stack([axes["x"], axes["y"], axes["z"]])
    ray = rotation @ ray_cam
    origin = np.array(axes["origin"])
    if abs(ray[2]) < 1e-6:
        raise ValueError("Camera ray is parallel to the table.")
    s = (plane_z - origin[2]) / ray[2]
    hit = origin + s * ray
    return float(hit[0]), float(hit[1])


def rect_corners_world(rect, view: dict, plane_z: float = TABLE_TOP_Z) -> list:
    return [pixel_to_table(u, v, view, plane_z) for u, v in cv2.boxPoints(rect)]


def side_lengths(corners: list) -> list:
    return [float(np.hypot(*np.subtract(corners[i], corners[(i + 1) % 4]))) for i in range(4)]


# ---------- the blue zone ----------

def blue_mask(bgr: np.ndarray) -> np.ndarray:
    return cv2.inRange(cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV), BLUE_HSV_LOW, BLUE_HSV_HIGH)


def find_empty_zone(bgr: np.ndarray, view: dict):
    """The blue zone's minAreaRect in pixels, from a photo of the EMPTY zone. Raises ZoneBlockedError."""
    contours, _ = cv2.findContours(blue_mask(bgr), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours or cv2.contourArea(max(contours, key=cv2.contourArea)) < MIN_ZONE_AREA_PX:
        raise ZoneBlockedError("no blue zone found in the photo")
    rect = cv2.minAreaRect(max(contours, key=cv2.contourArea))
    sides = side_lengths(rect_corners_world(rect, view))
    if any(abs(s - ZONE_SIDE_MM) > ZONE_SIDE_TOLERANCE_MM for s in sides):
        raise ZoneBlockedError("blue zone looks partly covered or out of view: sides "
                               + ", ".join(f"{s:.0f}" for s in sides) + f" mm, expected ~{ZONE_SIDE_MM:.0f}")
    return rect


def save_zone(rect, view: dict) -> None:
    (u, v), (w, h), angle = rect
    x, y = pixel_to_table(u, v, view)
    save_pose("blue_zone", {
        "view": VIEW,
        "pixel_rect": [[round(u, 2), round(v, 2)], [round(w, 2), round(h, 2)], round(angle, 3)],
        "center_world": {"x": round(x, 3), "y": round(y, 3), "z": TABLE_TOP_Z},
        "corners_world": [[round(cx, 1), round(cy, 1)] for cx, cy in rect_corners_world(rect, view)],
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
    })


def saved_zone_rect():
    (u, v), (w, h), angle = load_poses()["blue_zone"]["pixel_rect"]
    return ((u, v), (w, h), angle)


def zone_area_mask(shape, rect, margin_px: int = 0) -> np.ndarray:
    mask = np.zeros(shape[:2], np.uint8)
    cv2.fillConvexPoly(mask, cv2.boxPoints(rect).astype(np.int32), 255)
    if margin_px > 0:
        mask = cv2.dilate(mask, np.ones((2 * margin_px + 1,) * 2, np.uint8))
    elif margin_px < 0:
        mask = cv2.erode(mask, np.ones((-2 * margin_px + 1,) * 2, np.uint8))
    return mask


# ---------- the box ----------

def match_known_box(length: float, width: float, height: float | None):
    """Closest KNOWN_BOXES entry within tolerance, or None."""
    best, best_err = None, None
    for name, (bl, bw, bh) in KNOWN_BOXES.items():
        if abs(length - bl) > SIZE_TOLERANCE_MM or abs(width - bw) > SIZE_TOLERANCE_MM:
            continue
        if height is not None and abs(height - bh) > HEIGHT_TOLERANCE_MM:
            continue
        err = abs(length - bl) + abs(width - bw) + (abs(height - bh) if height is not None else 0)
        if best_err is None or err < best_err:
            best, best_err = name, err
    return best


def _object_mask_from_depth(bgr, depth, zone_rect):
    """Pixels sticking up above the paper, near the zone. Also returns the paper's depth."""
    near_zone = zone_area_mask(bgr.shape, zone_rect, ZONE_SEARCH_MARGIN_PX)
    paper = depth[(blue_mask(bgr) > 0) & (zone_area_mask(bgr.shape, zone_rect, -10) > 0) & (depth > 0)]
    if paper.size < 2000:
        raise ZoneBlockedError("can't see enough of the blue zone to measure the table")
    paper_depth = float(np.median(paper))
    tall = ((paper_depth - depth.astype(np.float32)) > MIN_OBJECT_HEIGHT_MM) & (depth > 0)
    mask = (tall & (near_zone > 0)).astype(np.uint8) * 255
    return cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8)), paper_depth


def _object_mask_from_color(bgr, zone_rect):
    """Offline fallback without depth: anything not blue inside the zone."""
    inside = zone_area_mask(bgr.shape, zone_rect, -12)
    mask = cv2.bitwise_and(cv2.bitwise_not(blue_mask(bgr)), inside)
    return cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))


def find_box_in_photo(bgr: np.ndarray, view: dict, depth: np.ndarray | None = None, zone_rect=None) -> BoxDetection:
    """Find a known box in (or slightly over) the blue zone in an already-taken photo.
    Raises ZoneBlockedError or NoBoxError. (find_pick.find_box is the one that moves the arm first.)"""
    zone_rect = zone_rect or saved_zone_rect()
    if depth is not None:
        mask, paper_depth = _object_mask_from_depth(bgr, depth, zone_rect)
    else:
        mask, paper_depth = _object_mask_from_color(bgr, zone_rect), None

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    seen = []
    for contour in sorted(contours, key=cv2.contourArea, reverse=True):
        if cv2.contourArea(contour) < 500:
            break
        height = None
        if depth is not None:
            region = np.zeros(mask.shape, np.uint8)
            cv2.drawContours(region, [contour], -1, 255, cv2.FILLED)
            top = depth[(cv2.erode(region, np.ones((15, 15), np.uint8)) > 0) & (depth > 0)]
            if top.size:
                height = paper_depth - float(np.median(top))
        rect = cv2.minAreaRect(contour)
        top_z = TABLE_TOP_Z + (height if height is not None else 0.0)
        corners = rect_corners_world(rect, view, top_z)
        sides = side_lengths(corners)
        length, width = max(sides), min(sides)

        name = match_known_box(length, width, height)
        if name is None:
            if height is None and depth is None:
                # No depth: we measured at table height; retry each known box at its own top height.
                for n, (_, _, bh) in KNOWN_BOXES.items():
                    sides_n = side_lengths(rect_corners_world(rect, view, TABLE_TOP_Z + bh))
                    if match_known_box(max(sides_n), min(sides_n), None) == n:
                        name, height, top_z = n, bh, TABLE_TOP_Z + bh
                        corners, sides = rect_corners_world(rect, view, top_z), sides_n
                        length, width = max(sides), min(sides)
                        break
            if name is None:
                h = f" x {height:.0f}" if height is not None else ""
                seen.append(f"{length:.0f} x {width:.0f}{h} mm")
                continue

        # The measurement only told us WHICH box this is. From here on use its known height, so
        # everyone (pick, inspection, place) works from the same exact number.
        measured_height = height if depth is not None else None
        known_height = KNOWN_BOXES[name][2]
        top_z = TABLE_TOP_Z + known_height
        corners = rect_corners_world(rect, view, top_z)
        sides = side_lengths(corners)
        length, width = max(sides), min(sides)
        x, y = pixel_to_table(*rect[0], view, top_z)
        i = int(np.argmax(sides))
        dx, dy = np.subtract(corners[(i + 1) % 4], corners[i])
        yaw = float(np.degrees(np.arctan2(dy, dx))) % 180.0
        return BoxDetection(name, x, y, length, width, known_height, yaw, tuple(round(c) for c in rect[0]),
                            cv2.boxPoints(rect).round().astype(int).tolist(), measured_height)

    detail = f" (saw objects: {'; '.join(seen)}; known boxes: " + ", ".join(
        f"{n} {l:.0f} x {w:.0f} x {h:.0f}" for n, (l, w, h) in KNOWN_BOXES.items()) + ")" if seen else ""
    raise NoBoxError(f"no known box in the blue zone{detail}")


# ---------- picture for humans ----------

DEBUG_IMAGE = Path(__file__).parent / "captures" / "detection.jpg"


def save_debug_image(bgr: np.ndarray, box: BoxDetection | None, message: str = "", open_it: bool = True) -> Path:
    """Draw the saved zone (pink), the detected box (yellow) and its center (red cross); save and open it."""
    img = bgr.copy()
    if "blue_zone" in load_poses():
        cv2.drawContours(img, [cv2.boxPoints(saved_zone_rect()).astype(np.int32)], -1, (255, 0, 255), 2)
    if box is not None:
        cv2.drawContours(img, [np.array(box.corners_px, np.int32)], -1, (0, 255, 255), 3)
        cv2.drawMarker(img, box.pixel, (0, 0, 255), cv2.MARKER_CROSS, 40, 3)
        message = message or f"{box.name}: x={box.x:.0f} y={box.y:.0f} mm, yaw {box.yaw_deg:.0f} deg"
    cv2.rectangle(img, (0, 0), (img.shape[1], 40), (0, 0, 0), -1)
    cv2.putText(img, message or "no box", (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
    DEBUG_IMAGE.parent.mkdir(exist_ok=True)
    cv2.imwrite(str(DEBUG_IMAGE), img)
    if open_it:
        import subprocess
        import sys
        opener = {"darwin": ["open"], "linux": ["xdg-open"]}.get(sys.platform)
        if opener:
            subprocess.run(opener + [str(DEBUG_IMAGE)], check=False)
    return DEBUG_IMAGE


# ---------- live camera ----------

async def live_images(machine) -> tuple:
    """(color BGR, depth in mm or None) from the camera. Refuses unless the arm is at VIEW."""
    from viam.components.arm import Arm
    from viam.components.camera import Camera

    from robot import ARM, CAMERA

    joints = (await Arm.from_robot(machine, ARM).get_joint_positions()).values
    drift = max(abs(a - b) for a, b in zip(joints, get_pose(VIEW)["joints"]))
    if drift > 0.5:
        raise NotAtViewError(f"Arm is {drift:.1f} deg away from {VIEW}; move it there first.")
    camera = Camera.from_robot(machine, CAMERA)
    images, _ = await camera.get_images()
    color = next(i for i in images if i.name == "color")
    bgr = cv2.imdecode(np.frombuffer(color.data, np.uint8), cv2.IMREAD_COLOR)
    # Depth comes from the point cloud, not the "depth" image: the depth image uses the depth
    # sensor's own (wider) field of view, while the point cloud has one point per color pixel.
    cloud, _ = await camera.get_point_cloud()
    depth = point_cloud_to_depth(cloud, bgr.shape[:2])
    return bgr, depth


def point_cloud_to_depth(pcd: bytes, shape: tuple) -> np.ndarray | None:
    """Organized PCD (one point per color pixel, metres) -> depth image in mm, 0 where unknown."""
    start = pcd.index(b"\n", pcd.index(b"DATA")) + 1
    points = np.frombuffer(pcd[start:], dtype=np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("rgb", "<u4")]))
    if len(points) != shape[0] * shape[1]:
        return None  # not one point per pixel; fall back to color
    z = np.nan_to_num(points["z"], nan=0.0) * 1000.0
    return z.reshape(shape).astype(np.float32)


async def detect_box(machine) -> BoxDetection:
    """Live: photograph from VIEW and find the box. Raises NotAtViewError, ZoneBlockedError, NoBoxError."""
    bgr, depth = await live_images(machine)
    return find_box_in_photo(bgr, get_pose(VIEW), depth)


async def _live_main() -> tuple:
    from robot import connect

    async with await connect() as machine:
        try:
            return await live_images(machine)
        except NotAtViewError as e:
            raise SystemExit(str(e))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--save-zone", action="store_true", help="save the (empty) zone's position as blue_zone")
    parser.add_argument("--save-photo", metavar="NAME", help="keep the live photo as reference/NAME.jpg (+ .npy depth)")
    parser.add_argument("--image", help="--save-zone only: use this saved photo instead of a live one")
    args = parser.parse_args()
    if not (args.save_zone or args.save_photo):
        parser.error("nothing to do: use --save-zone and/or --save-photo NAME (to find the box: python find_pick.py find)")

    view = get_pose(VIEW)
    if args.image:
        bgr = cv2.imread(args.image)
    else:
        bgr, depth = asyncio.run(_live_main())
        if args.save_photo:
            out = REFERENCE_DIR / f"{args.save_photo}.jpg"
            cv2.imwrite(str(out), bgr)
            if depth is not None:
                np.save(out.with_suffix(".npy"), depth.astype(np.uint16))
            print(f"Photo saved to {out}" + (" (+ depth)" if depth is not None else ""))

    if args.save_zone:
        try:
            rect = find_empty_zone(bgr, view)
        except ZoneBlockedError as e:
            raise SystemExit(f"Not saving: {e}. Clear the blue zone and try again.")
        save_zone(rect, view)
        z = load_poses()["blue_zone"]["center_world"]
        print(f"Saved blue_zone: center x={z['x']:.1f} y={z['y']:.1f}. Commit poses.json so the team gets it.")


if __name__ == "__main__":
    main()
