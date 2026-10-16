"""Save the arm's current position under a name. Read-only: never moves the robot.

Usage:
    python record_pose.py overview          # save current pose as "overview"
    python record_pose.py overview --force  # overwrite an existing "overview"
    python record_pose.py --list            # show saved poses
"""
import argparse
import asyncio

from viam.components.arm import Arm

from poses import capture_current_pose, load_poses, save_pose
from robot import ARM, connect

SETTLE_CHECK_SECONDS = 1.0
MAX_JOINT_DRIFT_DEG = 0.1


def print_poses(poses: dict) -> None:
    if not poses:
        print("No saved poses yet.")
        return
    for name, p in sorted(poses.items()):
        t = p["tip_world"]
        print(f"  {name:12s} tip x={t['x']:7.1f}  y={t['y']:7.1f}  z={t['z']:7.1f}   ({p['recorded_at']})")


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("name", nargs="?", help="pose name, e.g. overview, v_place, r_place")
    parser.add_argument("--force", action="store_true", help="overwrite an existing pose")
    parser.add_argument("--list", action="store_true", help="list saved poses and exit")
    args = parser.parse_args()

    if args.list or not args.name:
        print_poses(load_poses())
        return
    if args.name in load_poses() and not args.force:
        raise SystemExit(f"Pose {args.name!r} already exists. Use --force to overwrite.")

    async with await connect() as machine:
        arm = Arm.from_robot(machine, ARM)
        if await arm.is_moving():
            raise SystemExit("Arm is moving. Wait for it to stop, then try again.")

        first = await capture_current_pose(machine)
        await asyncio.sleep(SETTLE_CHECK_SECONDS)
        second = await capture_current_pose(machine)
        drift = max(abs(a - b) for a, b in zip(first["joints"], second["joints"]))
        if drift > MAX_JOINT_DRIFT_DEG:
            raise SystemExit(f"Arm moved {drift:.2f} deg during the check. Wait for it to settle.")

    save_pose(args.name, second)
    print(f"Saved {args.name!r}:")
    print_poses({args.name: second})


if __name__ == "__main__":
    asyncio.run(main())
