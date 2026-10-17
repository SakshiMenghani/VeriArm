# VeriArm

**InspectAI — Voice-Powered Robotic Quality Inspection**

Viam Robot Hackathon "Code That Moves" (Oct 16–17, 2026).

The robot reads a package label with the arm camera, checks it against the expected inventory,
says the result out loud, and moves the box from the blue zone (DETECTED) to green (ACCEPTED) or orange (REJECTED).

## Setup

Needs **Python 3.10+** (we use 3.12). With [uv](https://docs.astral.sh/uv/):

```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

(Or `python3.12 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`.)

Create `.env` (git-ignored) with the machine credentials from the Viam app's CONNECT tab:

```
VIAM_ADDRESS=<machine>.viam.cloud
VIAM_API_KEY=...
VIAM_API_KEY_ID=...
```

## Scripts

- `robot.py`: shared `connect()` helper that reads `.env`, plus component name constants (`ARM`, `GRIPPER`, `CAMERA`, `MOTION`)
- `connect_test.py`: read-only smoke test (lists resources, reads arm pose, saves camera frames to `captures/`)
- `check_frames.py`: read-only frame system report (what each part is attached to, offsets, obstacles, where the camera and suction tip are now)
- `record_pose.py <name>`: save where the arm is now as a named pose in `poses.json` (read-only; refuses if the arm is moving). `--list` shows saved poses, `--force` overwrites.
- `inventory.py`: inventory database (SQLite, seeded from `inventory_seed.csv`) and `verify()`. Loads the seed automatically on first use; `python inventory.py init` reloads it after editing the CSV.
- `make_labels.py`: printable QR label PDF from the inventory → `labels/labels.pdf`
- `label_api.py`: HTTP API + web page for live QR generation (package id + recipient), adding packages, label PDFs, verify (http://localhost:8000)
- `zones.py`: the green/orange drop zones (measured once with the camera, saved in `poses.json` with a reach-checked drop point). `--show` prints them; `--survey` / `--check-drops` re-measure if a sheet moves.
- `ui.py`: **control panel** at http://localhost:8080: Start (one box / until the blue zone is empty / scan only), Stop, live progress + log, latest result with photos, history. Runs `main.py` on this laptop. Also serves the labels page at `/labels`.
- `main.py`: the whole cycle: scan → pick → place in green/orange → announce. `--dry-run` scans only; `--yes --loop` runs until the blue zone is empty. Stops before picking until Yixin's `place.py` exists.
- `scan.py`: `scan(machine)` = find → inspect 5 faces → verify; returns the result for pick/place and writes `runs/last_result.json` (local, not in git). Format examples: `examples/scan_result_*.json`.
- `inspection.py`: `inspect_box()` reads the QR label from the box's visible faces; `--plan`, `--decode` run without motion.
- `poses.py`: load saved poses in code, e.g. `get_pose("v_place")["joints"]` or `dict_to_pose(get_pose("v_place")["tip_world"])`
- `find_pick.py`: `find_box()` (Sandy), `pick_box()` (Yixin). `python find_pick.py find` finds the box (see below); `plan` prints pick targets without the robot; `hover`, `pick`, `release` move the arm and ask before each motion. `pick` opens suction, approaches and lowers the tip to the box top, grabs, reports the holding sensor status, and lifts regardless of that status.
- `detect.py`: the vision code behind `find_box` (box detection by depth + size, `KNOWN_BOXES`, pixel → table). As a script it's for setup only: `--save-zone` re-records the blue zone (zone empty, arm at `overview-blue`); `--save-photo NAME` keeps a test photo + depth in `reference/`.
- `announce.py`: VeriArm's voice, through the laptop speaker (the robot has none). `main.py` announces each step: intro, package detected / not, label read, verified / not, moving to the accepted / rejected area. The table at the top of `announce.py` lists every announcement and where it's called from. `python announce.py script` plays them all (no robot); `say "text"` tests the voice; `watch` announces what's in the blue zone (never moves the arm). Mute with `VERIARM_VOICE=0`.
- `checkout.py`: the sorting flow as a **voice conversation** (paid / unpaid). VeriArm asks, listens on the laptop mic (`voice_input.py`: ElevenLabs speech-to-text), and Claude maps the answer to a choice (`understand.py`). Unpaid → shipping option → card tap (say "done" for now) → payment accepted. Then the email notification question and "another package?". `--simulate paid|unpaid` runs it without the robot; `--type` to type answers. Needs `ANTHROPIC_API_KEY`, `ELEVENLABS_API_KEY`, `VERIARM_NOTIFY_EMAIL` in `.env`. VeriArm speaks with an ElevenLabs voice (cached per sentence in `captures/voice_cache/`; Mac voice as fallback, or `VERIARM_TTS=say`); pick another voice with `ELEVENLABS_VOICE_ID`.

## Where is the box? (`find`)

One command, three ways to run it:

```bash
python find_pick.py find                                    # moves the arm to overview-blue first (asks), then looks
python find_pick.py find --no-move                          # looks from where the arm is now (must be at overview-blue)
python find_pick.py find --image reference/box-center.jpg   # offline, from a saved photo (no robot needed)
```

In code it's the same function: `find_box(machine)` or `find_box(machine, move=False)`.

Example output:

```
Box 'large': top 287 x 175 mm (measured), height 61 mm (known, camera saw 56), yaw 93 deg
  box_center (world mm):  x=349.6  y=131.1  z=38.0  (z = top of the box: table -23 + 61 mm box height)
```

- **`box_center`**: center of the box's footprint, in mm, in the world frame (same as the arm's base frame). This is exactly what `find_box()` returns. **`z` is the top of the box** (where the suction cup goes): the table top (−23) plus the known height of the detected box. **`theta` is the box's yaw** (same as the printed yaw), not a wrist angle.
- **Size and yaw**: length/width are measured from the camera, so they include the box's end flaps (a bit bigger than 272 x 170). **Height is the known height** of the matched box (`KNOWN_BOXES`), not the camera's reading, which varies by ~5 mm; the camera's value is shown for reference (`measured_height_mm` in code). `yaw` is the direction of the long side, 0-180 deg.
- To send the suction tip somewhere, remember the tip is **17 mm lower** than the robot thinks (see PLAN.md).

Each run also saves and opens a picture, `captures/detection.jpg`: **pink** = the blue zone, **yellow** = the detected box, **red cross** = the center it returns. Use it to check the detection by eye.

**Boxes not in `KNOWN_BOXES`:** known boxes are always matched first. If nothing matches, a box-shaped, fully visible object (5–33 cm sides, 3–25 cm tall) is accepted as `unknown`, using the camera's **measured** size and height; the voice says "an unknown box, using measured size". The measured height can be ~5 mm off, and up to ~15 mm too high for tall boxes (the suction may stop short), so add boxes you use often to `KNOWN_BOXES` with ruler numbers. Strict mode, known boxes only: `VERIARM_UNKNOWN_BOXES=0`.

If it can't give an answer, it says why and does nothing:

| Message | Meaning | Fix |
|---|---|---|
| `NotAtViewError: Arm is N deg away from overview-blue` | the camera isn't where the coordinates assume | run without `--no-move` so it moves there first |
| `NoBoxError: no known box in the blue zone (saw objects: ...)` | nothing box-sized and box-tall in the zone | put the box fully (or nearly) inside the blue zone; "saw objects" lists what it rejected |
| `ZoneBlockedError: ...` | the camera can't see enough of the blue zone | clear hands, laptops, etc. off the zone |

## Machine resources

- `arm`, `vacuum_gripper-1`, `cam` (wrist camera, color + depth), `audio_in-1` (microphone), `builtin` motion service, `discovery-1`
- `table`, `wall-front`, `wall-side`, `ceiling`: obstacle geometry for the motion planner, not real grippers
