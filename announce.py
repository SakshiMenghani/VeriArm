"""Spoken status announcements through the laptop speaker (the robot has no speaker).

Usage:
    python announce.py say "Hello from VeriArm"   # test the voice
    python announce.py watch                      # arm at overview-blue: announce when a package
                                                  # appears / disappears in the blue zone. Never moves the arm.
    python announce.py result                     # speak the latest scan result (runs/last_result.json)
    python announce.py follow                     # keep running; speak each new scan result as scan.py saves it

In code:
    from announce import speak
    speak("Package PKG-001 verified. Accepted.")
"""
import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from detect import NoBoxError, NotAtViewError, ZoneBlockedError, find_box_in_photo, live_images
from poses import get_pose
from robot import connect

VIEW = "overview-blue"
LAST_RESULT = Path(__file__).parent / "runs" / "last_result.json"  # written by scan.py
WATCH_INTERVAL_S = 3.0
SAME_RESULT_TWICE = True  # only announce a change after seeing it twice in a row (ignores one-frame glitches)


def speak(text: str, wait: bool = True) -> None:
    """Say `text` out loud on this computer. Also prints it, so it shows up in logs."""
    print(f"[say] {text}")
    if sys.platform == "darwin" and shutil.which("say"):
        cmd = ["say", text]
    elif shutil.which("spd-say"):
        cmd = ["spd-say", "--wait", text]
    elif shutil.which("espeak"):
        cmd = ["espeak", text]
    else:
        return  # no speech engine: printed only
    (subprocess.run if wait else subprocess.Popen)(cmd)


async def zone_status(machine) -> tuple[str, str]:
    """(status, sentence) for what's in the blue zone right now. No motion."""
    try:
        bgr, depth = await live_images(machine)
        box = find_box_in_photo(bgr, get_pose(VIEW), depth)
        return f"found:{box.name}", f"Package found: {box.name} box."
    except NoBoxError:
        return "empty", "No package in the zone."
    except ZoneBlockedError:
        return "blocked", "The zone is blocked."
    except NotAtViewError:
        return "not_at_view", "I can't see the zone. Move the arm to the overview position."


async def watch() -> None:
    async with await connect() as machine:
        speak("Watching the blue zone.")
        announced, pending = None, None
        while True:
            status, sentence = await zone_status(machine)
            if status != announced:
                if SAME_RESULT_TWICE and status != pending:
                    pending = status  # wait for one more look before announcing
                else:
                    speak(sentence)
                    announced, pending = status, None
            else:
                pending = None
            time.sleep(WATCH_INTERVAL_S)


def result_sentence(result: dict) -> str:
    """What to say for a scan result. scan.py already writes a sentence in result["say"]."""
    if result.get("say"):
        return result["say"]
    status = (result.get("status") or "unknown").lower()
    return f"Package {result.get('package_id') or 'without a readable label'} {status}. {result.get('reason') or ''}".strip()


def announce_result(path: Path = LAST_RESULT) -> None:
    if not path.exists():
        speak("No scan result yet.")
        return
    speak(result_sentence(json.loads(path.read_text())))


def follow(path: Path = LAST_RESULT, interval_s: float = 1.0) -> None:
    """Speak each new scan result as soon as it's saved. Results already there at start are skipped."""
    seen = path.stat().st_mtime if path.exists() else None
    print(f"Following {path} (Ctrl+C to stop)")
    while True:
        time.sleep(interval_s)
        if not path.exists():
            continue
        mtime = path.stat().st_mtime
        if mtime == seen:
            continue
        time.sleep(0.2)  # let scan.py finish writing
        try:
            result = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue  # half-written; try again next round
        seen = mtime
        speak(result_sentence(result))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("say").add_argument("text")
    sub.add_parser("watch")
    for name in ("result", "follow"):
        sub.add_parser(name).add_argument("--file", type=Path, default=LAST_RESULT,
                                          help="scan result JSON (default: runs/last_result.json)")
    args = parser.parse_args()
    try:
        if args.cmd == "say":
            speak(args.text)
        elif args.cmd == "result":
            announce_result(args.file)
        elif args.cmd == "follow":
            follow(args.file)
        else:
            asyncio.run(watch())
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
