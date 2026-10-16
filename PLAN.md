# VeriArm — Build Plan & Team Breakdown

The arm picks up a package from the sorting area, finds and reads its QR label, checks it with the server, and places it in the verified or unverified area.

## Setup

- **Arm:** `arm` with a vacuum gripper (`gripper`) and a wrist camera (`cam`, color + depth)
- **Motion:** `builtin` motion service, with `table` / wall / `ceiling` obstacles configured
- **Zones (colored areas on the table):**

  | Zone | Color | Meaning |
  |---|---|---|
  | Q | Blue | Sorting / incoming area |
  | V | Green | Verified |
  | R | Red | Unverified / rejected |

- **Boxes:** one fixed size, neutral color (not blue, green, or red). The suction cup needs a flat spot on top.
- **Labels:** QR code for the MVP. Print readable text next to it so we can switch to OCR later without reprinting.

## Main loop

1. Move to `overview` (fixed pose) above the blue zone and take a photo.
2. Find the box center. MVP: hardcoded to a taped target spot. Later: detected as the non-blue blob in the blue zone.
3. Build 5 inspection poses as `box_center + offset`: top, front, back, left, right.
4. Visit each pose, let the arm settle, grab a frame, try to decode a QR code. Skip any pose the arm can't reach. Stop as soon as a code decodes.
5. If no code was found: suction the top, lift, and show the bottom to the camera. Try once more.
6. If there's still no code, treat the box as unverified.
7. Send the ID to the server and get yes/no back.
8. Pick and place: green zone (V) if verified, red zone (R) if not.
9. Send the "done" notification.

### Why "detect from a fixed pose"

The camera is on the arm, so converting a pixel to a table position only works if we know exactly where the camera was when the photo was taken. Always take detection photos from a saved, settled pose, never while moving.

### Inspection poses

Because the box size is fixed, the inspection poses are constant offsets from the box center:

```
top   = center + (0, 0, box_h + ~20cm), camera facing down
front = center + (-~25cm, 0, box_h/2), camera facing +x
back  = center + (+~25cm, 0, box_h/2), camera facing -x
left / right = same idea on y
```

The code always computes poses as `box_center + offset`. For the first version, `box_center` is hardcoded to the tape spot. Switching to the detected location later means replacing one constant with a function call.

## Team breakdown

### Sandy — find and reach the box
- `find_box() -> box_center`: move to `overview`, detect the box in the blue zone, return its position. Start hardcoded, then real detection.
- `pick_box(box_center)`: lower onto the top, turn on suction, lift.
- Shared setup: Viam machine config, camera-to-gripper offset (frame system), saved poses.
- **Test:** hover the gripper over the point `find_box` returns and check it's within a couple of cm of the real center.

### Sakshi — inspect the box
- `inspect_box(box_center) -> id | None`: build the 5 poses, visit each, try to decode a QR code, skip unreachable poses.
- Bottom fallback: if nothing is found, call `pick_box`, show the bottom to the camera, try once more.
- **Test:** hardcode `box_center`, put a box on the tape spot, run `inspect_box`.

### Yixin — sort the box and notify
- `place_box(verified: bool)`: carry the picked box to V or R, lower it, release suction, return to a safe pose.
- `notify_done(result)`: send the "done" notification.
- **Test:** jog the arm, pick a box with a manual suction command, test both drops and the notification.

### Not assigned yet
- `verify(id) -> bool`: the server check. A mocked list of valid IDs is fine for the MVP.
- `main.py`: calls the pieces in order.

## Interfaces (agree on these first)

- `box_center`: x, y (and z if available) in **mm**, in the **arm's base frame**.
- Function signatures:

  ```python
  async def find_box() -> Pose                     # Sandy
  async def pick_box(box_center: Pose) -> None     # Sandy
  async def inspect_box(box_center: Pose) -> str | None   # Sakshi
  async def verify(package_id: str | None) -> bool # TBD
  async def place_box(verified: bool) -> None      # Yixin
  async def notify_done(package_id: str | None, verified: bool) -> None  # Yixin
  ```

## Build order

1. Hardcoded pick-and-place between saved poses (`overview`, V-place, R-place)
2. Inspection poses from a hardcoded center + QR decoding at each pose
3. Bottom-check fallback, server verification, notification
4. Swap in the detected box center
5. Stretch goals: handle box rotation on the table (OpenCV `minAreaRect`), OCR

## Risks to check early

- **Reachability:** can the arm reach the "back" inspection pose without hitting the box or table? Keep the blue zone well inside the arm's range.
- **Calibration:** errors in the pixel-to-table conversion carry into every inspection pose. Test with the hover check above.
- **Suction:** the cup may lose grip on a crinkly label or tape seam. Keep the top surface flat and clear.
