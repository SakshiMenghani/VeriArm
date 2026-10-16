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
- `find_pick.py`: `find_box()` / `pick_box()` (Sandy). Needs saved poses `overview-blue` and `tape_spot`. `python find_pick.py plan` prints targets without the robot; `find`, `hover`, `pick`, `release` move it and ask before each motion.

## Machine resources

- `arm`, `vacuum_gripper-1`, `cam` (wrist camera, color + depth), `audio_in-1` (microphone), `builtin` motion service, `discovery-1`
- `table`, `wall-front`, `wall-side`, `ceiling`: obstacle geometry for the motion planner, not real grippers
