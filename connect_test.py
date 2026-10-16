"""Read-only smoke test: connect, list resources, read arm pose, grab a camera frame.
Nothing here moves the robot."""
import asyncio
from pathlib import Path

from viam.components.arm import Arm
from viam.components.camera import Camera
from viam.components.gripper import Gripper

from robot import connect

CAPTURES = Path(__file__).parent / "captures"


async def main():
    async with await connect() as machine:
        print("Resources:")
        for name in sorted(machine.resource_names, key=lambda r: (r.subtype, r.name)):
            print(f"  {name.namespace}:{name.type}:{name.subtype}  {name.name}")

        arm = Arm.from_robot(machine, "arm")
        print("\narm end position:", await arm.get_end_position())
        print("arm joint positions:", await arm.get_joint_positions())

        gripper = Gripper.from_robot(machine, "gripper")
        print("gripper is_moving:", await gripper.is_moving())

        cam = Camera.from_robot(machine, "cam")
        images, _ = await cam.get_images()
        CAPTURES.mkdir(exist_ok=True)
        for i, img in enumerate(images):
            ext = img.mime_type.split("/")[-1].replace("jpeg", "jpg")
            out = CAPTURES / f"cam_{img.name or i}.{ext}"
            out.write_bytes(img.data)
            print(f"cam image: {img.name!r} {img.mime_type} {len(img.data)} bytes -> {out}")


if __name__ == "__main__":
    asyncio.run(main())
