"""Vision helpers: find the blue zone and the box in it, and turn pixels into table positions.

Pixel -> table works only for photos taken from a saved pose (e.g. overview-blue),
because it needs to know exactly where the camera was.

Usage:
    python detect.py                     # live photo (arm must be at overview-blue): zone + box,
                                         # and save the zone center as "tape_spot"
    python detect.py --no-save           # live photo, just print
    python detect.py --reference         # reference/overview-blue.jpg, no robot needed, just print
    python detect.py --image PATH        # any saved photo taken from overview-blue, just print
    python detect.py --save-photo NAME   # live photo, also keep it as reference/NAME.jpg for offline tests
"""
import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from poses import get_pose, save_pose

VIEW = "overview-blue"
REFERENCE_DIR = Path(__file__).parent / "reference"
REFERENCE_IMAGE = REFERENCE_DIR / "overview-blue.jpg"
TABLE_TOP_Z = -23.0  # world frame, from check_frames.py (depth camera agrees within ~5 mm)

# OpenCV hue runs 0-180. Tuned on reference/overview-blue.jpg.
BLUE_HSV_LOW = (95, 120, 60)
BLUE_HSV_HIGH = (125, 255, 255)
MIN_ZONE_AREA_PX = 50_000

# The blue zone is a ~322 mm square (measured from the reference photo). If what we see is a
# different size, something is covering part of it and the center would be wrong.
ZONE_SIDE_MM = 322.0
ZONE_SIDE_TOLERANCE_MM = 15.0
ZONE_EDGE_MARGIN_PX = 12  # ignore the zone's border when looking for the box

# The box (PLAN.md). Generous tolerances: shadows and edges blur the outline.
BOX_L, BOX_W, BOX_H = 107.0, 67.0, 24.0
BOX_L_TOLERANCE_MM = 30.0
BOX_W_TOLERANCE_MM = 25.0
MIN_BOX_HEIGHT_MM = 12.0  # depth check: must stick up at least this much above the paper


class NotAtViewError(RuntimeError):
    """The arm isn't at VIEW, so pixel -> table would be wrong."""


class ZoneBlockedError(LookupError):
    """The blue zone is missing, partly covered, or out of view."""


class NoBoxError(LookupError):
    """The zone is visible but no box-sized object is in it."""


@dataclass
class BoxDetection:
    x: float                   # center of the box footprint, world mm
    y: float
    length_mm: float           # measured size of the top face
    width_mm: float
    yaw_deg: float             # direction of the long side in the world x-y plane
    height_mm: float | None    # from depth; None if no depth image
    pixel: tuple               # center in the photo


def find_blue_zone(bgr: np.ndarray):
    """Largest blue region. Returns OpenCV minAreaRect ((cx, cy), (w, h), angle) in pixels, or None."""
    mask = cv2.inRange(cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV), BLUE_HSV_LOW, BLUE_HSV_HIGH)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    biggest = max(contours, key=cv2.contourArea)
    if cv2.contourArea(biggest) < MIN_ZONE_AREA_PX:
        return None
    return cv2.minAreaRect(biggest)


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


def zone_side_lengths(rect, view: dict) -> list:
    return side_lengths(rect_corners_world(rect, view))


def check_zone_size(rect, view: dict) -> None:
    """Raise ZoneBlockedError if the detected zone isn't the expected size (partly covered or out of view)."""
    sides = zone_side_lengths(rect, view)
    if any(abs(s - ZONE_SIDE_MM) > ZONE_SIDE_TOLERANCE_MM for s in sides):
        raise ZoneBlockedError("blue zone looks partly covered or out of view: sides "
                               + ", ".join(f"{s:.0f}" for s in sides) + f" mm, expected ~{ZONE_SIDE_MM:.0f}")


def find_zone(bgr: np.ndarray, view: dict):
    """The blue zone's rect, checked for size. Raises ZoneBlockedError."""
    rect = find_blue_zone(bgr)
    if rect is None:
        raise ZoneBlockedError("no blue zone found in the photo")
    check_zone_size(rect, view)
    return rect


def _box_height_mm(depth: np.ndarray, zone_inside: np.ndarray, box_mask: np.ndarray) -> float | None:
    """How far the box top sticks up above the paper, from the depth image (mm)."""
    paper = depth[(zone_inside > 0) & (box_mask == 0) & (depth > 0)]
    top = depth[(cv2.erode(box_mask, np.ones((9, 9), np.uint8)) > 0) & (depth > 0)]
    if paper.size < 100 or top.size < 100:
        return None
    return float(np.median(paper) - np.median(top))


def find_box(bgr: np.ndarray, view: dict, depth: np.ndarray | None = None) -> BoxDetection:
    """Find the box inside the blue zone. Raises ZoneBlockedError or NoBoxError."""
    zone = find_zone(bgr, view)

    zone_inside = np.zeros(bgr.shape[:2], np.uint8)
    cv2.fillConvexPoly(zone_inside, cv2.boxPoints(zone).astype(np.int32), 255)
    zone_inside = cv2.erode(zone_inside, np.ones((2 * ZONE_EDGE_MARGIN_PX + 1,) * 2, np.uint8))

    blue = cv2.inRange(cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV), BLUE_HSV_LOW, BLUE_HSV_HIGH)
    not_blue = cv2.bitwise_and(cv2.bitwise_not(blue), zone_inside)
    not_blue = cv2.morphologyEx(not_blue, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))
    contours, _ = cv2.findContours(not_blue, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    box_top_z = TABLE_TOP_Z + BOX_H  # measure on the box's top face, not the table, to avoid parallax
    rejected = []
    for contour in sorted(contours, key=cv2.contourArea, reverse=True):
        rect = cv2.minAreaRect(contour)
        corners = rect_corners_world(rect, view, box_top_z)
        sides = side_lengths(corners)
        length, width = max(sides), min(sides)
        if abs(length - BOX_L) > BOX_L_TOLERANCE_MM or abs(width - BOX_W) > BOX_W_TOLERANCE_MM:
            if length > 20:
                rejected.append(f"{length:.0f} x {width:.0f} mm (wrong size)")
            continue

        height = None
        if depth is not None:
            box_mask = np.zeros(bgr.shape[:2], np.uint8)
            cv2.drawContours(box_mask, [contour], -1, 255, cv2.FILLED)
            height = _box_height_mm(depth, zone_inside, box_mask)
            if height is not None and height < MIN_BOX_HEIGHT_MM:
                rejected.append(f"{length:.0f} x {width:.0f} mm but only {height:.0f} mm tall (flat)")
                continue

        x, y = pixel_to_table(*rect[0], view, box_top_z)
        i = int(np.argmax(sides))
        dx, dy = np.subtract(corners[(i + 1) % 4], corners[i])
        yaw = float(np.degrees(np.arctan2(dy, dx))) % 180.0
        return BoxDetection(x, y, length, width, yaw, height, tuple(round(c) for c in rect[0]))

    detail = f" (saw: {'; '.join(rejected)})" if rejected else ""
    raise NoBoxError(f"no box in the blue zone{detail}")


async def live_images(machine) -> tuple:
    """(color BGR, depth in mm or None) from the camera. Refuses unless the arm is at VIEW."""
    from viam.components.arm import Arm
    from viam.components.camera import Camera

    from robot import ARM, CAMERA

    joints = (await Arm.from_robot(machine, ARM).get_joint_positions()).values
    drift = max(abs(a - b) for a, b in zip(joints, get_pose(VIEW)["joints"]))
    if drift > 0.5:
        raise NotAtViewError(f"Arm is {drift:.1f} deg away from {VIEW}; move it there first.")
    images, _ = await Camera.from_robot(machine, CAMERA).get_images()
    color = next(i for i in images if i.name == "color")
    bgr = cv2.imdecode(np.frombuffer(color.data, np.uint8), cv2.IMREAD_COLOR)
    depth_image = next((i for i in images if i.name == "depth"), None)
    depth = np.array(depth_image.bytes_to_depth_array()) if depth_image else None
    if depth is not None and depth.shape != bgr.shape[:2]:
        depth = None  # not aligned with the color image; skip the height check
    return bgr, depth


async def detect_box(machine) -> BoxDetection:
    """Live: photograph from VIEW and find the box. Raises NotAtViewError, ZoneBlockedError, NoBoxError."""
    bgr, depth = await live_images(machine)
    return find_box(bgr, get_pose(VIEW), depth)


async def detect_blue_center(machine) -> tuple:
    """Live: photograph from VIEW and return the blue zone center (x, y) on the table, world mm."""
    bgr, _ = await live_images(machine)
    view = get_pose(VIEW)
    return pixel_to_table(*find_zone(bgr, view)[0], view)


def save_tape_spot(x: float, y: float) -> None:
    view = get_pose(VIEW)
    save_pose("tape_spot", {
        "joints": None,
        "tip_world": {"x": round(x, 3), "y": round(y, 3), "z": TABLE_TOP_Z,
                      "o_x": 0.0, "o_y": 0.0, "o_z": -1.0, "theta": view["tip_world"]["theta"]},
        "source": f"blue zone center computed from a photo at {VIEW}",
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
    })


async def _live_main() -> tuple:
    from robot import connect

    async with await connect() as machine:
        try:
            return await live_images(machine)
        except NotAtViewError as e:
            raise SystemExit(str(e))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reference", action="store_true", help="use the saved reference photo, no robot (never saves)")
    parser.add_argument("--image", help="use this photo (taken from overview-blue), no robot (never saves)")
    parser.add_argument("--no-save", action="store_true", help="don't update tape_spot")
    parser.add_argument("--save-photo", metavar="NAME", help="keep the live photo as reference/NAME.jpg")
    args = parser.parse_args()

    view = get_pose(VIEW)
    if "camera_axes" not in view:
        raise SystemExit(f"{VIEW} was recorded before camera axes were saved. Re-record it with --force.")
    offline = args.reference or args.image
    if offline:
        bgr, depth = cv2.imread(str(args.image or REFERENCE_IMAGE)), None
    else:
        bgr, depth = asyncio.run(_live_main())
        if args.save_photo:
            out = REFERENCE_DIR / f"{args.save_photo}.jpg"
            cv2.imwrite(str(out), bgr)
            print(f"Photo saved to {out}")

    rect = find_blue_zone(bgr)
    if rect is None:
        raise SystemExit("No blue zone found.")
    (u, v), (w, h), angle = rect
    x, y = pixel_to_table(u, v, view)
    print(f"Blue zone in photo: center px=({u:.0f}, {v:.0f}), size {w:.0f} x {h:.0f} px, angle {angle:.1f} deg")
    print(f"Blue zone center on table: x={x:.1f}  y={y:.1f}  (world mm)")
    print("Blue zone side lengths: " + ", ".join(f"{s:.0f}" for s in zone_side_lengths(rect, view)) + " mm")

    try:
        box = find_box(bgr, view, depth)
        height = f"{box.height_mm:.0f} mm tall" if box.height_mm is not None else "height not checked (no depth)"
        print(f"Box: center x={box.x:.1f}  y={box.y:.1f}, top {box.length_mm:.0f} x {box.width_mm:.0f} mm, "
              f"yaw {box.yaw_deg:.0f} deg, {height}")
    except LookupError as e:
        print(f"Box: {e}")

    try:
        check_zone_size(rect, view)
    except ZoneBlockedError as e:
        raise SystemExit(f"Not saving tape_spot: {e}. Clear the blue zone and try again.")
    if not (offline or args.no_save):
        save_tape_spot(x, y)
        print("Saved zone center as 'tape_spot'. Commit poses.json only if the team should get this update.")


if __name__ == "__main__":
    main()
