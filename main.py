"""VeriArm main loop. By default: the voice CHECKOUT (checkout.py): VeriArm talks with you at the blue zone
(paid / unpaid / unidentified, payment, Standard or Express shipping, email notification), then moves
the box: Standard -> green zone, Express -> orange zone.

  python main.py                   checkout conversation, package after package ("another package?")
  python main.py --once            one package, then goodbye
  python main.py --max 5           stop after 5 packages
  python main.py --dry-run         one package: talk and scan, but never pick or place
  python main.py --type            type your answers instead of speaking them

  python main.py --classic         the previous sorting flow: verified -> green, not verified -> orange,
                                   continuously scanning (--once, --max, --dry-run, --poll-interval apply)

Classic flow:

Pieces (connect once here, pass `machine` to everyone):
  scan(machine)                       Sakshi  scan.py      -> result dict (see PLAN.md "Scan result"):
                                                  zone, accepted, box_center (where it is), drop_center (where it goes)
  pick_box(machine, box_center)       Yixin   find_pick.py
  place_box(machine, verified, drop)  place.py     carry to result["drop_center_pose"], set down, release
  notify_done(result)                 place.py     announce (prints result["say"] for now)

The drop routine returns the arm to overview-blue before the next scan. If no box is present, the
loop waits briefly and scans again. Stop with Ctrl+C. Keep someone at the e-stop.
"""
import argparse
import asyncio
import json

from viam.components.gripper import Gripper
from viam.robot.client import RobotClient

import announce
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
    printable = {k: v for k, v in result.items() if not k.endswith("_pose")}  # Pose objects aren't JSON
    print(json.dumps(printable, indent=2))
    print(f"\n-> {result['status']}: {result['zone'].upper()} zone. {result['reason']}.")


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

    # Re-check where the box is right before picking (the arm is back at overview after the scan; the
    # inspection could have nudged the box). Falls back to the scan's position if it can't see it.
    try:
        box_center = await find_pick.find_box(machine, move=False)
    except (LookupError, RuntimeError) as e:
        print(f"[main] couldn't re-check the box position ({e}); using the scan's position")
        box_center = result["box_center_pose"]
    print(f"[main] picking the box -> {result['zone']} zone at "
          f"x={result['drop_center']['x']} y={result['drop_center']['y']}")
    try:
        await find_pick.pick_box(machine, box_center)
    except find_pick.PickFailedError as e:
        print(f"[main] pick failed: {e}")
        await go_to_overview(machine)
        raise SystemExit("Pick failed.")

    await place_box(machine, result["accepted"], result["drop_center_pose"])
    mark_placed(result["result_id"])
    await notify_done(result)
    return result


async def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--confirm", action="store_true", help="ask before each robot motion")
    p.add_argument("--dry-run", action="store_true", help="scan + verify once, never pick or place")
    p.add_argument("--once", action="store_true", help="process one scan, then stop")
    p.add_argument("--loop", action="store_true", help="compatibility option; continuous scanning is the default")
    p.add_argument("--max", type=int, help="stop after placing this many boxes (default: no limit)")
    p.add_argument("--poll-interval", type=float, default=3.0,
                   help="classic: seconds to wait before retrying when the blue zone is empty (default: 3)")
    p.add_argument("--classic", action="store_true", help="the previous sorting flow (verified / not verified)")
    p.add_argument("--type", action="store_true", help="checkout: type answers instead of speaking them")
    args = p.parse_args()
    find_pick.ASK_BEFORE_MOVING = args.confirm

    if not args.classic:  # the voice checkout (checkout.py)
        import checkout
        announce.WORDING = "checkout"
        talk = argparse.Namespace(simulate=None, type=args.type, no_pick=args.dry_run,
                                  once=args.once or args.dry_run, max=args.max)
        async with await connect() as machine:
            await checkout.conversation(machine, talk)
        return

    done = []
    async with await connect() as machine:
        announce.intro()  # announcement 1 (see announce.py)
        while True:
            result = await handle_one_box(machine, args.dry_run)
            if result is None:
                print("[main] blue zone is empty; scanning again soon.")
                if args.once or args.dry_run:
                    break
                await asyncio.sleep(max(0.0, args.poll_interval))
                continue
            done.append(result)
            if args.dry_run or place_box is None:
                break  # the box did not move, so another scan would find it again
            if args.max is not None and len(done) >= args.max:
                print(f"[main] reached the limit of {args.max} placed box(es).")
                break
            if args.once:
                break
            print("[main] ready for the next package at overview-blue.")

    if len(done) > 1:
        print("\n[main] summary:")
        for r in done:
            print(f"  {r['package_id'] or '(no label)':10} {r['status']:8} -> {r['zone']}  {r['reason']}")


if __name__ == "__main__":
    asyncio.run(main())
