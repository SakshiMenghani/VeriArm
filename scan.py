"""Find the box, read its label on the 5 visible faces, verify against the inventory.
This is the handoff to pick/place: the result says which zone the box goes to and where it is.

Status flow: DETECTED (box found in blue zone) -> ACCEPTED (green zone) or REJECTED (orange zone).

  python scan.py            MOVES THE ARM: find_box -> inspect 5 faces -> verify; prints + saves the result

In main.py (connect once, pass machine):
    result = await scan(machine)                    # dict, also saved to runs/last_result.json
    if result is not None:
        await pick_box(machine, result["box_center_pose"])   # Yixin
        await place_box(machine, result["accepted"])         # Yixin: True -> green, False -> orange
        await notify_done(result)                            # Yixin: result["say"] for voice

Result fields (JSON in runs/last_result.json; same dict in Python, plus "box_center_pose" / "drop_center_pose"):
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
    box_center    {x, y, z, theta}             world mm; z = TOP of the box; theta = box yaw (deg); same as find_box()
    drop_center   {x, y, z, theta}             where to put it: the zone's reach-checked drop point (zones.py),
                                               z = top of the box once it rests there; theta 0 = keep the pick wrist
    box           {length_mm, width_mm, height_mm, yaw_deg}
    say           sentence for the voice announcement
    inspection_dir  runs/<folder> with one photo per face + contact.jpg
    result_id     row in inventory.db "results"; inventory.next_to_place() returns the latest unplaced one,
                  inventory.mark_placed(result_id) after the box is put down
    time          ISO timestamp
"""
import argparse
import asyncio
import json
from datetime import datetime

from viam.robot.client import RobotClient

import announce
import find_pick
from inspection import LAST_INSPECTION, RUNS, inspect_box
from detect import NoBoxError, TABLE_TOP_Z
from inventory import payment_for, record_detected, save_result, sender_email_for, verify
from zones import drop_center
from robot import connect

LAST_RESULT = RUNS / "last_result.json"


async def scan(machine: RobotClient) -> dict | None:
    """Find + inspect + verify. Returns the result dict (see module docstring), or None if no box."""
    try:
        box_center = await find_pick.find_box(machine)
    except NoBoxError as e:
        print(f"No box: {e}")
        return None
    print(record_detected().say())

    announce.checking_label()  # announcement 2b (see announce.py)
    text = await inspect_box(machine, box_center)  # 5 visible faces, no picking up
    announce.label_read(text)  # announcement 3 (see announce.py)
    report = json.loads(LAST_INSPECTION.read_text())

    unreadable = [f["face"] for f in report["faces"] if f.get("qr_shapes") and not f.get("text")]
    no_label_reason = (f"Label seen on the {unreadable[0]} but could not be read" if unreadable
                       else "No label found")
    r = verify(text, face=report.get("found_on"), no_label_reason=no_label_reason)
    announce.verification(r.accepted, r.reason)  # announcement 4 (see announce.py)

    box = report["box"]
    # where the box goes: the saved, reach-checked drop point of its zone (zones.py); z = box top once it rests there
    drop = drop_center(r.zone, box_center.z - TABLE_TOP_Z)
    result = {
        "status": r.status.value,
        "zone": r.zone,
        "accepted": r.accepted,
        "reason": r.reason,
        "package_id": r.package_id,
        "recipient": r.recipient,
        "payment": payment_for(r.accepted),          # PAID / UNPAID
        "sender_email": sender_email_for(r.recipient),  # who to notify (contacts_seed.csv), None if unknown
        "label_text": text,
        "label_face": report.get("found_on"),
        "box_center": {"x": round(box_center.x, 1), "y": round(box_center.y, 1), "z": round(box_center.z, 1),
                       "theta": round(box_center.theta, 1)},
        "drop_center": {"x": round(drop.x, 1), "y": round(drop.y, 1), "z": round(drop.z, 1), "theta": 0.0},
        "box": {"length_mm": round(box["size_mm"][0], 1), "width_mm": round(box["size_mm"][1], 1),
                "height_mm": round(box["size_mm"][2], 1), "yaw_deg": round(box["yaw_deg"], 1)},
        "say": r.say(),
        "inspection_dir": f"runs/{report['dir']}",
        "time": datetime.now().isoformat(timespec="seconds"),
    }
    result["result_id"] = save_result(result)  # the placing code reads it with inventory.next_to_place()
    LAST_RESULT.write_text(json.dumps(result, indent=2) + "\n")
    return result | {"box_center_pose": box_center, "drop_center_pose": drop}


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--confirm", action="store_true", help="ask before each robot motion")
    args = p.parse_args()
    find_pick.ASK_BEFORE_MOVING = args.confirm
    async with await connect() as machine:
        result = await scan(machine)
    if result is None:
        return
    result.pop("box_center_pose")
    result.pop("drop_center_pose")
    print("\n" + json.dumps(result, indent=2))
    print(f"\n-> {result['status']}: put the box in the {result['zone'].upper()} zone. {result['reason']}.")


if __name__ == "__main__":
    asyncio.run(main())
