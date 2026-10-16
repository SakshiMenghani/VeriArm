# VeriArm

**InspectAI — Voice-Powered Robotic Quality Inspection**

Viam Robot Hackathon "Code That Moves" (Oct 16–17, 2026).

The robot reads a package label with the arm camera, checks it against the expected inventory,
says the result out loud, and moves the box to ACCEPT / REJECT / REVIEW.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create `.env` (git-ignored) with the machine credentials from the Viam app's CONNECT tab:

```
VIAM_ADDRESS=<machine>.viam.cloud
VIAM_API_KEY=...
VIAM_API_KEY_ID=...
```

## Scripts

- `robot.py`: shared `connect()` helper that reads `.env`
- `connect_test.py`: read-only smoke test (lists resources, reads arm pose, saves camera frames to `captures/`)

## Machine resources

- `arm`, `gripper`, `cam` (color + depth), `builtin` motion service
- `table`, `wall-front`, `wall-side`, `ceiling`: obstacle geometry for the motion planner, not real grippers
