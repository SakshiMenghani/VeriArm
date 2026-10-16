"""Vision helpers: find the blue zone in a photo and turn pixels into table positions.

Pixel -> table works only for photos taken from a saved pose (e.g. overview-blue),
because it needs to know exactly where the camera was.

Usage:
    python detect.py                      # use reference/overview-blue.jpg, no robot needed
    python detect.py --live               # take a fresh photo (arm must be at overview-blue)
    python detect.py --save-tape-spot     # also save the blue zone center as "tape_spot"
"""
import argparse
import asyncio
from pathlib import Path

import cv2
import numpy as np

from poses import get_pose, load_poses, save_pose

VIEW = "overview-blue"
REFERENCE_IMAGE = Path(__file__).parent / "reference" / "overview-blue.jpg"
TABLE_TOP_Z = -23.0  # world frame, from check_frames.py (depth camera agrees within ~5 mm)

# OpenCV hue runs 0-180. Tuned on reference/overview-blue.jpg.
BLUE_HSV_LOW = (95, 120, 60)
BLUE_HSV_HIGH = (125, 255, 255)
MIN_ZONE_AREA_PX = 50_000


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


async def live_photo() -> np.ndarray:
    from viam.components.arm import Arm
    from viam.components.camera import Camera

    from robot import ARM, CAMERA, connect

    async with await connect() as machine:
        joints = (await Arm.from_robot(machine, ARM).get_joint_positions()).values
        drift = max(abs(a - b) for a, b in zip(joints, get_pose(VIEW)["joints"]))
        if drift > 0.5:
            raise SystemExit(f"Arm is {drift:.1f} deg away from {VIEW}; move it there first.")
        images, _ = await Camera.from_robot(machine, CAMERA).get_images()
        color = next(i for i in images if i.name == "color")
        return cv2.imdecode(np.frombuffer(color.data, np.uint8), cv2.IMREAD_COLOR)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="take a fresh photo instead of the reference image")
    parser.add_argument("--save-tape-spot", action="store_true", help="save the zone center as the tape_spot pose")
    args = parser.parse_args()

    view = get_pose(VIEW)
    if "camera_axes" not in view:
        raise SystemExit(f"{VIEW} was recorded before camera axes were saved. Re-record it with --force.")
    bgr = asyncio.run(live_photo()) if args.live else cv2.imread(str(REFERENCE_IMAGE))

    rect = find_blue_zone(bgr)
    if rect is None:
        raise SystemExit("No blue zone found.")
    (u, v), (w, h), angle = rect
    x, y = pixel_to_table(u, v, view)
    corners = zone_corners_world(rect, view)
    side_lengths = [np.hypot(*np.subtract(corners[i], corners[(i + 1) % 4])) for i in range(4)]

    print(f"Blue zone in photo: center px=({u:.0f}, {v:.0f}), size {w:.0f} x {h:.0f} px, angle {angle:.1f} deg")
    print(f"Blue zone center on table: x={x:.1f}  y={y:.1f}  (world mm)")
    print("Blue zone side lengths: " + ", ".join(f"{s:.0f}" for s in side_lengths) + " mm")
    print("Blue zone corners: " + "  ".join(f"({cx:.0f}, {cy:.0f})" for cx, cy in corners))

    if args.save_tape_spot:
        theta = view["tip_world"]["theta"]
        save_pose("tape_spot", {
            "joints": None,
            "tip_world": {"x": round(x, 3), "y": round(y, 3), "z": TABLE_TOP_Z,
                          "o_x": 0.0, "o_y": 0.0, "o_z": -1.0, "theta": theta},
            "source": f"blue zone center computed from the {VIEW} photo",
            "recorded_at": load_poses()[VIEW]["recorded_at"],
        })
        print("Saved as 'tape_spot'.")


if __name__ == "__main__":
    main()
