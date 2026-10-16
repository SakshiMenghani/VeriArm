"""Sandy: find the box in the blue zone and pick it up with suction.

MVP: box_center is the blue zone center, detected from a live overview photo each run
(falls back to the saved "tape_spot"). Later: find_box() detects the box itself.

Every motion asks for confirmation unless --yes is passed. Keep someone at the e-stop.

Usage:
    python find_pick.py plan      # print the targets, no connection to the robot
    python find_pick.py find      # move to overview, print box_center
    python find_pick.py hover     # put the suction tip 50 mm above the box top (hover test)
    python find_pick.py pick      # pick the box: approach, lower, suction on, lift
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

from detect import detect_blue_center
from poses import get_pose
from robot import ARM, GRIPPER, MOTION, connect

OVERVIEW = "overview-blue"

# Box is a fixed size (mm).
BOX_L, BOX_W, BOX_H = 107.0, 67.0, 24.0

# Table top in the config's world frame, from check_frames.py.
TABLE_TOP_Z = -23.0

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
    """Suction tip pointing straight down, `height_above_top` mm above the real box top."""
    return Pose(
        x=box_center.x,
        y=box_center.y,
        z=box_center.z + BOX_H + height_above_top + SUCTION_Z_CORRECTION_MM,
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


async def find_box(machine: RobotClient) -> Pose:
    """Move to overview and return box_center: x, y of the box, z = table top (box bottom), mm, world frame."""
    # Use the saved joint angles, not the tip pose: the planner can reach the same tip pose
    # with a different arm posture (we saw it pick one at a joint limit), and detection
    # needs the camera exactly where it was when the pose was recorded.
    confirm(f"move to {OVERVIEW} (joint move)")
    await Arm.from_robot(machine, ARM).move_to_joint_positions(JointPositions(values=get_pose(OVERVIEW)["joints"]))
    spot = get_pose("tape_spot")["tip_world"]
    # MVP: the box sits at the blue zone center, measured from a live photo each run.
    # Later: detect the box itself instead of the zone.
    try:
        x, y = await detect_blue_center(machine)
        print(f"Live detection: blue zone center x={x:.1f} y={y:.1f}")
    except (LookupError, RuntimeError) as e:
        x, y = spot["x"], spot["y"]
        print(f"Live detection failed ({e}); using saved tape_spot x={x:.1f} y={y:.1f}")
    return Pose(x=x, y=y, z=TABLE_TOP_Z, o_x=0, o_y=0, o_z=1, theta=spot["theta"])


async def hover_over_box(machine: RobotClient, box_center: Pose) -> None:
    """Hover test from PLAN.md: tip should end up over the real box center, within a couple of cm."""
    await move_tip(machine, tip_target(box_center, APPROACH_MM), f"hover {APPROACH_MM:.0f} mm above the box")


async def pick_box(machine: RobotClient, box_center: Pose) -> None:
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


def print_plan() -> None:
    spot = get_pose("tape_spot")["tip_world"]
    box_center = Pose(x=spot["x"], y=spot["y"], z=TABLE_TOP_Z, theta=spot["theta"])
    print(f"box_center: x={box_center.x:.1f} y={box_center.y:.1f} z={box_center.z:.1f} (table top)")
    print(f"box top (real): z={TABLE_TOP_Z + BOX_H:.1f}")
    for label, h in (("approach", APPROACH_MM), ("contact", -PRESS_MM), ("lift", LIFT_MM)):
        t = tip_target(box_center, h)
        print(f"  {label:9s} commanded tip z={t.z:6.1f}  (real tip ~{h:+.0f} mm vs box top)")


async def main():
    global ASK_BEFORE_MOVING
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["plan", "find", "hover", "pick", "release"])
    parser.add_argument("--yes", action="store_true", help="don't ask before each motion")
    args = parser.parse_args()
    ASK_BEFORE_MOVING = not args.yes

    if args.action == "plan":
        print_plan()
        return

    async with await connect() as machine:
        if args.action == "release":
            await release(machine)
            return
        box_center = await find_box(machine)
        print(f"box_center: x={box_center.x:.1f} y={box_center.y:.1f} z={box_center.z:.1f}")
        if args.action == "hover":
            await hover_over_box(machine, box_center)
        elif args.action == "pick":
            await pick_box(machine, box_center)


if __name__ == "__main__":
    asyncio.run(main())
