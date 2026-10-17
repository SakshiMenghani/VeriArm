"""VeriArm checkout: the sorting flow as a voice conversation (paid / unpaid packages).

Per package:  scan (find -> read label -> check inventory)
  PAID:    "This package is paid."  -> pick -> green zone
  UNPAID:  "...hasn't been paid yet." -> "Which shipping option?" -> "Please tap your card on the purple area,
           then say done." -> "Payment accepted." -> pick -> orange zone
  then:    "Would you like the notification sent to your email?" -> "Sending it to <email>."
           "Would you like to place another package?" -> next package, or "Thanks, goodbye!"

Quick version: paid = the package matches the inventory (scan's "accepted"). Stage 1: the email is only
announced (sending comes in stage 2) and the card tap is confirmed by saying "done" (camera check: stage 3).

  python checkout.py                      full conversation with the robot (asks before each motion)
  python checkout.py --yes                ... without the motion prompts
  python checkout.py --no-pick            talk + scan, but never pick up the box
  python checkout.py --simulate unpaid    no robot at all: test the conversation (paid or unpaid)
  python checkout.py --type ...           type your answers instead of speaking them

Email address: VERIARM_NOTIFY_EMAIL in .env.
"""
import argparse
import asyncio
import os

import announce
from understand import understand
from voice_input import ask

SHIPPING = {  # pre-determined options: key -> (name, delivery time, price in dollars)
    "standard": ("Standard", "3 to 5 days", 5),
    "express": ("Express", "1 to 2 days", 12),
    "overnight": ("Overnight", "next day", 25),
}
YES_NO = {"yes": "yes / agree / please do", "no": "no / decline / not now"}
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


def notify_email() -> str:
    from robot import _load_env
    _load_env()
    return os.environ.get("VERIARM_NOTIFY_EMAIL", "").strip()


def spoken_email(email: str) -> str:
    """'a.b@gmail.com' -> 'a dot b at gmail dot com', so the voice reads it clearly."""
    return email.replace("@", " at ").replace(".", " dot ")


def take_payment(typed: bool) -> bool:
    """Shipping choice + (simulated) card tap. True if paid."""
    options = ", ".join(f"{n} for {p} dollars" for n, _, p in list(SHIPPING.values())[:-1])
    last = list(SHIPPING.values())[-1]
    ship = choose(f"Which shipping option would you like? {options}, or {last[0]} for {last[2]} dollars.",
                  {k: f"{n} shipping, {t}, ${p}" for k, (n, t, p) in SHIPPING.items()}, typed)
    if ship is None:
        announce.speak("No problem. I'll set this package aside until it's paid.")
        return False
    name, days, price = SHIPPING[ship]
    tapped = choose(f"{name} shipping, {days}, {price} dollars. Please tap your card on the purple area, "
                    "then say done.",
                    {"done": "they tapped the card / done / finished / paid",
                     "cancel": "they don't want to pay / cancel / stop"},
                    typed, retry="Please tap your card on the purple area, then say done.")
    if tapped != "done":
        announce.speak("Okay, payment cancelled. I'll set this package aside.")
        return False
    announce.speak("Payment accepted.")
    return True


def offer_notification(typed: bool) -> None:
    if choose("Would you like the notification sent to your email?", YES_NO, typed) != "yes":
        return
    email = notify_email()
    if email:
        announce.speak(f"Sending it to {spoken_email(email)}.")  # stage 2: actually send it
    else:
        announce.speak("I don't have an email address set up yet.")


async def pick_and_place(machine, result: dict) -> bool:
    """Same as main.py's pick part: re-check the position, pick, place in the result's zone."""
    import find_pick
    from inventory import mark_placed
    from place import place_box
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
    await place_box(machine, result["accepted"], result["drop_center_pose"])
    mark_placed(result["result_id"])
    return True


async def handle_package(machine, args) -> bool:
    """One package. Returns False if there was no package to handle."""
    if args.simulate:
        result = {"accepted": args.simulate == "paid", "package_id": "PKG-001"}
        announce.package_detected("large")
        announce.label_read(f"VA1|{result['package_id']}|Test Person")
        announce.verification(result["accepted"])
    else:
        from scan import scan
        result = await scan(machine)  # announces: detected, label read, paid / not paid
        if result is None:
            return False

    paid = result["accepted"]
    if not paid:
        paid = take_payment(args.type)  # quick version: unpaid boxes still go to orange

    if not args.simulate and not args.no_pick:
        await pick_and_place(machine, result)
    elif args.simulate:
        announce.moving_to("green" if result["accepted"] else "orange")

    if paid:
        offer_notification(args.type)
    return True


FIXED_LINES = [  # generated in the background at start, so they play without delay (ElevenLabs voice)
    "Hello, this is VeriArm. I'll be here to help you sort packages whenever you are ready.",
    "This package is paid.", "This package hasn't been paid yet.", "Payment accepted.",
    "Would you like the notification sent to your email?", "Would you like to place another package?",
    "Great. Place it in the blue zone, then say ready.", "Thanks, goodbye!",
    "Package detected: the large box.", "Moving the package to the green area.",
    "Moving the package to the orange area.",
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
                      {"ready": "the package is placed / ready / go", "stop": "they want to stop"},
                      args.type) != "ready":
                break
            continue
        if choose("Would you like to place another package?", YES_NO, args.type) != "yes":
            break
        if choose("Great. Place it in the blue zone, then say ready.",
                  {"ready": "the package is placed / ready / go", "stop": "they want to stop"},
                  args.type) != "ready":
            break
    announce.speak("Thanks, goodbye!", wait=True)


async def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--yes", action="store_true", help="don't ask before each motion")
    p.add_argument("--no-pick", action="store_true", help="scan and talk, but never pick the box up")
    p.add_argument("--simulate", choices=["paid", "unpaid"], help="no robot: test the conversation")
    p.add_argument("--type", action="store_true", help="type answers instead of speaking them")
    args = p.parse_args()
    announce.WORDING = "payment"

    if args.simulate:
        await conversation(None, args)
        return
    import find_pick
    from robot import connect
    find_pick.ASK_BEFORE_MOVING = not args.yes
    async with await connect() as machine:
        await conversation(machine, args)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
