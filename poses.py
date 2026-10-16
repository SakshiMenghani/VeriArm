"""Named arm poses shared by the team, stored in poses.json.

Each entry holds the arm's joint angles (exact, repeatable) plus the suction tip
and camera positions in the world frame (mm, degrees) for motion planning.
"""
import json
from datetime import datetime
from pathlib import Path

from viam.components.arm import Arm
from viam.components.camera import Camera
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


async def camera_axes(machine: RobotClient) -> dict:
    """The camera frame's x, y, z axes as unit vectors in the world frame.
    Computed by Viam's own transform, so no orientation-vector math on our side."""
    async def point(x, y, z):
        p = (await machine.transform_pose(
            PoseInFrame(reference_frame=CAMERA, pose=Pose(x=x, y=y, z=z, o_z=1)), "world")).pose
        return (p.x, p.y, p.z)

    origin = await point(0, 0, 0)
    axes = {}
    for name, xyz in (("x", (1000, 0, 0)), ("y", (0, 1000, 0)), ("z", (0, 0, 1000))):
        tip = await point(*xyz)
        axes[name] = [round((t - o) / 1000, 6) for t, o in zip(tip, origin)]
    return {"origin": [round(v, 3) for v in origin], **axes}


async def capture_current_pose(machine: RobotClient) -> dict:
    """Read where the arm is now. Read-only."""
    joints = await Arm.from_robot(machine, ARM).get_joint_positions()
    intr = (await Camera.from_robot(machine, CAMERA).get_properties()).intrinsic_parameters
    return {
        "joints": [round(v, 3) for v in joints.values],
        "tip_world": pose_to_dict(await _world_pose(machine, GRIPPER)),
        "camera_world": pose_to_dict(await _world_pose(machine, CAMERA)),
        "camera_axes": await camera_axes(machine),
        "camera_intrinsics": {"fx": intr.focal_x_px, "fy": intr.focal_y_px,
                              "cx": intr.center_x_px, "cy": intr.center_y_px,
                              "width": intr.width_px, "height": intr.height_px},
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
    }
