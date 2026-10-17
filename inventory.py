"""Inventory database + label format + verify().

Box statuses (one zone each): DETECTED = blue (incoming), ACCEPTED = green, REJECTED = orange.

Storage: SQLite file inventory.db (git-ignored), seeded from inventory_seed.csv (committed, edit in a spreadsheet).
Label format (what the QR code encodes):  VA1|<package id>|<recipient>   e.g.  VA1|PKG-001|Sakshi Menghani
  - "VA1" marks it as one of our labels (format version 1), so random QR codes are not mistaken for packages.
  - Keep recipient names short (<= 24 chars): longer text = denser QR = harder to read from the arm camera.
Each package is PAID or UNPAID; a PAID package also has a shipping speed, EXPRESS or STANDARD.
A package is ACCEPTED only if its id is in the inventory, the recipient on the label matches, and it's PAID.
Each package also has a sender_email (notified when the label's name matches) and a wrong_recipient_email
(notified when a label with this id has a different name). Results store payment PAID (accepted) / UNPAID
(rejected), shipping, and the sender_email that applies, so the notification knows whom to email.

  python inventory.py init              reload inventory.db from inventory_seed.csv (also happens
                                        automatically the first time, when the db is empty)
  python inventory.py list              show packages
  python inventory.py add PKG-007 "Recipient Name" [--unpaid | --shipping express]
  python inventory.py pay PKG-001 standard        mark a package PAID with EXPRESS / STANDARD shipping
  python inventory.py unpay PKG-003               mark a package UNPAID
  python inventory.py check "VA1|PKG-001|Sakshi Menghani"     run verify() on a label text
  python inventory.py scans             show the scan log
  python inventory.py next              the next scanned box waiting to be placed (zone + box_center)
  python inventory.py results           recent scan results and whether they were placed
  python inventory.py email PKG-001 someone@example.com [--wrong-recipient]   set a package's email
"""
import argparse
import csv
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

ROOT = Path(__file__).parent
DB_FILE = ROOT / "inventory.db"
SEED_FILE = ROOT / "inventory_seed.csv"

PREFIX = "VA1"
SEP = "|"
MAX_NAME_LEN = 24

PAID, UNPAID = "PAID", "UNPAID"
SHIPPING = ("EXPRESS", "STANDARD")  # only for PAID packages


# ---------- label format ----------

def make_label_text(package_id: str, recipient: str) -> str:
    if SEP in package_id or SEP in recipient:
        raise ValueError(f"'{SEP}' is not allowed in package ids or recipient names")
    if len(recipient) > MAX_NAME_LEN:
        raise ValueError(f"recipient name too long ({len(recipient)} > {MAX_NAME_LEN}): {recipient!r}")
    return SEP.join([PREFIX, package_id.strip(), recipient.strip()])


def parse_label_text(text: str) -> tuple[str, str] | None:
    """'VA1|PKG-001|Sakshi Menghani' -> ('PKG-001', 'Sakshi Menghani'); None if it isn't one of our labels."""
    parts = text.strip().split(SEP)
    if len(parts) != 3 or parts[0] != PREFIX or not parts[1]:
        return None
    return parts[1].strip(), parts[2].strip()


def same_name(a: str, b: str) -> bool:
    """Case- and spacing-insensitive name comparison."""
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


# ---------- database ----------

def connect_db(path: Path = DB_FILE) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS items (
            id        TEXT PRIMARY KEY,           -- package id
            recipient TEXT NOT NULL,              -- person the package is for
            payment   TEXT NOT NULL DEFAULT 'PAID', -- PAID / UNPAID (UNPAID packages are rejected)
            shipping  TEXT,                       -- EXPRESS / STANDARD for PAID packages, NULL when UNPAID
            sender_email TEXT,                    -- who to notify about this package (label name matches)
            wrong_recipient_email TEXT,           -- who to notify when a label for this id has the wrong name
            status    TEXT,                       -- last Status seen for this package, NULL = never scanned
            updated   TEXT
        );
        CREATE TABLE IF NOT EXISTS scans (
            ts        TEXT NOT NULL,
            status    TEXT NOT NULL,  -- DETECTED / ACCEPTED / REJECTED
            raw       TEXT,           -- exact QR text, NULL if no label found
            id        TEXT,
            recipient TEXT,
            reason    TEXT NOT NULL,
            face      TEXT
        );
        -- One row per scanned box: where it must go. Written by scan.py, read by the placing code.
        CREATE TABLE IF NOT EXISTS results (
            result_id  INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         TEXT NOT NULL,
            status     TEXT NOT NULL,     -- ACCEPTED / REJECTED
            zone       TEXT NOT NULL,     -- green / orange: where to place the box
            accepted   INTEGER NOT NULL,  -- 1 / 0: the bool for place_box(machine, verified)
            package_id TEXT,
            recipient  TEXT,
            reason     TEXT NOT NULL,
            label_face TEXT,
            box_x REAL, box_y REAL, box_z REAL, box_theta REAL,  -- box_center: world mm, z = box top, theta = box yaw
            drop_x REAL, drop_y REAL, drop_z REAL,               -- drop_center: where to put it (z = box top there)
            say        TEXT,
            placed_at  TEXT,              -- NULL until the box has been put in its zone
            payment    TEXT,              -- PAID (accepted) / UNPAID (rejected)
            sender_email TEXT,            -- who to notify (items.sender_email, or wrong_recipient_email), NULL if none
            shipping   TEXT               -- EXPRESS / STANDARD for accepted (paid) packages, else NULL
        );
        DROP TABLE IF EXISTS contacts;  -- emails used to live here; now they're columns of items
    """)
    cols = {r["name"] for r in db.execute("PRAGMA table_info(items)")}
    scan_cols = {r["name"] for r in db.execute("PRAGMA table_info(scans)")}
    result_cols = {r["name"] for r in db.execute("PRAGMA table_info(results)")}
    if not {"status", "recipient"} <= cols or not {"status", "recipient"} <= scan_cols:
        db.close()  # db from an older version: rebuild (it's local)
        path.unlink()
        return connect_db(path)
    if "payment" not in cols:  # items from before payment replaced expected (1/0)
        db.execute("ALTER TABLE items ADD COLUMN payment TEXT NOT NULL DEFAULT 'PAID'")
        db.execute("ALTER TABLE items ADD COLUMN shipping TEXT")
        db.execute("UPDATE items SET payment = CASE WHEN expected THEN 'PAID' ELSE 'UNPAID' END, "
                   "shipping = CASE WHEN expected THEN 'STANDARD' END")
        db.commit()
    if "sender_email" not in cols:  # items from before the email columns: add them, fill in from the seed
        db.execute("ALTER TABLE items ADD COLUMN sender_email TEXT")
        db.execute("ALTER TABLE items ADD COLUMN wrong_recipient_email TEXT")
        if SEED_FILE.exists():
            db.executemany("UPDATE items SET sender_email = ?, wrong_recipient_email = ? WHERE id = ?",
                           [(e1, e2, pid) for pid, _, _, _, e1, e2 in read_seed()])
        db.commit()
    for col in ("drop_x", "drop_y", "drop_z"):  # results table from before drop_center existed
        if col not in result_cols:
            db.execute(f"ALTER TABLE results ADD COLUMN {col} REAL")
    new_cols = [c for c in ("payment", "sender_email") if c not in result_cols]
    for col in new_cols:  # results table from before payment / sender_email existed: add and fill in old rows
        db.execute(f"ALTER TABLE results ADD COLUMN {col} TEXT")
    if new_cols:
        for row in db.execute("SELECT result_id, accepted, package_id, recipient FROM results").fetchall():
            db.execute("UPDATE results SET payment = ?, sender_email = ? WHERE result_id = ?",
                       (payment_for(bool(row["accepted"])), sender_email_for(row["package_id"], row["recipient"], db),
                        row["result_id"]))
        db.commit()
    if "shipping" not in result_cols:
        db.execute("ALTER TABLE results ADD COLUMN shipping TEXT")
    # inventory.db is not in git: on a fresh clone it starts empty, which would REJECT every label as
    # "Not in inventory". Load the committed seed automatically the first time.
    if db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0 and SEED_FILE.exists():
        n = init_from_seed(db)
        print(f"[inventory] {path.name} was empty: loaded {n} packages from {SEED_FILE.name}")
    return db


def read_seed(seed: Path = SEED_FILE) -> list[tuple]:
    """inventory_seed.csv rows as (id, recipient, payment, shipping, sender_email, wrong_recipient_email)."""
    with seed.open(newline="") as f:
        rows = [(r["id"].strip(), r["recipient"].strip(),
                 *payment_fields((r.get("payment") or PAID).strip().upper() == PAID, r.get("shipping")),
                 (r.get("sender_email") or "").strip() or None, (r.get("wrong_recipient_email") or "").strip() or None)
                for r in csv.DictReader(f)]
    for pid, recipient, *_ in rows:
        make_label_text(pid, recipient)  # validates
    return rows


def init_from_seed(db: sqlite3.Connection, seed: Path = SEED_FILE) -> int:
    rows = read_seed(seed)
    db.execute("DELETE FROM items")
    db.execute("DELETE FROM scans")
    db.executemany("INSERT INTO items (id, recipient, payment, shipping, sender_email, wrong_recipient_email) "
                   "VALUES (?, ?, ?, ?, ?, ?)", rows)
    db.commit()
    return len(rows)


def email_for(item: sqlite3.Row, label_recipient: str | None) -> str | None:
    """Whom to notify about a scanned label for this package: its sender_email when the name on the label
    matches, else its wrong_recipient_email."""
    if label_recipient and same_name(item["recipient"], label_recipient):
        return item["sender_email"]
    return item["wrong_recipient_email"]


def sender_email_for(package_id: str | None, label_recipient: str | None,
                     db: sqlite3.Connection | None = None) -> str | None:
    """email_for() by package id; None if the package isn't in the inventory."""
    if not package_id:
        return None
    db, own = _connect_or(db)
    try:
        item = get_item(db, package_id)
        return None if item is None else email_for(item, label_recipient)
    finally:
        if own:
            db.close()


def set_email(db: sqlite3.Connection, package_id: str, email: str | None, wrong_recipient: bool = False) -> None:
    """Set a package's sender_email (or wrong_recipient_email). email None clears it."""
    if get_item(db, package_id) is None:
        raise ValueError(f"{package_id} is not in the inventory")
    col = "wrong_recipient_email" if wrong_recipient else "sender_email"
    db.execute(f"UPDATE items SET {col} = ? WHERE id = ?", (email.strip() if email else None, package_id))
    db.commit()


def payment_for(accepted: bool) -> str:
    """ACCEPTED -> PAID, REJECTED -> UNPAID."""
    return "PAID" if accepted else "UNPAID"


def payment_fields(paid: bool, shipping: str | None = None) -> tuple[str, str | None]:
    """(payment, shipping) for the items table: PAID needs EXPRESS / STANDARD (default STANDARD), UNPAID has none."""
    if not paid:
        return UNPAID, None
    shipping = (shipping or "STANDARD").strip().upper()
    if shipping not in SHIPPING:
        raise ValueError(f"shipping must be EXPRESS or STANDARD, not {shipping!r}")
    return PAID, shipping


def add_item(db: sqlite3.Connection, package_id: str, recipient: str, paid: bool = True,
             shipping: str | None = None, sender_email: str | None = None,
             wrong_recipient_email: str | None = None) -> str:
    """Add or replace a package. Returns its label text."""
    text = make_label_text(package_id, recipient)
    db.execute("INSERT OR REPLACE INTO items (id, recipient, payment, shipping, sender_email, wrong_recipient_email) "
               "VALUES (?, ?, ?, ?, ?, ?)",
               (package_id.strip(), recipient.strip(), *payment_fields(paid, shipping),
                (sender_email or "").strip() or None, (wrong_recipient_email or "").strip() or None))
    db.commit()
    return text


def set_payment(db: sqlite3.Connection, package_id: str, paid: bool, shipping: str | None = None) -> None:
    """Mark a package PAID (with EXPRESS / STANDARD shipping) or UNPAID."""
    if get_item(db, package_id) is None:
        raise ValueError(f"{package_id} is not in the inventory")
    db.execute("UPDATE items SET payment = ?, shipping = ? WHERE id = ?", (*payment_fields(paid, shipping), package_id))
    db.commit()


def get_item(db: sqlite3.Connection, package_id: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM items WHERE id = ?", (package_id,)).fetchone()


def all_items(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM items ORDER BY id").fetchall()


# ---------- verify ----------

class Status(str, Enum):
    """Where a box is in the flow. Each status has its own zone on the table."""
    DETECTED = "DETECTED"   # found in the blue zone, not verified yet
    ACCEPTED = "ACCEPTED"   # our label, known id, recipient matches, PAID -> green zone
    REJECTED = "REJECTED"   # anything else (unknown, UNPAID, mismatch, no/unreadable label) -> orange zone


ZONE = {Status.DETECTED: "blue", Status.ACCEPTED: "green", Status.REJECTED: "orange"}


@dataclass
class Result:
    status: Status
    reason: str
    package_id: str | None = None
    recipient: str | None = None
    shipping: str | None = None  # EXPRESS / STANDARD when accepted (paid)
    sender_email: str | None = None  # whom to notify (see email_for), None if unknown

    @property
    def accepted(self) -> bool:
        """For place_box(verified: bool): True -> green zone, False -> orange zone."""
        return self.status is Status.ACCEPTED

    @property
    def zone(self) -> str:
        return ZONE[self.status]

    def say(self) -> str:
        """Short sentence for the voice announcement."""
        pkg = f"Package {self.package_id}" if self.package_id else "Package"
        if self.recipient:
            pkg += f" for {self.recipient}"
        return {Status.DETECTED: "Package detected. Checking label.",
                Status.ACCEPTED: f"{pkg} paid. Accepted for {(self.shipping or 'standard').lower()} shipping.",
                Status.REJECTED: f"{pkg} rejected. {self.reason}."}[self.status]


def _log(db: sqlite3.Connection, r: Result, raw: str | None, face: str | None) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    db.execute("INSERT INTO scans (ts, status, raw, id, recipient, reason, face) VALUES (?, ?, ?, ?, ?, ?, ?)",
               (now, r.status.value, raw, r.package_id, r.recipient, r.reason, face))
    item = get_item(db, r.package_id) if r.package_id else None
    # Only update the package when the label really identifies it (a wrong-recipient label could be a fake).
    if item is not None and r.recipient and same_name(item["recipient"], r.recipient):
        db.execute("UPDATE items SET status = ?, updated = ? WHERE id = ?", (r.status.value, now, r.package_id))
    db.commit()


def record_detected(db: sqlite3.Connection | None = None) -> Result:
    """Log that a box was found in the blue zone (label not read yet)."""
    own = db is None
    db = db or connect_db()
    try:
        r = Result(Status.DETECTED, "Box found in blue zone")
        _log(db, r, None, None)
        return r
    finally:
        if own:
            db.close()


def verify(label_text: str | None, db: sqlite3.Connection | None = None, face: str | None = None,
           no_label_reason: str = "No label found") -> Result:
    """Check a decoded QR text against the inventory: ACCEPTED or REJECTED (with reason). Logs the scan."""
    own = db is None
    db = db or connect_db()
    try:
        if label_text is None:
            r = Result(Status.REJECTED, no_label_reason)
        elif (parsed := parse_label_text(label_text)) is None:
            r = Result(Status.REJECTED, "Label is not a VeriArm code")
        else:
            pid, recipient = parsed
            item = get_item(db, pid)
            if item is None:
                r = Result(Status.REJECTED, "Not in inventory", pid, recipient)
            elif not same_name(item["recipient"], recipient):
                r = Result(Status.REJECTED, "Recipient does not match", pid, recipient,
                           sender_email=email_for(item, recipient))
            elif item["payment"] != PAID:
                r = Result(Status.REJECTED, "Not paid", pid, item["recipient"], sender_email=item["sender_email"])
            else:
                r = Result(Status.ACCEPTED, "Paid", pid, item["recipient"], item["shipping"], item["sender_email"])
        _log(db, r, label_text, face)
        return r
    finally:
        if own:
            db.close()


# ---------- results: handoff to the placing code ----------

def _connect_or(db):
    return (db, False) if db is not None else (connect_db(), True)


def save_result(result: dict, db: sqlite3.Connection | None = None) -> int:
    """Store a scan result (scan.py's dict). Returns its result_id."""
    db, own = _connect_or(db)
    try:
        c = result["box_center"]
        d = result.get("drop_center") or {}
        cur = db.execute(
            "INSERT INTO results (ts, status, zone, accepted, package_id, recipient, reason, label_face, "
            "box_x, box_y, box_z, box_theta, drop_x, drop_y, drop_z, say, payment, sender_email, shipping) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (result.get("time") or datetime.now().isoformat(timespec="seconds"), result["status"], result["zone"],
             1 if result["accepted"] else 0, result.get("package_id"), result.get("recipient"), result["reason"],
             result.get("label_face"), c["x"], c["y"], c["z"], c["theta"],
             d.get("x"), d.get("y"), d.get("z"), result.get("say"),
             result.get("payment") or payment_for(result["accepted"]),
             result.get("sender_email") or sender_email_for(result.get("package_id"), result.get("recipient"), db),
             result.get("shipping")))
        db.commit()
        return cur.lastrowid
    finally:
        if own:
            db.close()


def _result_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["accepted"] = bool(d["accepted"])
    d["box_center"] = {k: d.pop(f"box_{k}") for k in ("x", "y", "z", "theta")}
    d["drop_center"] = {k: d.pop(f"drop_{k}") for k in ("x", "y", "z")}
    return d


def next_to_place(db: sqlite3.Connection | None = None) -> dict | None:
    """The most recent scanned box that hasn't been placed yet, or None.
    Keys: result_id, zone ("green"/"orange"), accepted, box_center {x, y, z, theta}, drop_center {x, y, z},
    status, reason,
    package_id, recipient, label_face, say, ts. Older unplaced results are ignored (the box was rescanned)."""
    db, own = _connect_or(db)
    try:
        row = db.execute("SELECT * FROM results ORDER BY result_id DESC LIMIT 1").fetchone()
        return None if row is None or row["placed_at"] is not None else _result_row_to_dict(row)
    finally:
        if own:
            db.close()


def mark_placed(result_id: int, db: sqlite3.Connection | None = None) -> None:
    """Call after the box has been put down in its zone."""
    db, own = _connect_or(db)
    try:
        db.execute("UPDATE results SET placed_at = ? WHERE result_id = ?",
                   (datetime.now().isoformat(timespec="seconds"), result_id))
        db.commit()
    finally:
        if own:
            db.close()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("list")
    sub.add_parser("scans")
    sub.add_parser("next")
    sub.add_parser("results")
    em = sub.add_parser("email")
    em.add_argument("id")
    em.add_argument("email")
    em.add_argument("--wrong-recipient", action="store_true", help="set wrong_recipient_email instead")
    a = sub.add_parser("add")
    a.add_argument("id")
    a.add_argument("recipient")
    a.add_argument("--unpaid", action="store_true")
    a.add_argument("--shipping", choices=["express", "standard"], default="standard")
    pay = sub.add_parser("pay")
    pay.add_argument("id")
    pay.add_argument("shipping", choices=["express", "standard"])
    unpay = sub.add_parser("unpay")
    unpay.add_argument("id")
    c = sub.add_parser("check")
    c.add_argument("text")
    args = p.parse_args()

    db = connect_db()
    if args.cmd == "init":
        print(f"loaded {init_from_seed(db)} packages from {SEED_FILE.name} into {DB_FILE.name}")
    elif args.cmd == "list":
        for r in all_items(db):
            print(f"  {r['id']:10} {r['recipient']:{MAX_NAME_LEN}}  "
                  f"{r['payment']:6}  {r['shipping'] or '-':8}  status: {r['status'] or '-':9}  "
                  f"email: {r['sender_email'] or '-':28}  wrong-recipient email: {r['wrong_recipient_email'] or '-'}")
    elif args.cmd == "add":
        print("added", add_item(db, args.id, args.recipient, not args.unpaid, args.shipping))
    elif args.cmd == "pay":
        set_payment(db, args.id, True, args.shipping)
        print(f"{args.id}: PAID, {args.shipping.upper()} shipping")
    elif args.cmd == "unpay":
        set_payment(db, args.id, False)
        print(f"{args.id}: UNPAID")
    elif args.cmd == "check":
        r = verify(args.text, db)
        print(f"{r.status.value} -> {r.zone} zone: {r.reason}  (id={r.package_id}, recipient={r.recipient})")
        print("say:", r.say())
    elif args.cmd == "next":
        r = next_to_place(db)
        if r is None:
            print("Nothing waiting to be placed.")
        else:
            c = r["box_center"]
            print(f"result {r['result_id']}: {r['status']} -> place in {r['zone'].upper()} zone  ({r['reason']})")
            dc = r["drop_center"]
            print(f"  box_center:  x={c['x']} y={c['y']} z={c['z']} theta={c['theta']}   package={r['package_id']}")
            print(f"  drop_center: x={dc['x']} y={dc['y']} z={dc['z']}   (where to put it)")
    elif args.cmd == "results":
        for r in db.execute("SELECT * FROM results ORDER BY result_id DESC LIMIT 20"):
            print(f"  #{r['result_id']:<3} {r['ts']}  {r['status']:8} {r['payment'] or '-':6} {r['shipping'] or '-':8}"
                  f" -> {r['zone']:6}"
                  f"  {r['package_id'] or '-':10}  {r['sender_email'] or '-':28}"
                  f"  placed: {r['placed_at'] or 'NO'}   {r['reason']}")
    elif args.cmd == "email":
        set_email(db, args.id, args.email, args.wrong_recipient)
        print(f"{args.id}: {'wrong_recipient_email' if args.wrong_recipient else 'sender_email'} = {args.email}")
    elif args.cmd == "scans":
        for r in db.execute("SELECT * FROM scans ORDER BY rowid DESC LIMIT 30"):
            print(f"  {r['ts']}  {r['status']:8}  {r['id'] or '-':10} {r['recipient'] or '-':{MAX_NAME_LEN}}"
                  f"  {r['reason']}  face={r['face']}")


if __name__ == "__main__":
    main()
