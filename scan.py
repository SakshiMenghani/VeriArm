"""Find the box, read its label on the 5 visible faces, verify against the inventory.
This is the handoff to pick/place: the result says which zone the box goes to and where it is.

Status flow: DETECTED (box found in blue zone) -> ACCEPTED (green zone) or REJECTED (orange zone).

  python scan.py --yes      MOVES THE ARM: find_box -> inspect 5 faces -> verify; prints + saves the result

In main.py (connect once, pass machine):
    result = await scan(machine)                    # dict, also saved to runs/last_result.json
    if result is not None:
        await pick_box(machine, result["box_center_pose"])   # Yixin
        await place_box(machine, result["accepted"])         # Yixin: True -> green, False -> orange
        await notify_done(result)                            # Yixin: result["say"] for voice

Result fields (JSON in runs/last_result.json; same dict in Python, plus "box_center_pose"):
    status        "ACCEPTED" | "REJECTED"
    zone          "green" | "orange"           where place_box should put it
    accepted      true | false                 the bool for place_box(machine, verified)
    reason        why, e.g. "Matches inventory", "Not in inventory", "Recipient does not match",
                  "Not expected", "Label is not a VeriArm code", "No label found",
                  "Label seen on the +x side but could not be read"
    package_id    "PKG-001" | null             from the QR (null if no readable label)
    recipient     "Sakshi Menghani" | null     from the inventory when accepted, else from the QR
    label_text    raw QR text, e.g. "VA1|PKG-001|Sakshi Menghani" | null
    label_face    "top" | "-x side" | ... | null   which face the label was read on
    box_center    {x, y, z, theta}             world mm; z = TOP of the box; same as find_box()
    box           {length_mm, width_mm, height_mm, yaw_deg}
    say           sentence for the voice announcement
    inspection_dir  runs/<folder> with one photo per face + contact.jpg
    time          ISO timestamp
"""
import argparse
import asyncio
import json
from datetime import datetime

from viam.robot.client import RobotClient

import find_pick
from inspection import LAST_INSPECTION, RUNS, inspect_box
from inventory import record_detected, verify
from robot import connect

LAST_RESULT = RUNS / "last_result.json"


async def scan(machine: RobotClient) -> dict | None:
    """Find + inspect + verify. Returns the result dict (see module docstring), or None if no box."""
    try:
        box_center = await find_pick.find_box(machine)
    except LookupError as e:  # ZoneBlockedError / NoBoxError
        print(f"No box: {e}")
        return None
    print(record_detected().say())

    text = await inspect_box(machine, box_center)  # 5 visible faces, no picking up
    report = json.loads(LAST_INSPECTION.read_text())

    unreadable = [f["face"] for f in report["faces"] if f.get("qr_shapes") and not f.get("text")]
    no_label_reason = (f"Label seen on the {unreadable[0]} but could not be read" if unreadable
                       else "No label found")
    r = verify(text, face=report.get("found_on"), no_label_reason=no_label_reason)

    box = report["box"]
    result = {
        "status": r.status.value,
        "zone": r.zone,
        "accepted": r.accepted,
        "reason": r.reason,
        "package_id": r.package_id,
        "recipient": r.recipient,
        "label_text": text,
        "label_face": report.get("found_on"),
        "box_center": {"x": round(box_center.x, 1), "y": round(box_center.y, 1), "z": round(box_center.z, 1),
                       "theta": round(box_center.theta, 1)},
        "box": {"length_mm": round(box["size_mm"][0], 1), "width_mm": round(box["size_mm"][1], 1),
                "height_mm": round(box["size_mm"][2], 1), "yaw_deg": round(box["yaw_deg"], 1)},
        "say": r.say(),
        "inspection_dir": f"runs/{report['dir']}",
        "time": datetime.now().isoformat(timespec="seconds"),
    }
    LAST_RESULT.write_text(json.dumps(result, indent=2) + "\n")
    return result | {"box_center_pose": box_center}


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--yes", action="store_true", help="don't ask before each move")
    find_pick.ASK_BEFORE_MOVING = not p.parse_args().yes
    async with await connect() as machine:
        result = await scan(machine)
    if result is None:
        return
    result.pop("box_center_pose")
    print("\n" + json.dumps(result, indent=2))
    print(f"\n-> {result['status']}: put the box in the {result['zone'].upper()} zone. {result['reason']}.")


if __name__ == "__main__":
    asyncio.run(main())
