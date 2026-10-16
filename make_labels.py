"""Make printable QR labels from the inventory. Each QR encodes "VA1|<package id>|<recipient>"; both are printed below.

  python make_labels.py                     all inventory items -> labels/labels.pdf
  python make_labels.py PKG-001 PKG-003     only these ids
  python make_labels.py --test-cases        also add labels that must be REJECTED (unknown id, wrong recipient)
  python make_labels.py --size 35           QR side in mm (default 40; box sides are ~58 mm tall)

Print at 100% / "actual size" (not "fit to page") so the codes keep their size.
Every label is decoded back and run through verify() before saving, so a bad label never gets printed.
"""
import argparse
import io
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from inventory import all_items, connect_db, get_item, make_label_text, parse_label_text
from inspection import decode_qr

OUT_DIR = Path(__file__).parent / "labels"
DPI = 300
PAGE_MM = (210, 279)  # fits both A4 and US Letter
MM = DPI / 25.4

# Labels that should NOT be accepted, to test the other branches end to end.
TEST_CASES = [
    ("PKG-999", "Unknown Person"),   # not in inventory    -> REJECTED
    ("PKG-001", "Wrong Person"),     # recipient mismatch  -> REJECTED
]


def qr_image(text: str, side_px: int) -> np.ndarray:
    code = cv2.QRCodeEncoder.create().encode(text)  # 1 px per module, includes the quiet zone
    return cv2.resize(code, (side_px, side_px), interpolation=cv2.INTER_NEAREST)


def font(size_px: int) -> ImageFont.ImageFont:
    for name in ("Helvetica.ttc", "Arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size_px)
        except OSError:
            continue
    return ImageFont.load_default()


def make_label(pid: str, recipient: str, qr_mm: float) -> Image.Image:
    text = make_label_text(pid, recipient)
    q = int(qr_mm * MM)
    pad = int(3 * MM)
    big, small = font(int(5 * MM)), font(int(3.5 * MM))
    w, h = q + 2 * pad, q + pad + int(12 * MM)
    img = Image.new("L", (w, h), 255)
    img.paste(Image.fromarray(qr_image(text, q)), (pad, pad))
    d = ImageDraw.Draw(img)
    d.text((w / 2, q + pad + int(1 * MM)), pid, font=big, fill=0, anchor="ma")
    d.text((w / 2, q + pad + int(7 * MM)), f"To: {recipient}", font=small, fill=0, anchor="ma")
    d.rectangle([0, 0, w - 1, h - 1], outline=170, width=2)  # cut line
    return img


def check_label(img: Image.Image, pid: str, recipient: str) -> None:
    got = decode_qr(cv2.cvtColor(np.array(img), cv2.COLOR_GRAY2BGR))
    if got is None or parse_label_text(got) != (pid, recipient):
        raise SystemExit(f"Label self-check failed for {pid}: decoded {got!r}")


def layout(labels: list[Image.Image]) -> list[Image.Image]:
    pw, ph = int(PAGE_MM[0] * MM), int(PAGE_MM[1] * MM)
    margin, gap = int(12 * MM), int(6 * MM)
    pages, page, x, y, row_h = [], None, margin, margin, 0
    for lab in labels:
        if page is None:
            page, x, y, row_h = Image.new("L", (pw, ph), 255), margin, margin, 0
        if x + lab.width > pw - margin:
            x, y, row_h = margin, y + row_h + gap, 0
        if y + lab.height > ph - margin:
            pages.append(page)
            page, x, y, row_h = Image.new("L", (pw, ph), 255), margin, margin, 0
        page.paste(lab, (x, y))
        x += lab.width + gap
        row_h = max(row_h, lab.height)
    if page is not None:
        pages.append(page)
    return pages


def build_pdf(rows: list[tuple[str, str]], qr_mm: float = 40.0) -> bytes:
    """Labels for (id, recipient) rows -> PDF bytes. Each label is self-checked first."""
    labels = []
    for pid, recipient in rows:
        img = make_label(pid, recipient, qr_mm)
        check_label(img, pid, recipient)
        labels.append(img)
    pages = layout(labels)
    buf = io.BytesIO()
    pages[0].save(buf, format="PDF", save_all=True, append_images=pages[1:], resolution=DPI)
    return buf.getvalue()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("ids", nargs="*")
    p.add_argument("--test-cases", action="store_true")
    p.add_argument("--size", type=float, default=40.0, help="QR side in mm")
    p.add_argument("--out", default="labels.pdf")
    args = p.parse_args()

    db = connect_db()
    if args.ids:
        rows = []
        for pid in args.ids:
            r = get_item(db, pid)
            if r is None:
                raise SystemExit(f"{pid} is not in the inventory (python inventory.py list)")
            rows.append((r["id"], r["recipient"]))
    else:
        rows = [(r["id"], r["recipient"]) for r in all_items(db)]
        if not rows:
            raise SystemExit("Inventory is empty: run  python inventory.py init")
    if args.test_cases:
        rows += TEST_CASES

    for pid, recipient in rows:
        print(f"  {pid:10} {recipient:24} -> {make_label_text(pid, recipient)}")
    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / args.out
    out.write_bytes(build_pdf(rows, args.size))
    print(f"{len(rows)} labels -> {out}  (print at 100%)")


if __name__ == "__main__":
    main()
