"""Find the box in the blue zone (Sandy: find_box) and pick it up (Yixin: pick_box).

find_box() takes a live photo from overview-blue and finds the box anywhere in the blue zone.
If the zone is blocked or there's no box, it raises an error instead of guessing.

Every motion asks for confirmation unless --yes is passed. Keep someone at the e-stop.
`find` also saves and opens captures/detection.jpg: pink = blue zone, yellow = box, red cross = center.

Usage:
    python find_pick.py find                 # move to overview-blue, find the box, print box_center
    python find_pick.py find --no-move       # find the box from where the arm is now (must be overview-blue)
    python find_pick.py find --image reference/box-center.jpg   # offline, from a saved photo (no robot)
    python find_pick.py plan      # print the pick targets, no connection to the robot
    python find_pick.py hover     # put the suction tip 50 mm above the box top (hover test)
    python find_pick.py pick      # open suction, approach, lower, grab, verify, lift
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

from detect import KNOWN_BOXES, TABLE_TOP_Z, VIEW, BoxDetection, find_box_in_photo, live_images, save_debug_image
import announce
from poses import get_pose
from robot import ARM, GRIPPER, MOTION, connect

OVERVIEW = "overview-blue"

# The box we demo with (detect.KNOWN_BOXES). find_box() uses the height of whichever
# known box it detects; this is only for the offline `plan` printout.
BOX_L, BOX_W, BOX_H = KNOWN_BOXES["large"]

# The real suction tip is ~17 mm lower than the config thinks (measured twice by hand,
# see PLAN.md). Aim this much higher so the real tip lands where we mean.
SUCTION_Z_CORRECTION_MM = 17.0  # default, for box heights not measured below

# Measured per box with the hover test (tip commanded 30 / 50 mm above the box top, real gap by ruler):
#   large (61 mm): real gap 35 mm vs 30 -> tip 5 mm too high with 17 -> 12   (Oct 16, Sakshi)
#   tall (140 mm): real gap 45 mm vs 50 -> tip 5 mm too low  with 17 -> 22   (Oct 16, Sandy)
# Keyed by box height: box_center.z = table top + known box height (box_center_from).
SUCTION_Z_CORRECTION_BY_HEIGHT = {61.0: 12.0, 82.0: 15.0, 140.0: 22.0, 179.0: 27.0}  # 82, 179: estimated, not measured


def suction_correction(box_top_z: float) -> float:
    """Correction for the box whose top is at box_top_z (matched by known height within 5 mm)."""
    h = box_top_z - TABLE_TOP_Z
    for height, corr in SUCTION_Z_CORRECTION_BY_HEIGHT.items():
        if abs(h - height) <= 5:
            return corr
    return SUCTION_Z_CORRECTION_MM

APPROACH_MM = 50.0  # hover height above the box top before descending
PRESS_MM = 15.0     # press 15 mm past the top surface
LIFT_MM = 80.0      # how far to lift after grabbing
GRIP_SETTLE_S = 2.0 # wait after suction on before lifting
GRIP_CHECK_BLOCKS = False  # camera grip check only warns for now: it misjudges plain cardboard
GENTLE_LIFT_MM = 30.0   # lift the first part slowly, in small steps, so the seal holds as the weight comes on
GENTLE_STEP_MM = 5.0
GENTLE_PAUSE_S = 0.4

ASK_BEFORE_MOVING = True


def confirm(what: str) -> None:
    if ASK_BEFORE_MOVING and input(f"\nAbout to {what}. Press Enter to go, anything else to abort: ").strip():
        raise SystemExit("Aborted.")


def overview_tip_theta() -> float:
    """The suction tip's rotation (wrist roll) at overview-blue. Our default wrist rotation for picking."""
    return get_pose(OVERVIEW)["tip_world"]["theta"]


def tip_target(box_center: Pose, height_above_top: float, tip_theta: float | None = None) -> Pose:
    """Suction tip pointing straight down, `height_above_top` mm above the real box top (box_center.z).

    tip_theta is the wrist rotation; default = the overview's, so the wrist doesn't twist to follow
    the box (the suction cup is round). Note box_center.theta is the BOX's yaw, not a wrist angle."""
    return Pose(
        x=box_center.x,
        y=box_center.y,
        z=box_center.z + height_above_top + suction_correction(box_center.z),
        o_x=0, o_y=0, o_z=-1,
        theta=overview_tip_theta() if tip_theta is None else tip_theta,
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
    """box_center for a detection: x, y = box center, z = TOP of the box (table top + known box height),
    theta = the box's yaw (direction of its long side, 0-180 deg, world frame)."""
    top_z = TABLE_TOP_Z + box.height_mm  # known height (or measured, for an "unknown" box: see detect.py)
    return Pose(x=box.x, y=box.y, z=top_z, o_x=0, o_y=0, o_z=1, theta=box.yaw_deg)


async def find_box(machine: RobotClient, move: bool = True) -> Pose:
    """Return box_center: x, y = center of the box, z = TOP of the box, theta = box yaw (deg). mm, world frame.

    move=True: first move the arm to overview-blue. move=False: the arm must already be there.
    Raises NoBoxError / ZoneBlockedError (and NotAtViewError if move=False and the arm is elsewhere)
    instead of guessing. Saves captures/detection.jpg."""
    if move:
        # Use the saved joint angles, not the tip pose: the planner can reach the same tip pose
        # with a different arm posture (we saw it pick one at a joint limit), and detection
        # needs the camera exactly where it was when the pose was recorded.
        await Arm.from_robot(machine, ARM).move_to_joint_positions(JointPositions(values=get_pose(OVERVIEW)["joints"]))
    bgr, depth = await live_images(machine)
    try:
        box_center, box_name = _find_and_draw(bgr, depth)
    except LookupError as e:  # NoBoxError / ZoneBlockedError
        announce.no_package(e)  # announcement 2 (see announce.py)
        raise
    announce.package_detected(box_name)  # announcement 2 (see announce.py)
    return box_center


def _find_and_draw(bgr, depth) -> tuple[Pose, str]:
    """Detect the box in a photo from overview-blue, save the picture, return (box_center, box name)."""
    try:
        box = find_box_in_photo(bgr, get_pose(VIEW), depth)
    except LookupError as e:
        save_debug_image(bgr, None, f"{type(e).__name__}: {e}"[:90], open_it=False)
        raise
    save_debug_image(bgr, box, open_it=False)
    box_center = box_center_from(box)
    measured = f", camera saw {box.measured_height_mm:.0f}" if box.measured_height_mm is not None else ""
    kind = "MEASURED, unknown box" if box.name == "unknown" else f"known{measured}"
    print(f"Box '{box.name}': top {box.length_mm:.0f} x {box.width_mm:.0f} mm (measured), "
          f"height {box.height_mm:.0f} mm ({kind}), yaw {box.yaw_deg:.0f} deg")
    print(f"  box_center (world mm):  x={box_center.x:.1f}  y={box_center.y:.1f}  z={box_center.z:.1f}  "
          f"(z = top of the box: table {TABLE_TOP_Z:.0f} + {box.height_mm:.0f} mm box height)")
    return box_center, box.name


async def hover_over_box(machine: RobotClient, box_center: Pose) -> None:
    """Hover test from PLAN.md: tip should end up over the real box center, within a couple of cm."""
    await move_tip(machine, tip_target(box_center, APPROACH_MM), f"hover {APPROACH_MM:.0f} mm above the box")


class PickFailedError(RuntimeError):
    """The suction didn't hold the box. Suction is off and the tip has backed away."""


LABEL_MAX_OFFSET_MM = 60.0  # aim at the label only if it's within this of the box center (cup stays on the top)


async def grip_point(machine: RobotClient, box_center: Pose) -> Pose:
    """Where to put the suction cup: on the QR label if it's on the box top (smooth paper/tape seals;
    bare corrugated cardboard leaks), else the box center. Needs the arm at overview-blue (uses that
    photo); anywhere else it just returns box_center."""
    import cv2
    import numpy as np
    from detect import NotAtViewError, pixel_to_table
    try:
        bgr, _ = await live_images(machine)
    except NotAtViewError:
        return box_center
    ok, pts = cv2.QRCodeDetectorAruco().detectMulti(bgr)
    if not ok:
        print("[pick] no label visible on the top: aiming at the box center")
        return box_center
    view = get_pose(OVERVIEW)
    best = None
    for quad in pts:
        u, v = quad.mean(axis=0)
        x, y = pixel_to_table(u, v, view, box_center.z)  # the label lies on the box top
        d = float(np.hypot(x - box_center.x, y - box_center.y))
        if best is None or d < best[2]:
            best = (x, y, d)
    x, y, d = best
    if d > LABEL_MAX_OFFSET_MM:  # too close to the edge: move toward it, but keep the cup on the top
        k = LABEL_MAX_OFFSET_MM / d
        x, y = box_center.x + (x - box_center.x) * k, box_center.y + (y - box_center.y) * k
    print(f"[pick] aiming at the label: x={x:.1f} y={y:.1f} ({d:.0f} mm from the box center)")
    return Pose(x=x, y=y, z=box_center.z, o_x=0, o_y=0, o_z=1, theta=box_center.theta)


async def pick_box(machine: RobotClient, box_center: Pose) -> None:
    """Approach the box top, engage vacuum, lift it, and confirm the grip with the wrist camera.
    Raises PickFailedError (suction off, tip LIFT_MM above the box) if the box didn't come up,
    instead of carrying nothing to a zone."""
    gripper = Gripper.from_robot(machine, GRIPPER)
    box_center = await grip_point(machine, box_center)  # on the label if there is one on the top
    confirm("open the vacuum gripper (turn suction OFF)")
    await gripper.open()
    await move_tip(machine, tip_target(box_center, APPROACH_MM), "approach above the box")
    await move_tip(machine, tip_target(box_center, -PRESS_MM), "lower onto the box top", straight_line=True)
    confirm("turn suction ON")
    grabbed = await gripper.grab()
    print("grab() returned:", grabbed)
    await asyncio.sleep(GRIP_SETTLE_S)  # let the vacuum build
    # Grip check with the wrist camera (is_holding_something is not reliable on this vacuum gripper):
    # photo at the box top, lift straight up, photo again. Held -> the box rose with the camera.
    from grip_check import judge, wrist_photo
    before = await wrist_photo(machine)
    h = -PRESS_MM
    while h < GENTLE_LIFT_MM - PRESS_MM:  # slow start: small steps with pauses
        h = min(h + GENTLE_STEP_MM, GENTLE_LIFT_MM - PRESS_MM)
        await move_tip(machine, tip_target(box_center, h), f"lift slowly ({h + PRESS_MM:.0f} mm)", straight_line=True)
        await asyncio.sleep(GENTLE_PAUSE_S)
    await move_tip(machine, tip_target(box_center, LIFT_MM), "lift the box", straight_line=True)
    held, score = judge(before, await wrist_photo(machine))
    print(f"Grip check (camera): score {score:.2f} -> {'HELD' if held else 'NOT held'}  (captures/grip.jpg)")
    if not held and not GRIP_CHECK_BLOCKS:
        print("[pick] WARNING: camera check says not held; continuing anyway (GRIP_CHECK_BLOCKS = False)")
    if not held and GRIP_CHECK_BLOCKS:
        # put it back down first: if the check is wrong and the box IS held, it mustn't drop from a height
        await move_tip(machine, tip_target(box_center, 10.0), "lower back before letting go", straight_line=True)
        await gripper.open()
        raise PickFailedError(f"the box didn't come up with the gripper (camera similarity {score:.2f}); "
                              "is there a smooth patch in the middle of the box top?")


async def release(machine: RobotClient) -> None:
    confirm("turn suction OFF (the box will drop if lifted)")
    await Gripper.from_robot(machine, GRIPPER).open()


def _open_picture() -> None:
    import subprocess
    import sys
    from detect import DEBUG_IMAGE
    if not DEBUG_IMAGE.exists():
        return  # no photo was taken (e.g. the arm wasn't at overview-blue)
    opener = {"darwin": ["open"], "linux": ["xdg-open"]}.get(sys.platform)
    if opener:
        subprocess.run(opener + [str(DEBUG_IMAGE)], check=False)
    print(f"Picture: {DEBUG_IMAGE}  (pink = blue zone, yellow = box, red = center)")


def print_plan() -> None:
    zone = get_pose("blue_zone")["center_world"]
    box_center = Pose(x=zone["x"], y=zone["y"], z=TABLE_TOP_Z + BOX_H, o_z=1, theta=90)
    print(f"Example: box at the blue zone center x={box_center.x:.1f} y={box_center.y:.1f}, "
          f"box {BOX_L:.0f} x {BOX_W:.0f} x {BOX_H:.0f} mm")
    print(f"box_center.z = box top (real): z={box_center.z:.1f}")
    for label, h in (("approach", APPROACH_MM), ("contact", -PRESS_MM), ("lift", LIFT_MM)):
        t = tip_target(box_center, h)
        print(f"  {label:9s} commanded tip z={t.z:6.1f}  (real tip ~{h:+.0f} mm vs box top)")


async def main():
    global ASK_BEFORE_MOVING
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["find", "plan", "hover", "pick", "release"])
    parser.add_argument("--yes", action="store_true", help="don't ask before each motion")
    parser.add_argument("--no-move", action="store_true", help="find: don't move to overview-blue first")
    parser.add_argument("--image", help="find: use a saved photo from overview-blue (+ its .npy depth), no robot")
    args = parser.parse_args()
    ASK_BEFORE_MOVING = not args.yes

    if args.action == "plan":
        print_plan()
        return
    from detect import DEBUG_IMAGE
    DEBUG_IMAGE.unlink(missing_ok=True)  # only ever show a picture from this run
    if args.action == "find" and args.image:
        import cv2
        import numpy as np
        from pathlib import Path
        depth_file = Path(args.image).with_suffix(".npy")
        depth = np.load(depth_file) if depth_file.exists() else None
        try:
            _find_and_draw(cv2.imread(args.image), depth)
        except LookupError as e:
            raise SystemExit(f"{type(e).__name__}: {e}")
        finally:
            _open_picture()
        return

    async with await connect() as machine:
        if args.action == "release":
            await release(machine)
            return
        try:
            box_center = await find_box(machine, move=not args.no_move)
        except (LookupError, RuntimeError) as e:  # NoBoxError, ZoneBlockedError, NotAtViewError
            raise SystemExit(f"Not moving: {type(e).__name__}: {e}")
        finally:
            if args.action == "find":
                _open_picture()
        if args.action == "hover":
            await hover_over_box(machine, box_center)
        elif args.action == "pick":
            await pick_box(machine, box_center)


if __name__ == "__main__":
    asyncio.run(main())
