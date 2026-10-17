"""VeriArm checkout: a voice conversation at the blue zone (the checkout counter), then one move.

Per package, at the blue zone:
  scan (find -> read label -> check inventory)
  UNIDENTIFIED (no / unreadable / non-VeriArm label, not in the inventory, recalled):
           "I can't identify this package. Please generate a shipping label first, ..." -> stays, no move
  PAID     (ORDERS says so, with its shipping):  "This package is paid, with Standard shipping."
  UNPAID   "Standard for 5 dollars, or Express for 12?" -> "tap your card on the purple area, then say done"
           -> "Payment accepted."   (cancelled -> "This package stays here until it's paid." -> no move)
  "Would you like the notification sent to your email?" -> "Sending it to <email>."
then:   pick -> Standard -> green zone, Express -> orange zone
        "Would you like to place another package?" -> next package, or "Thanks, goodbye!"

Paid status and shipping are hardcoded in ORDERS for the demo (to move into the inventory database).
Stage 1: the email is only announced (sending: stage 2); the card tap is confirmed by saying "done"
(camera check: stage 3).

  python checkout.py                       full conversation with the robot (no motion prompts)
  python checkout.py --confirm             ... asking before each robot motion
  python checkout.py --no-pick             talk + scan, but never pick up the box
  python checkout.py --simulate PKG-003    no robot: test the conversation for this package
                                           (PKG-001 paid, PKG-003 unpaid, PKG-006 recalled, NONE no label)
  python checkout.py --type ...            type your answers instead of speaking them

Email address: VERIARM_NOTIFY_EMAIL in .env.
"""
import argparse
import asyncio
import os

import announce
from understand import understand
from voice_input import ask

# Hardcoded for the demo, until the inventory database has paid / shipping columns.
# Known packages that aren't listed here pay at checkout.
ORDERS = {
    "PKG-001": {"paid": True, "shipping": "standard"},
    "PKG-003": {"paid": False},
}
SHIPPING = {  # key -> (name, delivery time, price in dollars, zone)
    "standard": ("Standard", "3 to 5 days", 5, "green"),
    "express": ("Express", "1 to 2 days", 12, "orange"),
}
YES_NO = {"yes": "yes / agree / please do", "no": "no / decline / not now / no thanks"}
READY = {"ready": "the package is placed / ready / go", "stop": "they want to stop / finish"}
MAX_TRIES = 2
SIMULATED_INVENTORY = {"PKG-001", "PKG-002", "PKG-003", "PKG-004", "PKG-005"}  # --simulate only (PKG-006 recalled)


def choose(question: str, choices: dict[str, str], typed: bool, retry: str | None = None) -> str | None:
    """Ask, listen, and map the answer to a choice key. Asks again once if unclear; None if still unclear."""
    for attempt in range(MAX_TRIES):
        answer = ask(question if attempt == 0 else (retry or f"Sorry, I didn't catch that. {question}"), typed)
        choice = understand(question, choices, answer)
        print(f"  [understood] {choice}")
        if choice != "unclear":
            return choice
    return None


def notify_email() -> str:
    from robot import _load_env
    _load_env()
    return os.environ.get("VERIARM_NOTIFY_EMAIL", "").strip()


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
    tapped = choose(f"{name} shipping, {days}, {price} dollars. Please tap your card on the purple area, "
                    "then say done.",
                    {"done": "they tapped the card / done / finished / paid",
                     "cancel": "they don't want to pay / cancel / stop"},
                    typed, retry="Please tap your card on the purple area, then say done.")
    if tapped != "done":
        announce.speak("Okay, payment cancelled. This package stays here until it's paid.")
        return None
    announce.speak("Payment accepted.")
    return ship


def offer_notification(typed: bool) -> None:
    if choose("Would you like the notification sent to your email?", YES_NO, typed) != "yes":
        return
    email = notify_email()
    if email:
        announce.speak(f"Sending it to {spoken_email(email)}.")  # stage 2: actually send it
    else:
        announce.speak("I don't have an email address set up yet.")


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
    """A scan result without the robot, for --simulate."""
    pid = None if package.upper() == "NONE" else package.upper()
    announce.package_detected("large")
    announce.checking_label()
    announce.label_read(f"VA1|{pid}|Test Person" if pid else None)
    return {"accepted": pid in SIMULATED_INVENTORY, "package_id": pid,
            "reason": "Matches inventory" if pid in SIMULATED_INVENTORY else "Not expected"}


async def handle_package(machine, args) -> bool:
    """One package. Returns False if there was no package to handle."""
    if args.simulate:
        result = simulated_result(args.simulate)
    else:
        from detect import NotAtViewError, ZoneBlockedError
        from scan import scan
        try:
            result = await scan(machine)  # announces: detected, label read
        except (ZoneBlockedError, NotAtViewError) as e:  # find_box already said "I can't see the blue zone"
            print(f"[checkout] can't look at the zone: {e}")
            return False
        if result is None:
            return False

    if not result["accepted"]:  # no / unreadable / non-VeriArm label, not in inventory, recalled
        print(f"[checkout] unidentified: {result.get('reason')}")
        announce.speak("I can't identify this package. Please generate a shipping label first, "
                       "then place it in the checkout area.")
        return True

    order = ORDERS.get(result["package_id"], {"paid": False})
    if order["paid"]:
        shipping = order["shipping"]
        announce.speak(f"This package is paid, with {SHIPPING[shipping][0]} shipping.")
    else:
        shipping = take_payment(args.type)
        if shipping is None:
            return True  # not paid: the box stays at the counter

    offer_notification(args.type)  # at the counter, before the box moves

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
    "Standard shipping, 3 to 5 days, 5 dollars. Please tap your card on the purple area, then say done.",
    "Express shipping, 1 to 2 days, 12 dollars. Please tap your card on the purple area, then say done.",
    "Payment accepted.", "This package stays here until it's paid.",
    "I can't identify this package. Please generate a shipping label first, then place it in the checkout area.",
    "Would you like the notification sent to your email?", "Would you like to place another package?",
    "Great. Place it in the blue zone, then say ready.", "Thanks, goodbye!",
    "Moving the package to the Standard shipping area.", "Moving the package to the Express shipping area.",
]


async def conversation(machine, args) -> None:
    announce.prepare(FIXED_LINES)
    if not args.type:
        import voice_input
        voice_input.calibrate()  # measure the room while it's quiet, before VeriArm starts talking
    announce.intro()
    while True:
        if not await handle_package(machine, args):
            if choose("I don't see a package in the blue zone. Please place one, then say ready.",
                      READY, args.type) != "ready":
                break
            continue
        if args.simulate or choose("Would you like to place another package?", YES_NO, args.type) != "yes":
            break
        if choose("Great. Place it in the blue zone, then say ready.", READY, args.type) != "ready":
            break
    announce.speak("Thanks, goodbye!", wait=True)


async def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--confirm", action="store_true", help="ask before each robot motion")
    p.add_argument("--no-pick", action="store_true", help="scan and talk, but never pick the box up")
    p.add_argument("--simulate", metavar="PACKAGE", help="no robot: PKG-001, PKG-003, PKG-006, NONE ...")
    p.add_argument("--type", action="store_true", help="type answers instead of speaking them")
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
