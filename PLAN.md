# VeriArm — Build Plan & Team Breakdown

The arm finds a package in the blue (incoming) area, finds and reads its QR label, checks it against the inventory database, and places it in the green (accepted) or orange (rejected) area.

## Setup

- **Arm:** `arm` with a vacuum gripper (`gripper`) and a wrist camera (`cam`, color + depth)
- **Motion:** `builtin` motion service, with `table` / wall / `ceiling` obstacles configured
- **Zones (colored areas on the table):**

  | Zone | Color | Box status |
  |---|---|---|
  | Q | Blue | DETECTED: incoming, box is placed here |
  | V | Green | ACCEPTED |
  | R | Orange | REJECTED |

- **Boxes:** one fixed size, neutral color (not blue, green, or orange). The suction cup needs a flat spot on top.
- **Labels:** QR code for the MVP. Print readable text next to it so we can switch to OCR later without reprinting. See "Labels & inventory" below.

## Main loop

1. Move to `overview` (fixed pose) above the blue zone and take a photo.
2. Find the box (`find_pick.find_box` / `detect.py`): a known box size standing above the blue zone. Gives center (`box_center`, z = box top), size, rotation. Status: DETECTED.
3. Build inspection views from the detected box: top (camera straight down) + 4 sides (camera looking down at each face at 25°, steeper if unreachable).
4. Visit each view (top first, then sides nearest the arm first), let the arm settle, grab a frame, try to decode a QR code. Skip views the arm can't reach or that would put the camera in the wall. Stop as soon as a code decodes. The box is passed to the planner as an obstacle.
5. If no code was found: the label is probably on the bottom. The camera is on the arm, so it can't see the bottom of a box the arm is holding. Instead, pick the box, set it back down tipped onto a side so the bottom faces sideways, and re-run step 4 once. (Open question: does the suction hold well enough to tip it?)
6. If there's still no code, the box is REJECTED ("No label found").
7. `verify(label_text)` checks the label against the inventory database: ACCEPTED or REJECTED with a reason (see below).
8. Pick and place: green zone (V) if ACCEPTED, orange zone (R) if REJECTED.
9. Send the "done" notification.

### Why "detect from a fixed pose"

The camera is on the arm, so converting a pixel to a table position only works if we know exactly where the camera was when the photo was taken. Always take detection photos from a saved, settled pose, never while moving.

### Inspection views

Computed from the detected box (center, size, rotation), so the box can be anywhere on the zone, at any angle. The camera (not the gripper) is the component moved, so the frame system handles the camera offset.

```
top  = 250 mm above the top face, camera straight down        (box_center.z is the box TOP)
side = 260 mm from the face center, camera looking down at it at 25° (then steeper/closer if unreachable)
```

Measured on Oct 16 with the real box: top and three sides are reachable at the first angle. The side facing the front wall (+x) is skipped because the camera would be inside the wall. The blue zone's far edge is only ~4 cm from the wall.

**Why one face at a time instead of a corner view showing 3 faces:** a 40 mm label reads reliably up to ~40° off-axis and up to ~400 mm away, and fails at 60° or from the overview height (650 mm). Each side view does show the top and parts of the neighboring faces too.

## Team breakdown

### Sandy — find the box
- `find_box(machine) -> box_center`: move to `overview-blue`, detect the box in the blue zone (by depth + size, `detect.KNOWN_BOXES`), return its position. Raises `NoBoxError` / `ZoneBlockedError` instead of guessing. **Done; live detection works.**
- `python find_pick.py find`: find the box from the terminal (`--no-move` to stay put, `--image` for a saved photo). Opens a picture of the detection.
- Shared setup: frame system check, 17 mm tip offset, saved poses, `blue_zone`. **Done.**
- **Test:** hover the gripper over the point `find_box` returns and check it's within a couple of cm of the real center. **Passed** (blue zone center).
- **Detection tests passed (live):** centered, shifted to a corner (hanging ~3 cm over an edge), rotated ~36° (yaw 57°) with corners over three edges, and hanging ~10 cm off the zone, which is correctly **refused** (`NoBoxError`, no position). Photos + depth in `reference/box-*.jpg/.npy` for offline re-tests: `python find_pick.py find --image reference/box-rotated.jpg`.

### Sakshi — inspect the box, labels, verification
- `inspect_box(machine, box_center) -> str | None` in `inspection.py`: photograph the 5 visible faces (top, then the sides going around the box), decode QR, stop at the first valid VeriArm label, return the raw label text. Gets the box's size/yaw from `detect.detect_box`. Every image + a report goes to `runs/inspect_<time>/` and `runs/last_inspection.json`.
- Labels & inventory: `inventory.py` (database + `verify`), `make_labels.py` (printable labels), `label_api.py` (live QR API). See below.
- `scan.py`: `scan(machine)` = find → inspect → verify; returns the handoff result for pick/place (see "Scan result" below).
- Bottom fallback / wall side: `inspection.py --turn` picks the box (`pick_box`), turns it 180° and re-reads. Untested.
- **Test:** `python inspection.py --decode <image>` / `--plan` (no motion), then `python scan.py --yes`.

### Yixin — pick, sort the box, and notify
- `pick_box(machine, box_center)`: work out the grip point, lower onto the top, turn on suction, lift. Starting point: the untested draft in `find_pick.py`. Inputs: `box_center` (x, y = box center; **z = top of the box**, so aim the suction cup at `box_center.z` directly), tip 17 mm lower than the config thinks, move the tip with the motion service (gripper as the component).
- `place_box(verified: bool)`: carry the picked box to V or R, lower it, release suction, return to a safe pose.
- `notify_done(result)`: send the "done" notification.
- **Test:** jog the arm, pick a box with a manual suction command, test both drops and the notification.

### main.py (Sakshi)
- Connects once, then: `scan(machine)` → `pick_box(machine, result["box_center_pose"])` → `place_box(machine, result["accepted"])` → `notify_done(result)`. `--loop` repeats until the blue zone is empty.
- **Yixin: put `place_box(machine, verified)` and `notify_done(result)` in `place.py`.** `main.py` imports them from there automatically. Until `place.py` exists, `main.py` scans and stops **before** picking (the arm never holds a box with nowhere to put it); `notify_done` falls back to printing `result["say"]`.
- If the gripper isn't holding the box after `pick_box`, it releases suction, goes back to overview and stops.
- `python main.py --dry-run` (scan only), `python main.py --yes` (one box), `python main.py --yes --loop`.

## Labels & inventory

- **Database:** SQLite `inventory.db` (git-ignored, local), created from `inventory_seed.csv` (committed; edit it in a spreadsheet, then `python inventory.py init`). Table `items(id, recipient, expected, status)`: one row per package, with the person it's for. Every status change (DETECTED / ACCEPTED / REJECTED) is logged in `scans`. A package's status only changes when its label matches both id and recipient.
- **QR content:** `VA1|<package id>|<recipient name>`, e.g. `VA1|PKG-001|Sakshi Menghani`. `VA1` marks our own labels (format v1). Recipient names ≤ 24 chars so the code stays small (21×21 modules) and readable from the arm.
- **Printing:** `python make_labels.py [--test-cases]` → `labels/labels.pdf`, 40 mm codes with the package id and "To: <recipient>" printed underneath (for OCR later). Print at 100%. Each label is decoded back before saving.
- **API (live QR generation):** `python label_api.py` → http://localhost:8000 (`--host 0.0.0.0` to share on the wifi). Web page to add packages and download labels, plus `GET /qr?id=..&recipient=..` (any QR in our format, real time), `GET /qr/<id>`, `GET /label/<id>`, `GET /labels.pdf?ids=..`, `GET /items`, `POST /items`, `POST /verify`, `GET /scans`. Stdlib only.
- **Statuses** (`record_detected()` when the box is found, then `verify(label_text) -> Result`):

  | Situation | Status | Zone |
  |---|---|---|
  | Box found in the incoming area, label not read yet | DETECTED | blue (Q) |
  | Our label, package id in inventory, recipient matches, expected | ACCEPTED | green (V) |
  | Id not in inventory, package marked not expected, recipient mismatch, not our format, or no label | REJECTED (+ reason) | orange (R) |

  `result.accepted` is the bool for `place_box(verified)`, `result.zone` is "green"/"orange", and `result.say()` is a sentence for the voice output (includes the reject reason).

## Scan result: handoff to pick / place (Sakshi → Sandy, Yixin)

`result = await scan(machine)` in `scan.py` (find → inspect the 5 visible faces → verify). Also saved as `runs/last_result.json`. `None` = no box in the blue zone.

```python
result = await scan(machine)
if result is not None:
    await pick_box(machine, result["box_center_pose"])   # Yixin; same Pose find_box() returns
    await place_box(machine, result["accepted"])         # True -> green, False -> orange
    await notify_done(result)                            # result["say"] for voice
```

| Field | Example | Use |
|---|---|---|
| `status` | `"ACCEPTED"` / `"REJECTED"` | |
| `zone` | `"green"` / `"orange"` | where the box goes |
| `accepted` | `true` / `false` | the bool for `place_box` |
| `reason` | `"Matches inventory"`, `"Not in inventory"`, `"Recipient does not match"`, `"Not expected"`, `"Label is not a VeriArm code"`, `"No label found"`, `"Label seen on the +x side but could not be read"` | announcement / log |
| `package_id`, `recipient` | `"PKG-001"`, `"Sakshi Menghani"` (null if no readable label) | announcement / log |
| `label_text`, `label_face` | `"VA1\|PKG-001\|Sakshi Menghani"`, `"-x side"` | debugging |
| `box_center` | `{x, y, z, theta}` world mm, z = box top (same as `find_box`) | for `pick_box` (Python also gets `box_center_pose` as a `Pose`) |
| `box` | `{length_mm, width_mm, height_mm, yaw_deg}` | |
| `say` | `"Package PKG-001 for Sakshi Menghani verified. Accepted."` | voice |
| `inspection_dir` | `runs/inspect_<time>` | one photo per face + `contact.jpg` |

Known limit: a label on the side facing the wall is seen only at a steep angle and usually can't be read (→ REJECTED, "could not be read"). `inspection.py --turn` (pick, turn 180°, re-read) exists but is untested.

## Interfaces (agree on these first)

- `box_center`: a `Pose` in **mm**, in the **arm's base frame** (same as Viam's world frame). x, y = center of the box; **z = the TOP of the box** (table top −23 + the detected box's known height; +38 mm for the current box). The table top is always z = −23. *(Changed Oct 16: z used to be the table top. If you wrote `box_center.z + 61`, drop the `+ 61`.)* **`box_center.theta` = the box's yaw** (direction of its long side, 0–180°), not a wrist angle. For the wrist rotation when moving the suction tip, `find_pick.tip_target(..., tip_theta=...)` defaults to the overview's wrist angle.
- Function signatures:

  ```python
  async def find_box(machine) -> Pose                              # Sandy
  async def pick_box(machine, box_center: Pose) -> None            # Yixin
  async def inspect_box(machine, box_center: Pose) -> str | None   # Sakshi (raw QR text; re-detects yaw/size via detect.py)
  def verify(label_text: str | None) -> Result                     # Sakshi (inventory.py; result.accepted is the bool)
  async def place_box(machine, verified: bool) -> None             # Yixin
  async def notify_done(result: Result) -> None                    # Yixin (no robot needed; result.say() for voice)
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
- **Camera measures the blue zone surface at z ≈ −29 mm** (config table top: −23). If the table really is ~6 mm lower than configured, the 17 mm tip error is mostly the gripper length (~20 mm longer than the configured 126 mm). Camera offset could also be slightly off, so confirm with a ruler.
- Arm base = world origin, so "arm base frame" and "world frame" are the same. The camera is attached to the arm with a calibrated offset. Run `python check_frames.py` to see where everything is (read-only).
- **Blue zone:** a ~322 mm square, center at x = 348.6, y = 115.9 mm (world). Spans x 180–518, y −54–286. Its far edge is only ~4 cm from the front wall obstacle (x ≈ 558), so inspection poses on that side (the "back" pose at center + 25 cm in x) are **inside the wall**: use a shorter offset or look down at an angle.
- **Pixel → table works.** `detect.py` turns any pixel from the `overview-blue` photo into a table position. Hover test passed: the tip went to the computed blue zone center and was over the real center, within a centimetre.
- **Box height: use the known height, not the measured one.** The depth camera's box height varies by ~5 mm between photos (56–61 mm for the same box). Detection uses the measurement only to recognize which box it is; `BoxDetection.height_mm` and `box_center.z` both use the known height from `KNOWN_BOXES`, so pick, inspection and place all agree. The raw reading is `BoxDetection.measured_height_mm`.
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
