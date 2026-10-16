"""Named arm poses shared by the team, stored in poses.json.

Each entry holds the arm's joint angles (exact, repeatable) plus the suction tip
and camera positions in the world frame (mm, degrees) for motion planning.
"""
import json
from datetime import datetime
from pathlib import Path

from viam.components.arm import Arm
from viam.proto.common import Pose, PoseInFrame
from viam.robot.client import RobotClient

from robot import ARM, CAMERA, GRIPPER

POSES_FILE = Path(__file__).parent / "poses.json"
POSE_FIELDS = ("x", "y", "z", "o_x", "o_y", "o_z", "theta")


def pose_to_dict(pose: Pose) -> dict:
    return {f: round(getattr(pose, f), 3) for f in POSE_FIELDS}


def dict_to_pose(d: dict) -> Pose:
    return Pose(**{f: d[f] for f in POSE_FIELDS})


def load_poses() -> dict:
    if not POSES_FILE.exists():
        return {}
    return json.loads(POSES_FILE.read_text())


def get_pose(name: str) -> dict:
    """Look up a saved pose. Use dict_to_pose(entry["tip_world"]) or entry["joints"]."""
    poses = load_poses()
    if name not in poses:
        raise KeyError(f"No saved pose {name!r}. Saved poses: {sorted(poses)}")
    return poses[name]


def save_pose(name: str, entry: dict) -> None:
    poses = load_poses()
    poses[name] = entry
    POSES_FILE.write_text(json.dumps(poses, indent=2, sort_keys=True) + "\n")


async def _world_pose(machine: RobotClient, frame: str) -> Pose:
    result = await machine.transform_pose(PoseInFrame(reference_frame=frame, pose=Pose(o_z=1)), "world")
    return result.pose


async def capture_current_pose(machine: RobotClient) -> dict:
    """Read where the arm is now. Read-only."""
    joints = await Arm.from_robot(machine, ARM).get_joint_positions()
    return {
        "joints": [round(v, 3) for v in joints.values],
        "tip_world": pose_to_dict(await _world_pose(machine, GRIPPER)),
        "camera_world": pose_to_dict(await _world_pose(machine, CAMERA)),
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
    }
