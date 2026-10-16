"""Spoken status announcements through the laptop speaker (the robot has no speaker).

Usage:
    python announce.py say "Hello from VeriArm"   # test the voice
    python announce.py watch                      # arm at overview-blue: announce when a package
                                                  # appears / disappears in the blue zone. Never moves the arm.

In code:
    from announce import speak
    speak("Package PKG-001 verified. Accepted.")
"""
import argparse
import asyncio
import shutil
import subprocess
import sys
import time

from detect import NoBoxError, NotAtViewError, ZoneBlockedError, find_box_in_photo, live_images
from poses import get_pose
from robot import connect

VIEW = "overview-blue"
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


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("say").add_argument("text")
    sub.add_parser("watch")
    args = parser.parse_args()
    if args.cmd == "say":
        speak(args.text)
    else:
        try:
            asyncio.run(watch())
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
