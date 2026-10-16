"""VeriArm main loop: scan the box in the blue zone, pick it, place it in green/orange, announce.

  python main.py --dry-run     MOVES THE ARM (no pick): find + read label + verify, print where it would go
  python main.py               one box; asks before each pick/place motion
  python main.py --yes         one box, no questions (someone at the e-stop!)
  python main.py --yes --loop  keep going until the blue zone is empty (max --max boxes)

Pieces (connect once here, pass `machine` to everyone):
  scan(machine)                       Sakshi  scan.py      -> result dict (see PLAN.md "Scan result")
  pick_box(machine, box_center)       Yixin   find_pick.py
  place_box(machine, verified)        Yixin   place.py     (not written yet)
  notify_done(result)                 Yixin   place.py     (not written yet)

Safety: if place_box doesn't exist yet, main.py stops after scanning and never picks the box up
(the arm must never be left holding a box with nowhere to put it). If the pick doesn't hold the box,
it releases suction, goes back to overview and stops.
"""
import argparse
import asyncio
import json

from viam.components.gripper import Gripper
from viam.robot.client import RobotClient

import find_pick
from inspection import go_to_overview
from inventory import mark_placed
from robot import GRIPPER, connect
from scan import scan

try:  # Yixin's part. Until place.py exists, main.py runs scan-only.
    from place import place_box
except ImportError:
    place_box = None

try:
    from place import notify_done
except ImportError:
    async def notify_done(result: dict) -> None:
        """Placeholder until Yixin's notify_done exists: print the announcement."""
        print(f"[notify] {result['say']}")


def show(result: dict) -> None:
    printable = {k: v for k, v in result.items() if k != "box_center_pose"}
    print(json.dumps(printable, indent=2))
    print(f"\n-> {result['status']}: {result['zone'].upper()} zone. {result['reason']}.")


async def holding(machine: RobotClient) -> bool:
    return (await Gripper.from_robot(machine, GRIPPER).is_holding_something()).is_holding_something


async def handle_one_box(machine: RobotClient, dry_run: bool) -> dict | None:
    """Scan one box and (unless dry_run / place_box missing) move it to its zone.
    Returns the scan result, or None if the blue zone is empty."""
    result = await scan(machine)
    if result is None:
        return None
    show(result)

    if dry_run:
        print("[main] dry run: not picking the box up.")
        return result
    if place_box is None:
        print("[main] place_box isn't written yet (place.py), so the box stays in the blue zone. "
              "Stopping before the pick.")
        return result

    print(f"[main] picking the box -> {result['zone']} zone")
    await find_pick.pick_box(machine, result["box_center_pose"])
    if not await holding(machine):
        print("[main] the gripper is NOT holding the box after the pick. Releasing suction and stopping.")
        await Gripper.from_robot(machine, GRIPPER).open()
        await go_to_overview(machine)
        raise SystemExit("Pick failed.")

    await place_box(machine, result["accepted"])
    mark_placed(result["result_id"])
    await notify_done(result)
    return result


async def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--yes", action="store_true", help="don't ask before each motion")
    p.add_argument("--dry-run", action="store_true", help="scan + verify only, never pick")
    p.add_argument("--loop", action="store_true", help="repeat until the blue zone is empty")
    p.add_argument("--max", type=int, default=10, help="max boxes in --loop mode")
    args = p.parse_args()
    find_pick.ASK_BEFORE_MOVING = not args.yes

    done = []
    async with await connect() as machine:
        for _ in range(args.max if args.loop else 1):
            result = await handle_one_box(machine, args.dry_run)
            if result is None:
                print("[main] blue zone is empty.")
                break
            done.append(result)
            if args.dry_run or place_box is None:
                break  # the box didn't move: scanning again would find the same box

    if len(done) > 1:
        print("\n[main] summary:")
        for r in done:
            print(f"  {r['package_id'] or '(no label)':10} {r['status']:8} -> {r['zone']}  {r['reason']}")


if __name__ == "__main__":
    asyncio.run(main())
