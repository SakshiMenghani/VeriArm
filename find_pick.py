"""Find the box in the blue zone (Sandy: find_box, locate) and pick it up (Yixin: pick_box).

find_box() takes a live photo from overview-blue and finds the box anywhere in the blue zone.
If the zone is blocked or there's no box, it raises an error instead of guessing.

Every motion asks for confirmation unless --yes is passed. Keep someone at the e-stop.

Usage:
    python find_pick.py plan      # print the targets, no connection to the robot
    python find_pick.py locate    # no motion: arm must be at overview; print where the box is
    python find_pick.py locate --image reference/box-center.jpg   # same, offline from a saved photo
    python find_pick.py find      # move to overview, print box_center
    python find_pick.py hover     # put the suction tip 50 mm above the box top (hover test)
    python find_pick.py pick      # pick the box (Yixin's, untested draft): approach, lower, suction on, lift
    python find_pick.py release   # turn suction off
"""
import argparse
import asyncio

from viam.components.arm import Arm
from viam.components.gripper import Gripper
from viam.proto.component.arm import JointPositions
from viam.proto.common import Pose, PoseInFrame
from viam.proto.service.motion import Constraints, LinearConstraint
from viam.robot.client import RobotClient
from viam.services.motion import MotionClient

from detect import (KNOWN_BOXES, TABLE_TOP_Z, VIEW, BoxDetection, find_box_in_photo, live_images,
                    save_debug_image)
from poses import get_pose
from robot import ARM, GRIPPER, MOTION, connect

OVERVIEW = "overview-blue"

# The box we demo with (detect.KNOWN_BOXES). find_box() uses the height of whichever
# known box it detects; this is only for the offline `plan` printout.
BOX_L, BOX_W, BOX_H = KNOWN_BOXES["large"]

# The real suction tip is ~17 mm lower than the config thinks (measured twice by hand,
# see PLAN.md). Aim this much higher so the real tip lands where we mean.
SUCTION_Z_CORRECTION_MM = 17.0

APPROACH_MM = 50.0  # hover height above the box top before descending
PRESS_MM = 2.0      # go slightly past the top surface so the cup seals
LIFT_MM = 80.0      # how far to lift after grabbing

ASK_BEFORE_MOVING = True


def confirm(what: str) -> None:
    if ASK_BEFORE_MOVING and input(f"\nAbout to {what}. Press Enter to go, anything else to abort: ").strip():
        raise SystemExit("Aborted.")


def tip_target(box_center: Pose, height_above_top: float) -> Pose:
    """Suction tip pointing straight down, `height_above_top` mm above the real box top (box_center.z)."""
    return Pose(
        x=box_center.x,
        y=box_center.y,
        z=box_center.z + height_above_top + SUCTION_Z_CORRECTION_MM,
        o_x=0, o_y=0, o_z=-1,
        theta=box_center.theta,
    )


async def move_tip(machine: RobotClient, target: Pose, what: str, straight_line: bool = False) -> None:
    confirm(f"{what} -> tip to x={target.x:.0f} y={target.y:.0f} z={target.z:.0f}")
    motion = MotionClient.from_robot(machine, MOTION)
    constraints = Constraints(linear_constraint=[LinearConstraint(line_tolerance_mm=5)]) if straight_line else None
    await motion.move(
        component_name=GRIPPER,
        destination=PoseInFrame(reference_frame="world", pose=target),
        constraints=constraints,
    )


def box_center_from(box: BoxDetection) -> Pose:
    """box_center for a detection: x, y = box center, z = TOP of the box (table top + known box height)."""
    theta = get_pose(OVERVIEW)["tip_world"]["theta"]
    top_z = TABLE_TOP_Z + KNOWN_BOXES[box.name][2]  # known height: the depth measurement runs ~5 mm low
    return Pose(x=box.x, y=box.y, z=top_z, o_x=0, o_y=0, o_z=1, theta=theta)


async def find_box(machine: RobotClient) -> Pose:
    """Move to overview and return box_center: x, y = center of the box, z = TOP of the box. mm, world frame."""
    # Use the saved joint angles, not the tip pose: the planner can reach the same tip pose
    # with a different arm posture (we saw it pick one at a joint limit), and detection
    # needs the camera exactly where it was when the pose was recorded.
    confirm(f"move to {OVERVIEW} (joint move)")
    await Arm.from_robot(machine, ARM).move_to_joint_positions(JointPositions(values=get_pose(OVERVIEW)["joints"]))
    box = await _detect_and_draw(machine)  # raises ZoneBlockedError / NoBoxError: don't guess, let the caller decide
    print(f"Box found: x={box.x:.1f} y={box.y:.1f}, {box.length_mm:.0f} x {box.width_mm:.0f} mm, yaw {box.yaw_deg:.0f} deg")
    return box_center_from(box)


async def hover_over_box(machine: RobotClient, box_center: Pose) -> None:
    """Hover test from PLAN.md: tip should end up over the real box center, within a couple of cm."""
    await move_tip(machine, tip_target(box_center, APPROACH_MM), f"hover {APPROACH_MM:.0f} mm above the box")


async def pick_box(machine: RobotClient, box_center: Pose) -> None:
    """Yixin owns this. Untested draft: never run on a real box yet; change or replace freely."""
    gripper = Gripper.from_robot(machine, GRIPPER)
    await move_tip(machine, tip_target(box_center, APPROACH_MM), "approach above the box")
    await move_tip(machine, tip_target(box_center, -PRESS_MM), "lower onto the box top", straight_line=True)
    confirm("turn suction ON")
    await gripper.grab()
    await move_tip(machine, tip_target(box_center, LIFT_MM), "lift the box", straight_line=True)
    print("Holding something:", await gripper.is_holding_something())


async def release(machine: RobotClient) -> None:
    confirm("turn suction OFF (the box will drop if lifted)")
    await Gripper.from_robot(machine, GRIPPER).open()


def print_locate(box: BoxDetection) -> None:
    """Print where the box is (no grip decisions: that's pick_box's job)."""
    print(f"Box '{box.name}': {box.length_mm:.0f} x {box.width_mm:.0f} x {box.height_mm:.0f} mm (measured), "
          f"yaw {box.yaw_deg:.0f} deg")
    c = box_center_from(box)
    print(f"  box_center (world mm):  x={c.x:.1f}  y={c.y:.1f}  z={c.z:.1f}  "
          f"(z = top of the box: table {TABLE_TOP_Z:.0f} + {KNOWN_BOXES[box.name][2]:.0f} mm box height)")


async def _detect_and_draw(machine: RobotClient) -> BoxDetection:
    """Photograph from overview (no motion) and find the box. Saves captures/detection.jpg."""
    bgr, depth = await live_images(machine)
    try:
        box = find_box_in_photo(bgr, get_pose(VIEW), depth)
    except LookupError as e:
        save_debug_image(bgr, None, f"{type(e).__name__}: {e}"[:90], open_it=False)
        raise
    save_debug_image(bgr, box, open_it=False)
    return box


async def locate(machine: RobotClient) -> None:
    """No motion: photograph from overview and print where the box is. Opens the picture."""
    try:
        print_locate(await _detect_and_draw(machine))
    finally:
        _open_picture()


def _open_picture() -> None:
    import subprocess
    import sys
    from detect import DEBUG_IMAGE
    opener = {"darwin": ["open"], "linux": ["xdg-open"]}.get(sys.platform)
    if opener and DEBUG_IMAGE.exists():
        subprocess.run(opener + [str(DEBUG_IMAGE)], check=False)
    print(f"Picture: {DEBUG_IMAGE}  (pink = blue zone, yellow = box, red = center)")


def print_plan() -> None:
    zone = get_pose("blue_zone")["center_world"]
    theta = get_pose(OVERVIEW)["tip_world"]["theta"]
    box_center = Pose(x=zone["x"], y=zone["y"], z=TABLE_TOP_Z + BOX_H, theta=theta)
    print(f"Example: box at the blue zone center x={box_center.x:.1f} y={box_center.y:.1f}, "
          f"box {BOX_L:.0f} x {BOX_W:.0f} x {BOX_H:.0f} mm")
    print(f"box_center.z = box top (real): z={box_center.z:.1f}")
    for label, h in (("approach", APPROACH_MM), ("contact", -PRESS_MM), ("lift", LIFT_MM)):
        t = tip_target(box_center, h)
        print(f"  {label:9s} commanded tip z={t.z:6.1f}  (real tip ~{h:+.0f} mm vs box top)")


async def main():
    global ASK_BEFORE_MOVING
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["plan", "locate", "find", "hover", "pick", "release"])
    parser.add_argument("--yes", action="store_true", help="don't ask before each motion")
    parser.add_argument("--image", help="locate only: use a saved photo from overview-blue (+ its .npy depth)")
    args = parser.parse_args()
    ASK_BEFORE_MOVING = not args.yes

    if args.action == "plan":
        print_plan()
        return
    if args.action == "locate" and args.image:
        import cv2
        import numpy as np
        from pathlib import Path
        depth_file = Path(args.image).with_suffix(".npy")
        depth = np.load(depth_file) if depth_file.exists() else None
        bgr = cv2.imread(args.image)
        try:
            box = find_box_in_photo(bgr, get_pose(VIEW), depth)
        except LookupError as e:
            save_debug_image(bgr, None, f"{type(e).__name__}: {e}"[:90])
            raise SystemExit(f"{type(e).__name__}: {e}")
        print_locate(box)
        print(f"Picture: {save_debug_image(bgr, box)}  (pink = blue zone, yellow = box, red = center)")
        return

    async with await connect() as machine:
        if args.action == "release":
            await release(machine)
            return
        if args.action == "locate":
            try:
                await locate(machine)
            except (LookupError, RuntimeError) as e:  # NoBoxError, ZoneBlockedError, NotAtViewError
                raise SystemExit(f"{type(e).__name__}: {e}")
            return
        try:
            box_center = await find_box(machine)
        except LookupError as e:  # ZoneBlockedError or NoBoxError
            raise SystemExit(f"Not moving: {e}")
        print(f"box_center: x={box_center.x:.1f} y={box_center.y:.1f} z={box_center.z:.1f}")
        if args.action == "hover":
            await hover_over_box(machine, box_center)
        elif args.action == "pick":
            await pick_box(machine, box_center)


if __name__ == "__main__":
    asyncio.run(main())
