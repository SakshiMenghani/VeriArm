"""VeriArm checkout: a voice conversation at the blue zone (the checkout counter), then one move.

Per package, at the blue zone:
  scan (find -> read label -> check inventory)
  UNIDENTIFIED (no / unreadable / non-VeriArm label, not in the inventory, wrong recipient name):
           "I can't identify this package. Please generate a shipping label first, ..." -> stays, no move
  PAID     (inventory says so, with its shipping):  "This package is paid, with Standard shipping."
  UNPAID   "Standard for 5 dollars, or Express for 12?" -> "tap your card on the payment area, then say done"
           -> "Payment accepted."   (cancelled -> "This package stays here until it's paid." -> no move)
  "Would you like the notification sent to your email?" -> "Sending it to <email>."
then:   pick -> Standard -> green zone, Express -> orange zone
        "Do you have another package to send?" -> yes: place it in the blue scan area, same process again;
        no: "Thank you, goodbye!"   (no email on file: offers to take one, saves it to the package, sends)

Paid status, shipping and the email come from the inventory database (inventory_seed.csv); paying at
checkout marks the package PAID there. The email is sent by notify.py (SMTP settings in .env); the card tap is confirmed by saying "done"
(camera check: stage 3).

  python checkout.py                       full conversation with the robot (no motion prompts)
  python checkout.py --confirm             ... asking before each robot motion
  python checkout.py --no-pick             talk + scan, but never pick up the box
  python checkout.py --simulate PKG-003    no robot: test the conversation for this package
                                           (PKG-001 paid, PKG-003 unpaid, PKG-099 unknown, NONE no label)
  python checkout.py --type ...            type your answers instead of speaking them
  python checkout.py --once                one package, then goodbye (--max N: stop after N packages)

main.py runs this conversation by default (python main.py --classic = the old verified / rejected sorting).

Email address: the package's sender_email in the inventory database (inventory_seed.csv; a package
without one gets asked for an email by voice, which is then saved to the database).
"""
import argparse
import asyncio
import os

import announce
from understand import understand, understand_email
from voice_input import ask

# Paid status, shipping and the notification email come from the inventory database (inventory_seed.csv).
SHIPPING = {  # key -> (name, delivery time, price in dollars, zone)
    "standard": ("Standard", "3 to 5 days", 5, "green"),
    "express": ("Express", "1 to 2 days", 12, "orange"),
}
YES_NO = {"yes": "yes / agree / please do", "no": "no / decline / not now / no thanks"}
MAX_TRIES = 2


def choose(question: str, choices: dict[str, str], typed: bool, retry: str | None = None) -> str | None:
    """Ask, listen, and map the answer to a choice key. Asks again once if unclear; None if still unclear."""
    for attempt in range(MAX_TRIES):
        answer = ask(question if attempt == 0 else (retry or f"Sorry, I didn't catch that. {question}"), typed)
        choice = understand(question, choices, answer)
        print(f"  [understood] {choice}")
        if choice != "unclear":
            return choice
    return None


def spoken_email(email: str) -> str:
    """'a.b@gmail.com' -> 'a dot b at gmail dot com', so the voice reads it clearly."""
    return email.replace("@", " at ").replace(".", " dot ")


def take_payment(typed: bool) -> str | None:
    """Shipping choice + (simulated) card tap. Returns the shipping key, or None if not paid."""
    (s_name, _, s_price, _), (e_name, _, e_price, _) = SHIPPING["standard"], SHIPPING["express"]
    ship = choose(f"This package hasn't been paid yet. Which shipping would you like? "
                  f"{s_name} for {s_price} dollars, or {e_name} for {e_price} dollars.",
                  {k: f"{n} shipping, {t}, ${p}" for k, (n, t, p, _) in SHIPPING.items()}, typed,
                  retry=f"Sorry, I didn't catch that. {s_name} for {s_price} dollars, or {e_name} for {e_price}?")
    if ship is None:
        announce.speak("This package stays here until it's paid.")
        return None
    name, days, price, _ = SHIPPING[ship]
    tapped = choose(f"{name} shipping, {days}, {price} dollars. Please tap your card on the payment area, "
                    "then say done.",
                    {"done": "they tapped the card / done / finished / paid",
                     "cancel": "they don't want to pay / cancel / stop"},
                    typed, retry="Please tap your card on the payment area, then say done.")
    if tapped != "done":
        announce.speak("Okay, payment cancelled. This package stays here until it's paid.")
        return None
    announce.speak("Payment accepted.")
    return ship


def ask_new_email(typed: bool) -> str | None:
    """No email on file: offer to take one. Listens, reads it back, and returns it once confirmed (None if not)."""
    if choose("I don't have an email address for this package. Would you like to share one?",
              YES_NO, typed) != "yes":
        announce.speak("Okay, no email then.")
        return None
    question = "Please say your email address."
    for _ in range(MAX_TRIES):
        email = understand_email(ask(question, typed))
        print(f"  [understood email] {email or '-'}")
        if not email:
            question = "Sorry, I didn't catch an email address. Please say it again, slowly."
            continue
        if choose(f"I heard {spoken_email(email)}. Is that right?", YES_NO, typed) == "yes":
            return email
        question = "Okay, please say your email address again."
    announce.speak("Sorry, I couldn't get the email address, so I won't send a notification.")
    return None


def offer_notification(typed: bool, result: dict, shipping: str, paid_now: bool = False,
                       simulate: bool = False) -> None:
    """Email the package's sender_email from the inventory (asks for one by voice if it has none) via notify.py.
    simulate: say it, but don't send."""
    answer = choose("Would you like the notification sent to your email?", YES_NO, typed)
    if answer == "no":
        announce.speak("Okay, no notification.")
        return
    if answer != "yes":  # unclear twice: don't send, and say so
        announce.speak("Okay, I'll skip the notification.")
        return
    email = result.get("sender_email")  # only from the inventory database
    if not email:
        email = ask_new_email(typed)
        if not email:
            return
        result["sender_email"] = email
        if not simulate and result.get("package_id"):  # remember it for this package (and on this result)
            from inventory import connect_db, set_email, update_result
            db = connect_db()
            try:
                set_email(db, result["package_id"], email)
                if result.get("result_id"):
                    update_result(result["result_id"], db, sender_email=email)
                print(f"[checkout] saved {email} for {result['package_id']}")
            finally:
                db.close()
    announce.speak(f"Sending it to {spoken_email(email)}.")
    if simulate:
        print(f"[notify] (simulate) not sending to {email}")
        return
    import notify
    try:
        notify.send_package_email(email, result.get("package_id"), result.get("recipient"), shipping, paid_now)
        print(f"[notify] email sent to {email}")
    except notify.NotifyError as e:
        print(f"[notify] email to {email} failed: {e}")
        announce.speak("Sorry, I couldn't send the email.")


async def move_to_shipping_area(machine, result: dict, shipping: str) -> bool:
    """Re-check the position, pick, place in the shipping area's zone (Standard = green, Express = orange)."""
    import find_pick
    from detect import TABLE_TOP_Z
    from inventory import mark_placed
    from place import place_box
    from zones import drop_center
    zone = SHIPPING[shipping][3]
    try:
        box_center = await find_pick.find_box(machine, move=False)  # re-check: the box may have been nudged
    except (LookupError, RuntimeError) as e:
        print(f"[checkout] re-check failed: {e}")
        announce.speak("I can't see the package clearly anymore, so I won't pick it up.")
        return False
    try:
        await find_pick.pick_box(machine, box_center)
    except find_pick.PickFailedError as e:
        print(f"[checkout] pick failed: {e}")
        announce.speak("I couldn't pick up the package.")
        return False
    drop = drop_center(zone, box_center.z - TABLE_TOP_Z)
    await place_box(machine, zone == "green", drop)  # says "Moving the package to the ... shipping area."
    mark_placed(result["result_id"])
    return True


def simulated_result(package: str) -> dict:
    """A scan result without the robot, for --simulate: the package as the inventory database has it."""
    from inventory import PAID, connect_db, get_item
    pid = None if package.upper() == "NONE" else package.upper()
    db = connect_db()
    try:
        item = get_item(db, pid) if pid else None
    finally:
        db.close()
    announce.package_detected("large")
    announce.checking_label()
    announce.label_read(f"VA1|{pid}|{item['recipient'] if item else 'Test Person'}" if pid else None)
    if item is None:
        return {"accepted": False, "package_id": pid, "reason": "Not in inventory" if pid else "No label found"}
    paid = item["payment"] == PAID
    return {"accepted": paid, "package_id": pid, "recipient": item["recipient"],
            "reason": "Paid" if paid else "Not paid", "payment": item["payment"],
            "shipping": item["shipping"], "sender_email": item["sender_email"]}


WATCH_INTERVAL_S = 1.5  # how often to look at the blue zone while waiting
CONFIRM_LOOKS = 2       # a box must be seen this many looks in a row (one bad frame is not a package)


async def wait_for_package(machine) -> None:
    """Watch the blue zone quietly until a box is really there. Says nothing per look; scan() then
    announces "Package detected" once."""
    import find_pick
    from detect import NoBoxError, ZoneBlockedError
    seen, first, waiting_said = 0, True, False
    while seen < CONFIRM_LOOKS:
        try:
            await find_pick.find_box(machine, move=first, speak=False)  # move to overview-blue once
            seen += 1
        except (NoBoxError, ZoneBlockedError) as e:  # empty, or a hand is in the zone placing the box
            seen = 0
            if not waiting_said:
                print(f"[checkout] waiting for a package in the blue zone ({type(e).__name__})")
                announce.speak("Waiting for a package in the blue zone.")
                waiting_said = True
        first = False
        if seen < CONFIRM_LOOKS:
            await asyncio.sleep(WATCH_INTERVAL_S)


async def handle_package(machine, args) -> bool:
    """One package. Returns False if there was no package to handle."""
    if args.simulate:
        result = simulated_result(args.simulate)
    else:
        from detect import NotAtViewError, ZoneBlockedError
        from scan import scan
        await wait_for_package(machine)
        try:
            result = await scan(machine)  # announces: detected, label read
        except (ZoneBlockedError, NotAtViewError) as e:  # find_box already said "I can't see the blue zone"
            print(f"[checkout] can't look at the zone: {e}")
            return False
        if result is None:
            return False

    unpaid = result.get("reason") == "Not paid"  # our label, known package, recipient matches: pays at checkout
    if not result["accepted"] and not unpaid:  # no / unreadable / non-VeriArm label, not in inventory, wrong name
        print(f"[checkout] unidentified: {result.get('reason')}")
        announce.speak("I can't identify this package. Please generate a shipping label first, "
                       "then place it in the checkout area.")
        return True

    if not unpaid:
        shipping = (result.get("shipping") or "standard").lower()
        announce.speak(f"This package is paid, with {SHIPPING[shipping][0]} shipping.")
    else:
        shipping = take_payment(args.type)
        if shipping is None:
            return True  # not paid: the box stays at the counter
        if not args.simulate:  # record the payment, so the next scan sees it as paid
            from inventory import connect_db, mark_paid_at_checkout, set_payment
            db = connect_db()
            try:
                set_payment(db, result["package_id"], True, shipping)
                if result.get("result_id"):
                    mark_paid_at_checkout(result["result_id"], shipping, db)
            finally:
                db.close()

    if not args.simulate and result.get("result_id"):  # the box goes by shipping, not by the scan's zone
        from inventory import update_result
        update_result(result["result_id"], zone=SHIPPING[shipping][3])

    offer_notification(args.type, result, shipping, paid_now=unpaid, simulate=bool(args.simulate))  # before the move

    if args.simulate:
        announce.moving_to(SHIPPING[shipping][3])
    elif not args.no_pick:
        await move_to_shipping_area(machine, result, shipping)
    return True


FIXED_LINES = [  # generated in the background at start, so they play without delay (ElevenLabs voice)
    "Hello, this is VeriArm. I'll be here to help you sort packages whenever you are ready.",
    "Package detected: the large box.", "Package detected: the tall box.", "Checking the label.",
    "This package is paid, with Standard shipping.", "This package is paid, with Express shipping.",
    "This package hasn't been paid yet. Which shipping would you like? Standard for 5 dollars, "
    "or Express for 12 dollars.",
    "Standard shipping, 3 to 5 days, 5 dollars. Please tap your card on the payment area, then say done.",
    "Express shipping, 1 to 2 days, 12 dollars. Please tap your card on the payment area, then say done.",
    "Okay, no notification.", "Okay, I'll skip the notification.",
    "Payment accepted.", "This package stays here until it's paid.",
    "I can't identify this package. Please generate a shipping label first, then place it in the checkout area.",
    "Would you like the notification sent to your email?", "Do you have another package to send?",
    "Great. Please place it in the blue scan area.", "Waiting for a package in the blue zone.",
    "Thank you, goodbye!", "Please say your email address.", "Okay, no email then.",
    "If you have another package, just place it in the blue scan area.",
    "I don't have an email address for this package. Would you like to share one?",
    "Moving the package to the Standard shipping area.", "Moving the package to the Express shipping area.",
    "Sorry, I couldn't send the email.",
]


def package_lines() -> list[str]:
    """Per-package sentences (label read, email) from the inventory, generated at start like FIXED_LINES,
    so they don't take ~1.5 s of ElevenLabs time in the middle of the conversation."""
    from inventory import all_items, connect_db
    db = connect_db()
    try:
        items = all_items(db)
    finally:
        db.close()
    lines = [f"Label read: package {announce.spell_id(r['id'])}, for {r['recipient']}." for r in items]
    return lines + [f"Sending it to {spoken_email(r['sender_email'])}." for r in items if r["sender_email"]]


async def conversation(machine, args) -> None:
    announce.prepare(FIXED_LINES + package_lines())
    if not args.type:
        import voice_input
        voice_input.calibrate()  # measure the room while it's quiet, before VeriArm starts talking
    announce.intro()
    handled = 0
    while True:
        if not await handle_package(machine, args):
            continue  # the box was gone by the time we scanned: wait for one again
        handled += 1
        if args.once or (args.max and handled >= args.max):  # --simulate asks too (yes: the same test package again)
            break
        more = choose("Do you have another package to send?", YES_NO, args.type)
        if more == "no":
            break
        if more == "yes":
            announce.speak("Great. Please place it in the blue scan area.")  # wait_for_package() notices it
        else:  # unclear twice (chatter, silence): don't end the session, just keep watching the zone
            announce.speak("If you have another package, just place it in the blue scan area.")
    announce.speak("Thank you, goodbye!", wait=True)


async def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--confirm", action="store_true", help="ask before each robot motion")
    p.add_argument("--no-pick", action="store_true", help="scan and talk, but never pick the box up")
    p.add_argument("--simulate", metavar="PACKAGE", help="no robot: PKG-001, PKG-003, PKG-099, NONE ...")
    p.add_argument("--type", action="store_true", help="type answers instead of speaking them")
    p.add_argument("--once", action="store_true", help="one package, then goodbye")
    p.add_argument("--max", type=int, help="stop after this many packages")
    args = p.parse_args()
    announce.WORDING = "checkout"

    if args.simulate:
        await conversation(None, args)
        return
    import find_pick
    from robot import connect
    find_pick.ASK_BEFORE_MOVING = args.confirm
    async with await connect() as machine:
        await conversation(machine, args)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
