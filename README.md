# VeriArm

**InspectAI — Voice-Powered Robotic Quality Inspection**

Viam Robot Hackathon "Code That Moves" (Oct 16–17, 2026).

The robot reads a package label with the arm camera, checks it against the expected inventory,
says the result out loud, and moves the box to ACCEPT / REJECT / REVIEW.

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
- `poses.py`: load saved poses in code, e.g. `get_pose("v_place")["joints"]` or `dict_to_pose(get_pose("v_place")["tip_world"])`
- `find_pick.py`: `find_box()` (Sandy), `pick_box()` (Yixin, untested draft). `python find_pick.py find` finds the box (see below); `plan` prints pick targets without the robot; `hover`, `pick`, `release` move the arm and ask before each motion.
- `detect.py`: the vision code behind `find_box` (box detection by depth + size, `KNOWN_BOXES`, pixel → table). As a script it's for setup only: `--save-zone` re-records the blue zone (zone empty, arm at `overview-blue`); `--save-photo NAME` keeps a test photo + depth in `reference/`.

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
Box 'large': 290 x 177 x 56 mm (measured), yaw 93 deg
  box_center (world mm):  x=349.6  y=131.4  z=38.0  (z = top of the box: table -23 + 61 mm box height)
```

- **`box_center`**: center of the box's footprint, in mm, in the world frame (same as the arm's base frame). This is exactly what `find_box()` returns. **`z` is the top of the box** (where the suction cup goes): the table top (−23) plus the known height of the detected box.
- **Size and yaw**: measured from the camera, so they include the box's end flaps (a bit bigger than 272 x 170 x 61). `yaw` is the direction of the long side, 0-180 deg.
- To send the suction tip somewhere, remember the tip is **17 mm lower** than the robot thinks (see PLAN.md).

Each run also saves and opens a picture, `captures/detection.jpg`: **pink** = the blue zone, **yellow** = the detected box, **red cross** = the center it returns. Use it to check the detection by eye.

If it can't give an answer, it says why and does nothing:

| Message | Meaning | Fix |
|---|---|---|
| `NotAtViewError: Arm is N deg away from overview-blue` | the camera isn't where the coordinates assume | run without `--no-move` so it moves there first |
| `NoBoxError: no known box in the blue zone (saw objects: ...)` | nothing box-sized and box-tall in the zone | put the box fully (or nearly) inside the blue zone; "saw objects" lists what it rejected |
| `ZoneBlockedError: ...` | the camera can't see enough of the blue zone | clear hands, laptops, etc. off the zone |

## Machine resources

- `arm`, `vacuum_gripper-1`, `cam` (wrist camera, color + depth), `audio_in-1` (microphone), `builtin` motion service, `discovery-1`
- `table`, `wall-front`, `wall-side`, `ceiling`: obstacle geometry for the motion planner, not real grippers
