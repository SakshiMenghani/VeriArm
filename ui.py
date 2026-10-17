"""VeriArm control panel: a web page to start the robot and watch it work.

  python ui.py                  http://localhost:8080   (--host 0.0.0.0 to open it from another laptop)

Start runs the voice checkout (main.py) on THIS laptop: it must have .env, reach the robot, and the
customer talks to it through this laptop's microphone. Exactly like:
  checkout       python main.py            package after package ("Do you have another package?")
  one package    python main.py --once
  no pick        python main.py --dry-run  talk and scan one package, never pick it up
Stop ends the run and stops the arm where it is. It does NOT turn the suction off, so a box being
carried isn't dropped. The physical e-stop is still the real safety.

Also serves everything label_api.py does (labels page at /labels, /next, /results, /verify, ...).
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import label_api
from inventory import connect_db

ROOT = Path(__file__).parent
RUNS = ROOT / "runs"
LAST_RESULT = RUNS / "last_result.json"
NOISE = ("INFO", "GRPC proxy", "dial.py")  # SDK chatter, not useful in the panel

MODES = {
    "one": ["--once"],
    "loop": [],
    "scan": ["--dry-run"],
}


class Runner:
    """Runs main.py as a child process (one at a time) and keeps its output."""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.lines: list[str] = []
        self.state = "idle"  # idle | running | finished | failed | stopped
        self.mode = None
        self.started = self.ended = None
        self.exit_code = None

    def start(self, mode: str) -> tuple[bool, str]:
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return False, "already running"
            cmd = os.environ.get("VERIARM_UI_CMD")  # tests only: replace main.py with a fake command
            argv = cmd.split() if cmd else [sys.executable, "-u", str(ROOT / "main.py"), *MODES[mode]]
            self.proc = subprocess.Popen(argv, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, bufsize=1)
            self.lines, self.state, self.mode = [], "running", mode
            self.started, self.ended, self.exit_code = time.time(), None, None
            threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()
            return True, "started"

    def _read(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            line = line.rstrip()
            if line and not any(n in line for n in NOISE):
                with self.lock:
                    self.lines.append(line)
        code = proc.wait()
        with self.lock:
            if self.state == "running":
                self.state = "finished" if code == 0 else "failed"
            self.exit_code, self.ended = code, time.time()

    def stop(self) -> str:
        with self.lock:
            proc = self.proc
            if proc is None or proc.poll() is not None:
                return "not running"
            self.state = "stopped"
            self.lines.append("[ui] STOP pressed: ending the run and stopping the arm (suction left as is)")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        threading.Thread(target=self._stop_arm, daemon=True).start()
        return "stopping"

    def _stop_arm(self) -> None:
        async def go():
            from viam.components.arm import Arm
            from robot import ARM, connect
            async with await connect() as machine:
                await Arm.from_robot(machine, ARM).stop()
        try:
            asyncio.run(go())
            msg = "[ui] arm stopped"
        except Exception as e:  # robot unreachable etc.: the e-stop is the backup
            msg = f"[ui] couldn't send arm stop ({e}); use the e-stop if it's still moving"
        with self.lock:
            self.lines.append(msg)

    def status(self, since: int) -> dict:
        with self.lock:
            return {"state": self.state, "mode": self.mode, "started": self.started, "ended": self.ended,
                    "exit_code": self.exit_code, "total": len(self.lines), "lines": self.lines[since:]}


RUNNER = Runner()


def last_result() -> dict | None:
    """runs/last_result.json (written at scan time), updated with what checkout changed since in the database:
    payment, shipping, zone, email, placed."""
    if not LAST_RESULT.exists():
        return None
    r = json.loads(LAST_RESULT.read_text())
    if r.get("result_id"):
        db = connect_db()
        try:
            row = db.execute("SELECT * FROM results WHERE result_id = ?", (r["result_id"],)).fetchone()
        finally:
            db.close()
        if row is not None:
            r |= {k: row[k] for k in ("payment", "shipping", "zone", "sender_email", "reason", "placed_at")}
    folder = RUNS / Path(r.get("inspection_dir", "")).name
    r["images"] = sorted(f"/runs/{folder.name}/{p.name}" for p in folder.glob("*.jpg")
                         if p.name != "contact.jpg") if folder.is_dir() else []
    return r


PAGE = (ROOT / "ui.html")


class Handler(label_api.Handler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if path == "/labels":  # the label/inventory page that label_api serves at /
            self.path = "/"
            return super().do_GET()
        if path == "/run/status":
            since = int((urlparse(self.path).query.partition("since=")[2] or "0").split("&")[0] or 0)
            return self._json(RUNNER.status(since))
        if path == "/run/last":
            return self._json({"result": last_result()})
        if path == "/run/history":
            db = connect_db()
            try:
                rows = [dict(r) for r in db.execute("SELECT * FROM results ORDER BY result_id DESC LIMIT 15")]
            finally:
                db.close()
            return self._json(rows)
        if path.startswith("/runs/"):
            f = (ROOT / path.lstrip("/")).resolve()
            if RUNS.resolve() in f.parents and f.suffix == ".jpg" and f.is_file():
                return self._send(200, f.read_bytes(), "image/jpeg")
            return self._error("not found", 404)
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/run/start":
            mode = self._body().get("mode", "one")
            if mode not in MODES:
                return self._error(f"mode must be one of {sorted(MODES)}")
            ok, msg = RUNNER.start(mode)
            return self._json({"ok": ok, "message": msg}, 200 if ok else 409)
        if path == "/run/stop":
            return self._json({"message": RUNNER.stop()})
        return super().do_POST()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to open it from another laptop on the wifi")
    p.add_argument("--port", type=int, default=8080)
    args = p.parse_args()
    connect_db().close()
    print(f"VeriArm control panel: http://{'localhost' if args.host == '127.0.0.1' else args.host}:{args.port}/")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
