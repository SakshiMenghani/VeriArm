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

- `box_center`: a `Pose` in **mm**, in the **arm's base frame** (same as Viam's world frame). x, y = center of the box's footprint; **z = table top, i.e. the box's bottom** (−23 mm). The box top is `box_center.z + 61`.
- Function signatures:

  ```python
  async def find_box(machine) -> Pose                              # Sandy
  async def pick_box(machine, box_center: Pose) -> None            # Sandy
  async def inspect_box(machine, box_center: Pose) -> str | None   # Sakshi
  async def verify(package_id: str | None) -> bool                 # TBD (no robot needed)
  async def place_box(machine, verified: bool) -> None             # Yixin
  async def notify_done(package_id: str | None, verified: bool) -> None  # Yixin (no robot needed)
  ```

- `machine` is the live robot connection from `robot.connect()`. **Don't call `connect()` inside these functions.** `main.py` connects once and passes `machine` to each step:

  ```python
  async with await connect() as machine:
      box_center = await find_box(machine)
      ...
  ```

  One connection for the whole run, and the functions can move into a Viam module later without changes.

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

## Calibration notes (measured Oct 16)

- **Suction tip is 17 mm lower than the config thinks. Confirmed.** Measured with a ruler at two arm heights (config 158 → real 140 mm, config 167 → real 150 mm), then confirmed by the hover test: predicted 7.4 cm with the correction, measured 7.5 cm. Aim the tip **17 mm higher** in code whenever it approaches the box or table (`find_pick.SUCTION_Z_CORRECTION_MM`). The depth camera agrees with the configured table height within ~5 mm, so the cause is most likely the **gripper length** (config says 126 mm, real ≈ 143 mm). Fixing it in the gripper config would let us drop the correction; that's a team decision.
- **Move the suction tip, not the arm.** `arm.move_to_position()` positions the arm flange, which is 126 mm above the tip. To put the tip somewhere, use the motion service with the gripper (`robot.GRIPPER`) as the component to move. It also plans around the table and walls.
- Arm base = world origin, so "arm base frame" and "world frame" are the same. The camera is attached to the arm with a calibrated offset. Run `python check_frames.py` to see where everything is (read-only).
- **Blue zone:** a ~322 mm square, center at x = 348.6, y = 115.9 mm (world). Spans x 180–518, y −54–286. Its far edge is only ~4 cm from the front wall obstacle (x ≈ 558), so inspection poses on that side (the "back" pose at center + 25 cm in x) are **inside the wall**: use a shorter offset or look down at an angle.
- **Pixel → table works.** `detect.py` turns any pixel from the `overview-blue` photo into a table position. Hover test passed: the tip went to the computed blue zone center and was over the real center, within a centimetre.
- **Use the point cloud for depth, not the depth image.** The camera's `depth` image has its own (wider) field of view and doesn't line up with the color image; the point cloud has exactly one point per color pixel. `detect.live_images()` handles this.
- **Box:** 272 × 170 × 61 mm (10.7 × 6.7 × 2.4 in; the camera measures ~290 × 177 × 56 including the end flaps). It nearly fills the blue zone: only ~2.5 cm of blue on each side when centered, so it may hang over an edge. Box sizes live in `detect.KNOWN_BOXES`; we may add more sizes later.
- **Inspection poses need rethinking for this box:** a "front" pose 25 cm from the center is only ~11 cm from the long side, and `box_h/2` is ~3 cm above the table (still too low for the wrist camera).

## Saved poses (`poses.json`, record with `python record_pose.py <name>`)

| Name | What | Status |
|---|---|---|
| `overview-blue` | camera 526 mm above the table, looking straight down at the whole blue zone | recorded |
| `blue_zone` | the blue zone's position in the `overview-blue` photo and on the table (center x = 348.6, y = 115.9). Saved from a photo of the empty zone with `python detect.py --save-zone`; not an arm pose | recorded |
| `v_place` / `r_place` | above the green / orange zone, room for the 61 mm box underneath | **todo** (zones were moved; record at their new spots, and they must fit a 27 × 17 cm box) |

**Move to saved poses by joint angles**, not by tip pose: the planner can reach the same tip pose with a different arm posture (it picked one at a joint limit once, and the arm stopped with "Joint Angle Exceeds Limit").
