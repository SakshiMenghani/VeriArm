"""Vision helpers: find the blue zone in a photo and turn pixels into table positions.

Pixel -> table works only for photos taken from a saved pose (e.g. overview-blue),
because it needs to know exactly where the camera was.

Usage:
    python detect.py               # live photo (arm must be at overview-blue), save center as "tape_spot"
    python detect.py --no-save     # live photo, just print
    python detect.py --reference   # use reference/overview-blue.jpg instead (no robot needed), just print
"""
import argparse
import asyncio
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from poses import get_pose, save_pose

VIEW = "overview-blue"
REFERENCE_IMAGE = Path(__file__).parent / "reference" / "overview-blue.jpg"
TABLE_TOP_Z = -23.0  # world frame, from check_frames.py (depth camera agrees within ~5 mm)

# OpenCV hue runs 0-180. Tuned on reference/overview-blue.jpg.
BLUE_HSV_LOW = (95, 120, 60)
BLUE_HSV_HIGH = (125, 255, 255)
MIN_ZONE_AREA_PX = 50_000

# The blue zone is a ~322 mm square (measured from the reference photo). If what we see is a
# different size, something is covering part of it and the center would be wrong.
ZONE_SIDE_MM = 322.0
ZONE_SIDE_TOLERANCE_MM = 15.0


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


def zone_corners_world(rect, view: dict) -> list:
    return [pixel_to_table(u, v, view) for u, v in cv2.boxPoints(rect)]


def zone_side_lengths(rect, view: dict) -> list:
    corners = zone_corners_world(rect, view)
    return [float(np.hypot(*np.subtract(corners[i], corners[(i + 1) % 4]))) for i in range(4)]


def check_zone_size(rect, view: dict) -> None:
    """Raise LookupError if the detected zone isn't the expected size (partly covered or out of view)."""
    sides = zone_side_lengths(rect, view)
    if any(abs(s - ZONE_SIDE_MM) > ZONE_SIDE_TOLERANCE_MM for s in sides):
        raise LookupError("blue zone looks partly covered or out of view: sides "
                          + ", ".join(f"{s:.0f}" for s in sides) + f" mm, expected ~{ZONE_SIDE_MM:.0f}")


class NotAtViewError(RuntimeError):
    pass


async def live_photo(machine) -> np.ndarray:
    """Color photo from the camera. Refuses unless the arm is at VIEW, since pixel -> table depends on it."""
    from viam.components.arm import Arm
    from viam.components.camera import Camera

    from robot import ARM, CAMERA

    joints = (await Arm.from_robot(machine, ARM).get_joint_positions()).values
    drift = max(abs(a - b) for a, b in zip(joints, get_pose(VIEW)["joints"]))
    if drift > 0.5:
        raise NotAtViewError(f"Arm is {drift:.1f} deg away from {VIEW}; move it there first.")
    images, _ = await Camera.from_robot(machine, CAMERA).get_images()
    color = next(i for i in images if i.name == "color")
    return cv2.imdecode(np.frombuffer(color.data, np.uint8), cv2.IMREAD_COLOR)


async def detect_blue_center(machine) -> tuple:
    """Live: photograph from VIEW and return the blue zone center (x, y) on the table, world mm.
    Raises NotAtViewError or LookupError (no blue zone found)."""
    rect = find_blue_zone(await live_photo(machine))
    if rect is None:
        raise LookupError("No blue zone found in the photo.")
    check_zone_size(rect, get_pose(VIEW))
    return pixel_to_table(*rect[0], get_pose(VIEW))


def save_tape_spot(x: float, y: float) -> None:
    view = get_pose(VIEW)
    save_pose("tape_spot", {
        "joints": None,
        "tip_world": {"x": round(x, 3), "y": round(y, 3), "z": TABLE_TOP_Z,
                      "o_x": 0.0, "o_y": 0.0, "o_z": -1.0, "theta": view["tip_world"]["theta"]},
        "source": f"blue zone center computed from a photo at {VIEW}",
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
    })


async def _live_main() -> np.ndarray:
    from robot import connect

    async with await connect() as machine:
        try:
            return await live_photo(machine)
        except NotAtViewError as e:
            raise SystemExit(str(e))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reference", action="store_true", help="use the saved reference photo, no robot (never saves)")
    parser.add_argument("--no-save", action="store_true", help="don't update tape_spot")
    args = parser.parse_args()

    view = get_pose(VIEW)
    if "camera_axes" not in view:
        raise SystemExit(f"{VIEW} was recorded before camera axes were saved. Re-record it with --force.")
    bgr = cv2.imread(str(REFERENCE_IMAGE)) if args.reference else asyncio.run(_live_main())

    rect = find_blue_zone(bgr)
    if rect is None:
        raise SystemExit("No blue zone found.")
    (u, v), (w, h), angle = rect
    x, y = pixel_to_table(u, v, view)
    corners = zone_corners_world(rect, view)
    side_lengths = zone_side_lengths(rect, view)

    print(f"Blue zone in photo: center px=({u:.0f}, {v:.0f}), size {w:.0f} x {h:.0f} px, angle {angle:.1f} deg")
    print(f"Blue zone center on table: x={x:.1f}  y={y:.1f}  (world mm)")
    print("Blue zone side lengths: " + ", ".join(f"{s:.0f}" for s in side_lengths) + " mm")
    print("Blue zone corners: " + "  ".join(f"({cx:.0f}, {cy:.0f})" for cx, cy in corners))

    try:
        check_zone_size(rect, view)
    except LookupError as e:
        raise SystemExit(f"Not saving: {e}. Clear the blue zone and try again.")
    if not (args.reference or args.no_save):
        save_tape_spot(x, y)
        print("Saved as 'tape_spot'. Commit poses.json only if the team should get this update.")


if __name__ == "__main__":
    main()
