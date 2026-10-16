"""Small HTTP API for labels + inventory. Stdlib only (no extra installs).

  python label_api.py              serve on http://localhost:8000  (--port to change, --host 0.0.0.0 for the team)

Endpoints:
  GET  /                                  simple page: list items, add item, download labels
  GET  /qr?id=PKG-007&recipient=Jane%20Doe  QR PNG for any package id + recipient (VA1|id|recipient), not saved
  GET  /qr/PKG-001                        QR PNG for an inventory item
  GET  /label/PKG-001                     printable label PNG (QR + id + recipient text)
  GET  /labels.pdf                        all inventory labels;  ?ids=PKG-001,PKG-002  &size=40  &test_cases=1
  GET  /items                             inventory as JSON (with each item's status)
  POST /items      {"id","recipient","expected"?}   add/replace a package -> its label text + links
  POST /verify     {"text": "VA1|PKG-001|Sakshi Menghani"}   run verify() -> status JSON (logged like a real scan)
  GET  /scans                             last 50 scan log entries as JSON
"""
import argparse
import html
import io
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import cv2
from PIL import Image

from inventory import (MAX_NAME_LEN, add_item, all_items, connect_db, get_item, make_label_text, verify)
from make_labels import DPI, TEST_CASES, build_pdf, make_label, qr_image

QR_PX = 400  # PNG size for /qr


def png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", dpi=(DPI, DPI))
    return buf.getvalue()


def qr_png(pid: str, recipient: str, px: int = QR_PX) -> bytes:
    return png_bytes(Image.fromarray(qr_image(make_label_text(pid, recipient), px)))


INDEX = """<!doctype html><html><head><meta charset="utf-8"><title>VeriArm labels</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{font:15px system-ui;margin:24px;max-width:900px}table{border-collapse:collapse;width:100%}
td,th{border-bottom:1px solid #ddd;padding:6px;text-align:left}img{height:64px}
input{padding:4px}button{padding:5px 10px}</style></head><body>
<h2>VeriArm labels</h2>
<p><a href="/labels.pdf">Download all labels (PDF)</a> ·
<a href="/labels.pdf?test_cases=1">with reject test labels</a> · print at 100%</p>
<form id=f><b>Add package:</b> <input name=id placeholder="PKG-007" required>
<input name=recipient placeholder="Recipient name (max {MAX} chars)" maxlength={MAX} required>
<label><input type=checkbox name=unexpected> not expected</label> <button>Add</button></form>
<p id=msg></p>
<table><tr><th>QR</th><th>Package ID</th><th>Recipient</th><th>Expected</th><th>Status</th><th>Label</th></tr>{ROWS}</table>
<script>
f.onsubmit=async e=>{e.preventDefault();const d=new FormData(f);
const r=await fetch('/items',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({id:d.get('id'),recipient:d.get('recipient'),expected:!d.get('unexpected')})});
const j=await r.json(); if(r.ok) location.reload(); else msg.textContent=j.error;};
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str, filename: str | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        if filename:
            self.send_header("Content-Disposition", f'inline; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, indent=2).encode(), "application/json")

    def _error(self, msg: str, code: int = 400):
        self._json({"error": msg}, code)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        parts = [p for p in url.path.split("/") if p]
        db = connect_db()
        try:
            if not parts:
                rows = "".join(
                    f"<tr><td><img src='/qr/{quote(r['id'])}'></td><td>{r['id']}</td><td>{html.escape(r['recipient'])}</td>"
                    f"<td>{'yes' if r['expected'] else 'no'}</td><td>{r['status'] or '-'}</td>"
                    f"<td><a href='/label/{quote(r['id'])}'>PNG</a> · "
                    f"<a href='/labels.pdf?ids={quote(r['id'])}'>PDF</a></td></tr>" for r in all_items(db))
                return self._send(200, INDEX.replace("{MAX}", str(MAX_NAME_LEN)).replace("{ROWS}", rows).encode(), "text/html")

            if parts == ["qr"]:
                if not q.get("id") or not q.get("recipient"):
                    return self._error("need ?id=...&recipient=...")
                return self._send(200, qr_png(q["id"], q["recipient"], int(q.get("px", QR_PX))), "image/png")

            if len(parts) == 2 and parts[0] in ("qr", "label"):
                item = get_item(db, parts[1])
                if item is None:
                    return self._error(f"{parts[1]} not in inventory", 404)
                if parts[0] == "qr":
                    return self._send(200, qr_png(item["id"], item["recipient"]), "image/png")
                img = make_label(item["id"], item["recipient"], float(q.get("size", 40)))
                return self._send(200, png_bytes(img), "image/png", f"{item['id']}.png")

            if parts == ["labels.pdf"]:
                if q.get("ids"):
                    rows = []
                    for pid in q["ids"].split(","):
                        item = get_item(db, pid.strip())
                        if item is None:
                            return self._error(f"{pid} not in inventory", 404)
                        rows.append((item["id"], item["recipient"]))
                else:
                    rows = [(r["id"], r["recipient"]) for r in all_items(db)]
                if q.get("test_cases"):
                    rows += TEST_CASES
                if not rows:
                    return self._error("inventory is empty", 404)
                return self._send(200, build_pdf(rows, float(q.get("size", 40))), "application/pdf", "labels.pdf")

            if parts == ["items"]:
                return self._json([dict(r) | {"label": make_label_text(r["id"], r["recipient"])} for r in all_items(db)])

            if parts == ["scans"]:
                return self._json([dict(r) for r in db.execute("SELECT * FROM scans ORDER BY rowid DESC LIMIT 50")])

            return self._error("not found", 404)
        except ValueError as e:  # bad id/recipient for the label format
            return self._error(str(e))
        finally:
            db.close()

    def do_POST(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        body = self._body()
        db = connect_db()
        try:
            if parts == ["items"]:
                pid, recipient = str(body.get("id", "")).strip(), str(body.get("recipient", "")).strip()
                if not pid or not recipient:
                    return self._error("need id and recipient")
                expected = bool(body.get("expected", True))
                text = add_item(db, pid, recipient, expected)  # validates format + length
                return self._json({"id": pid, "recipient": recipient, "expected": expected, "label": text,
                                   "qr": f"/qr/{quote(pid)}", "label_png": f"/label/{quote(pid)}",
                                   "pdf": f"/labels.pdf?ids={quote(pid)}"}, 201)

            if parts == ["verify"]:
                r = verify(body.get("text"), db, face=body.get("face"))
                return self._json({"status": r.status.value, "zone": r.zone, "accepted": r.accepted,
                                   "reason": r.reason, "id": r.package_id, "recipient": r.recipient, "say": r.say()})

            return self._error("not found", 404)
        except (ValueError, sqlite3.Error) as e:
            return self._error(str(e))
        finally:
            db.close()

    def log_message(self, fmt, *args):
        print(f"[api] {self.address_string()} {fmt % args}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to let teammates on the same wifi use it")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()
    connect_db().close()  # make sure the db exists
    print(f"Label API on http://{args.host}:{args.port}/")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
