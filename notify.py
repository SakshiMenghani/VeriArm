"""Email notifications for checked-out packages (SMTP, e.g. Gmail).

Settings in .env:
  SMTP_USER=veriarm.demo@gmail.com     account that sends the mail
  SMTP_PASSWORD=abcd efgh ijkl mnop    for Gmail: an App Password (Google account -> Security ->
                                       2-Step Verification -> App passwords), not the normal password
  SMTP_HOST=smtp.gmail.com             optional (default smtp.gmail.com)
  SMTP_PORT=587                        optional (default 587, STARTTLS)
  SMTP_FROM=VeriArm <veriarm.demo@gmail.com>   optional (default SMTP_USER)

  python notify.py someone@example.com        send a test email
"""
import os
import smtplib
import sys
from datetime import datetime
from email.message import EmailMessage

from robot import _load_env

TIMEOUT_S = 15.0
SHIPPING_TIME = {"STANDARD": "3 to 5 days", "EXPRESS": "1 to 2 days"}


class NotifyError(RuntimeError):
    """The email could not be sent (not configured, refused, network)."""


def configured() -> bool:
    _load_env()
    return bool(os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASSWORD"))


def package_email(to: str, package_id: str | None, recipient: str | None, shipping: str,
                  paid_now: bool = False) -> EmailMessage:
    """The checkout notification for one package."""
    shipping = shipping.upper()
    pkg = package_id or "your package"
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = f"VeriArm: {pkg} is on its way ({shipping.title()} shipping)"
    lines = [
        f"Hello{' ' + recipient if recipient else ''},",
        "",
        f"Your package {pkg} was checked in by VeriArm at {datetime.now():%Y-%m-%d %H:%M}.",
        "",
        f"  Package:   {pkg}",
        f"  Recipient: {recipient or '-'}",
        f"  Payment:   {'Paid at checkout' if paid_now else 'Paid'}",
        f"  Shipping:  {shipping.title()} ({SHIPPING_TIME.get(shipping, '')})",
        "",
        "It has been moved to the shipping area and will go out with the next pickup.",
        "",
        "VeriArm",
    ]
    msg.set_content("\n".join(lines))
    return msg


def send(msg: EmailMessage) -> None:
    """Send via SMTP (STARTTLS). Raises NotifyError."""
    _load_env()
    user, password = os.environ.get("SMTP_USER", "").strip(), os.environ.get("SMTP_PASSWORD", "").strip()
    if not user or not password:
        raise NotifyError("SMTP_USER / SMTP_PASSWORD are not set in .env")
    if "From" not in msg:
        msg["From"] = os.environ.get("SMTP_FROM", "").strip() or user
    host = os.environ.get("SMTP_HOST", "").strip() or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT", "").strip() or 587)
    try:
        with smtplib.SMTP(host, port, timeout=TIMEOUT_S) as s:
            s.starttls()
            s.login(user, password)
            s.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise NotifyError(f"{type(e).__name__}: {e}") from e


def send_package_email(to: str, package_id: str | None, recipient: str | None, shipping: str,
                       paid_now: bool = False) -> None:
    send(package_email(to, package_id, recipient, shipping, paid_now))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    send_package_email(sys.argv[1], "PKG-TEST", "Test Recipient", "STANDARD")
    print(f"sent a test email to {sys.argv[1]}")
