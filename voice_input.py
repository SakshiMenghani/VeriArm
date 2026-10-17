"""Listening: VeriArm asks a question out loud, beeps, records the laptop mic until you stop talking,
and ElevenLabs (Scribe) turns it into text.

  python voice_input.py              ask "Say something", print what was heard (mic + ElevenLabs test)
  python voice_input.py --save F.wav also keep the recording

Needs ELEVENLABS_API_KEY in .env (Speech to Text permission). The first run asks macOS for microphone access.
Transcription is the same call as art_curator/listen.py (feature/art-curator branch).
"""
import argparse
import io
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import uuid
import wave

import numpy as np

import announce

SAMPLE_RATE = 16_000         # plenty for speech; small uploads
BLOCK_S = 0.05               # analyse the mic in 50 ms blocks
CALIBRATE_S = 0.4            # measure the room noise first
START_TIMEOUT_S = 7.0        # give up if nobody starts talking within this
END_SILENCE_S = 1.0          # stop after this much quiet once someone has spoken
MAX_RECORD_S = 10.0
SPEECH_FACTOR = 3.0          # speech = louder than room noise x this
MIN_SPEECH_RMS = 0.01
BEEP = "/System/Library/Sounds/Tink.aiff"

STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"
STT_MODEL = "scribe_v2"
STT_TIMEOUT_S = 30


class ListenError(RuntimeError):
    """Nothing usable was heard (silence, no key, API trouble)."""


def _beep() -> None:
    if os.path.exists(BEEP):
        subprocess.run(["afplay", BEEP], check=False)


def record_until_silence() -> bytes | None:
    """Record the default mic until the speaker goes quiet. Returns WAV bytes, or None if nobody spoke."""
    import sounddevice as sd  # here, not at the top: only needed when actually listening

    block = int(SAMPLE_RATE * BLOCK_S)
    chunks, started, quiet_s, waited_s = [], False, 0.0, 0.0
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=block) as stream:
        noise = [float(np.sqrt(np.mean(stream.read(block)[0] ** 2))) for _ in range(int(CALIBRATE_S / BLOCK_S))]
        threshold = max(MIN_SPEECH_RMS, float(np.median(noise)) * SPEECH_FACTOR)
        t0 = time.monotonic()
        while time.monotonic() - t0 < START_TIMEOUT_S + MAX_RECORD_S:
            data, _ = stream.read(block)
            loud = float(np.sqrt(np.mean(data ** 2))) > threshold
            if not started:
                waited_s += BLOCK_S
                chunks = (chunks + [data])[-6:]  # keep ~0.3 s before speech starts, so the first word isn't cut
                if loud:
                    started = True
                elif waited_s > START_TIMEOUT_S:
                    return None
                continue
            chunks.append(data)
            quiet_s = 0.0 if loud else quiet_s + BLOCK_S
            if quiet_s >= END_SILENCE_S or len(chunks) * BLOCK_S >= MAX_RECORD_S:
                break
    pcm = (np.clip(np.concatenate(chunks)[:, 0], -1, 1) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def transcribe(audio: bytes, mime: str = "audio/wav") -> str:
    """The words spoken in `audio`, or ListenError. Same request as art_curator/listen.py."""
    from robot import _load_env
    _load_env()
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not key:
        raise ListenError("no ELEVENLABS_API_KEY in .env")
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"model_id\"\r\n\r\n{STT_MODEL}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"tag_audio_events\"\r\n\r\nfalse\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"answer.wav\"\r\n"
            f"Content-Type: {mime}\r\n\r\n").encode() + audio + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(STT_URL, data=body, method="POST", headers={
        "xi-api-key": key, "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=STT_TIMEOUT_S) as resp:
            text = json.load(resp).get("text", "")
    except urllib.error.HTTPError as e:
        raise ListenError(f"ElevenLabs speech to text error {e.code}: {e.read().decode(errors='replace')[:200]}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise ListenError(f"can't reach ElevenLabs ({e})")
    return " ".join(text.split())


def ask(question: str, typed: bool = False, save: str | None = None) -> str:
    """Say `question`, then return the answer as text ("" if nothing was heard).
    typed=True: read the answer from the keyboard instead of the mic (quiet testing)."""
    announce.speak(question, wait=True)
    if typed:
        return input("  (type your answer) > ").strip()
    _beep()
    print("  [listening...]")
    audio = record_until_silence()
    if audio is None:
        print("  [heard nothing]")
        return ""
    if save:
        open(save, "wb").write(audio)
    try:
        text = transcribe(audio)
    except ListenError as e:
        print(f"  [listen] {e}")
        return ""
    print(f"  [heard] {text!r}")
    return text


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--save", help="also save the recording to this .wav file")
    args = p.parse_args()
    print("Heard:", repr(ask("Say something after the beep.", save=args.save)))
