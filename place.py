"""place_box(): carry the picked box to its drop zone (green = ACCEPTED, orange = REJECTED) and let go.

The drop point comes from the scan result (result["drop_center_pose"], see zones.py): x, y = where the
box goes, z = top of the box once it rests there. Same convention as box_center, so the tip heights
use find_pick.tip_target and the same 17 mm suction correction as pick_box.

  python place.py --test-pick --yes   MOVES THE ARM: find the box, pick it, lift 3 cm, put it back, release
                                      (checks the suction + the holding sensor before any carrying)
In main.py:  await place_box(machine, result["accepted"], result["drop_center_pose"])
"""
import argparse
import asyncio

from viam.components.gripper import Gripper
from viam.proto.common import Pose
from viam.robot.client import RobotClient

import announce
import find_pick
from detect import TABLE_TOP_Z
from find_pick import APPROACH_MM, move_tip, tip_target
from inspection import go_to_overview
from robot import GRIPPER, connect

CARRY_CLEARANCE_MM = 100  # box bottom this far above the table while carrying (the planner can't see the box)
PUT_DOWN_MM = 10          # release with the box bottom this far above the table
SETTLE_S = 1.0


async def place_box(machine: RobotClient, verified: bool, drop_center: Pose | None = None) -> None:
    """Carry the held box to its zone's drop point, set it down, release, back off, return to overview.
    drop_center: from the scan result; if None, the zone's saved drop point is used (needs the box height,
    taken from the default demo box)."""
    zone = "green" if verified else "orange"
    if drop_center is None:
        from zones import drop_center as zone_drop
        drop_center = zone_drop(zone, find_pick.BOX_H)
    box_h = drop_center.z - TABLE_TOP_Z
    print(f"[place] carrying the box to the {zone} zone (x={drop_center.x:.0f} y={drop_center.y:.0f})")
    announce.moving_to(zone)  # announcement 5 (see announce.py)

    # tip_target(drop, h) puts the real tip h mm above where the box top will be when it rests there,
    # so the box bottom is h mm above the table.
    await move_tip(machine, tip_target(drop_center, CARRY_CLEARANCE_MM), f"carry above the {zone} zone")
    await move_tip(machine, tip_target(drop_center, PUT_DOWN_MM), "lower the box", straight_line=True)
    gripper = Gripper.from_robot(machine, GRIPPER)
    find_pick.confirm("turn suction OFF (box is just above the table)")
    await gripper.open()
    await asyncio.sleep(SETTLE_S)
    await move_tip(machine, tip_target(drop_center, APPROACH_MM), "back off", straight_line=True)
    await go_to_overview(machine)
    print(f"[place] box placed in the {zone} zone ({box_h:.0f} mm tall box)")


async def notify_done(result: dict) -> None:
    """Announce the result. Placeholder: print it (voice can call result["say"] here)."""
    print(f"[notify] {result['say']}")


async def test_pick(machine: RobotClient) -> None:
    """Pick the box, lift a little, put it back where it was, release. No carrying."""
    box_center = await find_pick.find_box(machine)
    gripper = Gripper.from_robot(machine, GRIPPER)
    try:
        await find_pick.pick_box(machine, box_center)
    except find_pick.PickFailedError as e:
        print(f"[test] PICK FAILED: {e}")
        await go_to_overview(machine)
        return
    print("[test] holding the box; putting it back")
    await move_tip(machine, tip_target(box_center, PUT_DOWN_MM), "lower it back", straight_line=True)
    await gripper.open()
    await asyncio.sleep(SETTLE_S)
    await move_tip(machine, tip_target(box_center, APPROACH_MM), "back off", straight_line=True)
    await go_to_overview(machine)
    print("[test] pick test OK: the suction holds and the holding sensor works")


async def test_lift(machine: RobotClient, lift_mm: float = 20.0) -> None:
    """Suction on, lift a little IGNORING the holding sensor, put it back, release. For checking whether
    the suction holds when the sensor says it doesn't. Watch the box."""
    box_center = await find_pick.find_box(machine)
    gripper = Gripper.from_robot(machine, GRIPPER)
    await gripper.open()
    await move_tip(machine, tip_target(box_center, APPROACH_MM), "approach above the box")
    contact = tip_target(box_center, -find_pick.PRESS_MM)
    await move_tip(machine, contact, "lower onto the box top", straight_line=True)
    print("[lift] suction ON:", await gripper.grab())
    await asyncio.sleep(2)
    print("[lift] sensor before lift:", (await gripper.is_holding_something()).is_holding_something)
    await move_tip(machine, tip_target(box_center, -find_pick.PRESS_MM + lift_mm), f"lift {lift_mm:.0f} mm",
                   straight_line=True)
    print(f"[lift] lifted {lift_mm:.0f} mm -- is the box off the table?")
    await asyncio.sleep(3)
    print("[lift] sensor while lifted:", (await gripper.is_holding_something()).is_holding_something)
    await move_tip(machine, contact, "lower back", straight_line=True)
    await gripper.open()
    await asyncio.sleep(SETTLE_S)
    await move_tip(machine, tip_target(box_center, APPROACH_MM), "back off", straight_line=True)
    await go_to_overview(machine)
    print("[lift] done; suction off, box back on its spot")


async def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--test-pick", action="store_true")
    p.add_argument("--test-lift", action="store_true", help="lift 2 cm ignoring the holding sensor, put back")
    p.add_argument("--yes", action="store_true", help="don't ask before each motion")
    args = p.parse_args()
    find_pick.ASK_BEFORE_MOVING = not args.yes
    if not (args.test_pick or args.test_lift):
        p.print_help()
        return
    async with await connect() as machine:
        await (test_lift(machine) if args.test_lift else test_pick(machine))


if __name__ == "__main__":
    asyncio.run(main())
