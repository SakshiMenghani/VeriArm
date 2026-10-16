"""Read-only frame system report: where the robot thinks each part is.
Nothing here moves the robot. Rerun after anyone changes the machine config."""
import asyncio

from google.protobuf.json_format import MessageToDict
from viam.components.camera import Camera
from viam.proto.common import Pose, PoseInFrame

from robot import ARM, CAMERA, GRIPPER, connect


def fmt(p: Pose) -> str:
    return (f"x={p.x:7.1f}  y={p.y:7.1f}  z={p.z:7.1f}  "
            f"dir=({p.o_x:.2f}, {p.o_y:.2f}, {p.o_z:.2f})  theta={p.theta:6.1f}")


def obstacle_box(kinematics) -> str:
    links = MessageToDict(kinematics).get("links", [])
    geom = links[0].get("geometry", {}) if links else {}
    if geom.get("type") != "box":
        return ""
    return f"box {geom['x']:.0f} x {geom['y']:.0f} x {geom['z']:.0f} mm"


def tcp_offset_mm(kinematics) -> float:
    """Distance from the arm flange to the gripper tip, from the gripper's kinematics."""
    links = MessageToDict(kinematics).get("links", [])
    return sum(abs(l.get("translation", {}).get("Z", 0.0)) for l in links)


async def main():
    async with await connect() as machine:
        frames = await machine.get_frame_system_config()

        print("== Frame system config ==")
        table_top = None
        for f in frames:
            name = f.frame.reference_frame
            parent = f.frame.pose_in_observer_frame.reference_frame
            print(f"\n{name}  (attached to: {parent})")
            print("  offset:", fmt(f.frame.pose_in_observer_frame.pose))
            if f.HasField("kinematics"):
                shape = obstacle_box(f.kinematics)
                if shape:
                    print("  shape: ", shape)
                    if name == "table":
                        thickness = MessageToDict(f.kinematics)["links"][0]["geometry"]["z"]
                        table_top = f.frame.pose_in_observer_frame.pose.z + thickness / 2
                if name == GRIPPER:
                    print(f"  tip is {tcp_offset_mm(f.kinematics):.0f} mm past the arm flange")

        print("\n== Where things are right now (world frame, mm) ==")
        for name in (ARM, GRIPPER, CAMERA):
            p = await machine.transform_pose(PoseInFrame(reference_frame=name, pose=Pose(o_z=1)), "world")
            label = {ARM: "arm flange", GRIPPER: "suction tip", CAMERA: "camera"}[name]
            print(f"  {label:12s} {fmt(p.pose)}")
            if table_top is not None and name in (GRIPPER, CAMERA):
                print(f"  {'':12s} -> {p.pose.z - table_top:.0f} mm above the table top")

        if table_top is not None:
            print(f"\n  table top surface at z = {table_top:.0f} mm")

        props = await Camera.from_robot(machine, CAMERA).get_properties()
        i = props.intrinsic_parameters
        print("\n== Camera ==")
        print(f"  {i.width_px}x{i.height_px}, focal=({i.focal_x_px:.0f}, {i.focal_y_px:.0f}) px, "
              f"center=({i.center_x_px:.0f}, {i.center_y_px:.0f}) px, point clouds: {props.supports_pcd}")


if __name__ == "__main__":
    asyncio.run(main())
