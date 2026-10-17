"""VeriArm's voice: spoken announcements through the laptop speaker (the robot has no speaker).

Speech runs in the background, one sentence after another, so the arm never waits for it.
Mute with the environment variable VERIARM_VOICE=0 (sentences are still printed).
Voice: ElevenLabs when ELEVENLABS_API_KEY is in .env (cached per sentence), else the Mac's `say`.

THE SCRIPT: every announcement VeriArm makes, and exactly where in the code it's triggered.
To change wording, edit the functions below; to move or remove an announcement, edit the call site.

  #  Moment                         Function                     Called from
  1  program starts                 intro()                      main.py      main(), right after connecting
  2  looked in the blue zone        package_detected(box_name)   find_pick.py find_box(), box found
                                    no_package(error)            find_pick.py find_box(), no box / zone blocked
  2b about to read the label       checking_label()             scan.py      scan(), right before inspect_box()
  3  QR code read (or not)          label_read(label_text)       scan.py      scan(), right after inspect_box()
  4  checked against the inventory  verification(accepted, why)  scan.py      scan(), right after verify()
                                    (silent in checkout.py, which says paid / unpaid / unidentified itself)
  5  box starts moving to its zone  moving_to(zone)              place.py     place_box(), before carrying the box

Command line (for testing):
    python announce.py say "Hello from VeriArm"   # test the voice
    python announce.py script                     # play all the announcements once (no robot)
    python announce.py watch                      # arm at overview-blue: announce when a package
                                                  # appears / disappears in the blue zone. Never moves the arm.
    python announce.py result                     # speak the latest scan result (runs/last_result.json)
    python announce.py follow                     # speak each new scan result as scan.py saves it.
                                                  # Not needed with main.py: it already speaks steps 1-5.
"""
import argparse
import asyncio
import atexit
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

VIEW = "overview-blue"
LAST_RESULT = Path(__file__).parent / "runs" / "last_result.json"  # written by scan.py
WATCH_INTERVAL_S = 3.0
SAME_RESULT_TWICE = True  # watch: only announce a change after seeing it twice in a row
REPEAT_WINDOW_S = 60.0    # don't repeat the same announcement within this many seconds
                          # (main.py calls find_box twice per box: the scan and the re-check before picking)


# ---------- the script (see the table at the top for where each one is called) ----------

def intro() -> None:
    """1. main.py main(): right after connecting to the robot."""
    speak("Hello, this is VeriArm. I'll be here to help you sort packages whenever you are ready.")


def package_detected(box_name: str, recheck: bool = False) -> None:
    """2. find_pick.find_box(): a box was found in the blue zone. recheck=True (the look right before
    picking) says nothing: it's an internal check, and the package was already announced."""
    if recheck:
        return
    text = ("Package detected: an unknown box, using measured size." if box_name == "unknown"
            else f"Package detected: the {box_name} box.")
    _last_said["zone"] = (text, time.monotonic())
    speak(text)


def no_package(error: Exception | None = None) -> None:
    """2. find_pick.find_box(): no box in the blue zone, or the camera can't see the zone."""
    from detect import ZoneBlockedError  # here, not at the top: keeps `announce` light to import
    if isinstance(error, ZoneBlockedError):
        speak_once("I can't see the blue zone clearly. Please clear it.", key="zone")
    else:
        speak_once("No package detected.", key="zone")


def checking_label() -> None:
    """2b. scan.scan(): right before inspect_box(). Fills the quiet while the camera moves to the label and
    reads it. Plays in the background so the arm doesn't wait for it."""
    speak("Checking the label.", wait=False)


def label_read(label_text: str | None) -> None:
    """3. scan.scan(): right after inspect_box() returns the QR text (None if nothing was read)."""
    from inventory import parse_label_text
    if not label_text:
        speak("I couldn't read a label on this package.")
    elif parsed := parse_label_text(label_text):
        package_id, recipient = parsed
        speak(f"Label read: package {spell_id(package_id)}, for {recipient}.")
    else:
        speak("I read a label, but it isn't a VeriArm label.")


# Wording for announcements 4 and 5. "verified" = the sorting demo (main.py); "checkout" = the checkout
# conversation (checkout.py sets it): it says paid / unpaid / unidentified itself, and the zones are the
# shipping areas (green = Standard, orange = Express).
WORDING = "verified"
SHIPPING_AREA = {"green": "Standard", "orange": "Express"}


def verification(accepted: bool, reason: str = "") -> None:
    """4. scan.scan(): right after verify(). Silent in the checkout (checkout.py says paid / unpaid itself)."""
    if WORDING == "checkout":
        return
    speak("Package verified." if accepted else f"Package not verified. {reason}.".replace("..", "."))


def moving_to(zone: str) -> None:
    """5. place.place_box(): right before carrying the box. zone is "green" or "orange"."""
    if WORDING == "checkout":
        speak(f"Moving the package to the {SHIPPING_AREA[zone]} shipping area.")
    else:
        area = "accepted" if zone == "green" else "rejected"
        speak(f"Moving the package to the {area} area.")


def spell_id(package_id: str) -> str:
    """'PKG-001' -> 'P K G 0 0 1', so the voice reads it letter by letter."""
    return " ".join(ch for ch in package_id if ch.isalnum())


# ---------- speech engine ----------

_queue: "queue.Queue[str | None]" = queue.Queue()
_worker: threading.Thread | None = None
_last_said: dict[str, tuple[str, float]] = {}


def _speech_command(text: str) -> list[str] | None:
    if sys.platform == "darwin" and shutil.which("say"):
        return ["say", text]
    if shutil.which("spd-say"):
        return ["spd-say", "--wait", text]
    if shutil.which("espeak"):
        return ["espeak", text]
    return None


# ---------- ElevenLabs voice (same approach as art_curator/narrate.py) ----------
# Used when ELEVENLABS_API_KEY is in .env; any sentence it can't deliver goes to the Mac voice instead.
# Clips are cached by text, so repeated sentences (intro, questions) play instantly. VERIARM_TTS=say
# forces the Mac voice. Optional .env: ELEVENLABS_VOICE_ID, ELEVENLABS_MODEL.
VOICE_CACHE = Path(__file__).parent / "captures" / "voice_cache"
DEFAULT_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"  # "George": warm (ElevenLabs default library)
DEFAULT_TTS_MODEL = "eleven_multilingual_v2"
TTS_TIMEOUT_S = 20.0

_eleven: dict | None = None   # {"key", "voice", "model"} once configured; {} = not available
_pool = None                  # background generation of clips


def _elevenlabs() -> dict:
    global _eleven
    if _eleven is None:
        _eleven = {}
        if os.environ.get("VERIARM_TTS", "").lower() != "say" and shutil.which("afplay"):
            from robot import _load_env
            _load_env()
            key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
            if key:
                _eleven = {"key": key,
                           "voice": os.environ.get("ELEVENLABS_VOICE_ID", "").strip() or DEFAULT_VOICE_ID,
                           "model": os.environ.get("ELEVENLABS_MODEL", "").strip() or DEFAULT_TTS_MODEL}
    return _eleven


def _clip(text: str) -> Path:
    """mp3 of `text` in the ElevenLabs voice, from the cache or freshly generated. Raises on failure."""
    import hashlib
    import urllib.request
    cfg = _elevenlabs()
    path = VOICE_CACHE / (hashlib.sha1(f"{cfg['voice']}|{cfg['model']}|{text}".encode()).hexdigest() + ".mp3")
    if path.exists():
        return path
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{cfg['voice']}?output_format=mp3_44100_128",
        data=json.dumps({"text": text, "model_id": cfg["model"]}).encode(),
        headers={"xi-api-key": cfg["key"], "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=TTS_TIMEOUT_S) as resp:
        audio = resp.read()
    VOICE_CACHE.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(audio)
    tmp.replace(path)
    return path


def prepare(texts: list[str]) -> None:
    """Generate these sentences in the background now, so they play without delay later."""
    global _pool
    if os.environ.get("VERIARM_VOICE", "1") == "0" or not _elevenlabs():
        return
    if _pool is None:
        from concurrent.futures import ThreadPoolExecutor
        _pool = ThreadPoolExecutor(max_workers=4)
    for text in texts:
        _pool.submit(_clip, text)


def _say_one(text: str) -> None:
    global _eleven
    if _elevenlabs():
        try:
            subprocess.run(["afplay", str(_clip(text))], check=False)
            return
        except Exception as e:  # network, quota, timeout: this sentence gets the Mac voice
            if getattr(e, "code", None) in (401, 402, 403):  # bad key / out of credits: stop trying
                _eleven = {}
                print(f"[voice] ElevenLabs refused ({e}); Mac voice from now on")
            else:
                print(f"[voice] ElevenLabs failed ({type(e).__name__}: {e}); Mac voice for this sentence")
    cmd = _speech_command(text)
    if cmd:
        subprocess.run(cmd, check=False)


def _run_worker() -> None:
    while (text := _queue.get()) is not None:
        _say_one(text)
        _queue.task_done()
    _queue.task_done()


def _wait_until_spoken() -> None:
    """At exit: let queued sentences finish instead of cutting them off."""
    if _worker is not None:
        _queue.join()


# Finish each sentence before the robot continues: speaking in the background while the robot code is
# busy (point clouds, QR decoding, motion planning) made the audio stutter and sentences overlap the motion.
# VERIARM_VOICE_WAIT=0 goes back to background speech.
WAIT_FOR_SPEECH = os.environ.get("VERIARM_VOICE_WAIT", "1") != "0"


def speak(text: str, wait: bool | None = None) -> None:
    """Say `text` out loud, in order. Always printed too. Blocks until said when wait (default
    WAIT_FOR_SPEECH)."""
    if wait is None:
        wait = WAIT_FOR_SPEECH
    global _worker
    print(f"[say] {text}")
    if os.environ.get("VERIARM_VOICE", "1") == "0":
        return
    if _worker is None:
        _worker = threading.Thread(target=_run_worker, daemon=True)
        _worker.start()
        atexit.register(_wait_until_spoken)
    _queue.put(text)
    if wait:
        _queue.join()


def speak_once(text: str, key: str) -> None:
    """speak(), unless the same sentence for this key was said in the last REPEAT_WINDOW_S seconds."""
    last = _last_said.get(key)
    if last and last[0] == text and time.monotonic() - last[1] < REPEAT_WINDOW_S:
        return
    _last_said[key] = (text, time.monotonic())
    speak(text)


# ---------- command line ----------

async def zone_status(machine) -> tuple[str, str]:
    """(status, sentence) for what's in the blue zone right now. No motion."""
    from detect import NoBoxError, NotAtViewError, ZoneBlockedError, find_box_in_photo, live_images
    from poses import get_pose
    try:
        bgr, depth = await live_images(machine)
        box = find_box_in_photo(bgr, get_pose(VIEW), depth)
        return f"found:{box.name}", f"Package detected: the {box.name} box."
    except NoBoxError:
        return "empty", "No package detected."
    except ZoneBlockedError:
        return "blocked", "I can't see the blue zone clearly. Please clear it."
    except NotAtViewError:
        return "not_at_view", "I can't see the zone. Move the arm to the overview position."


async def watch() -> None:
    from robot import connect
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


def play_script() -> None:
    """Every announcement once, in flow order (no robot)."""
    intro()
    package_detected("large")
    package_detected("unknown")
    checking_label()
    label_read("VA1|PKG-001|Sakshi Menghani")
    verification(True)
    moving_to("green")
    no_package()
    label_read("VA1|PKG-999|Unknown Person")
    verification(False, "Not in inventory")
    moving_to("orange")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("say").add_argument("text")
    sub.add_parser("script")
    sub.add_parser("watch")
    for name in ("result", "follow"):
        sub.add_parser(name).add_argument("--file", type=Path, default=LAST_RESULT,
                                          help="scan result JSON (default: runs/last_result.json)")
    args = parser.parse_args()
    try:
        if args.cmd == "say":
            speak(args.text)
        elif args.cmd == "script":
            play_script()
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
