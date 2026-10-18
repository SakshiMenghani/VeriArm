"""Understanding answers: Claude maps what someone said to one of the expected choices.

    understand("Would you like the notification emailed?", {"yes": "...", "no": "..."}, "yeah go ahead")
    -> "yes"

Returns one of the choice keys, or "unclear". If Claude can't be reached, a simple keyword match is used.

  python understand.py "the fast one"        try it on the shipping question
"""
import json
import os
import re
import sys

MODEL = "claude-opus-5-5"
EFFORT = "low"  # short answers, live conversation: speed matters more than depth

SYSTEM = (
    "You interpret a spoken answer in a short voice conversation with VeriArm, a package-sorting robot. "
    "The answer came from speech-to-text, so expect transcription slips. Pick the one choice the person "
    "meant, or \"unclear\" if they didn't clearly pick one (silence, off-topic, asking something else). "
    "For yes/no questions, a bare \"thank you\" or \"thanks\" with nothing else is unclear: it can mean "
    "either \"yes please\" or \"no thanks\"."
)


def _keyword_fallback(choices: dict[str, str], answer: str) -> str:
    a = f" {answer.lower()} "
    if answer.lower().strip(" .!") in ("thank you", "thanks", "thank you very much"):
        return "unclear"
    synonyms = {"yes": ["yes", "yeah", "yep", "sure", "ok", "okay", "please", "go ahead"],
                "no": ["no", "nope", "not", "don't", "no thanks"]}
    hits = [k for k in choices if any(f" {w} " in a or w in a.split() for w in synonyms.get(k, [k]))]
    return hits[0] if len(hits) == 1 else "unclear"


QUICK_WORDS = {  # short answers that can only mean one choice: no need to ask Claude (saves 1.5-3.5 s)
    "yes": {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "please", "correct", "right", "absolutely",
            "definitely", "of course", "that's right", "that's correct", "yes please", "sure thing", "go ahead"},
    "no": {"no", "nope", "nah", "no thanks", "no thank you", "not now", "no, thank you", "no it's not",
           "that's wrong", "wrong", "incorrect"},
    "done": {"done", "i'm done", "finished", "paid", "tapped", "ok done", "okay done"},
    "cancel": {"cancel", "stop", "never mind"},
    "standard": {"standard", "standard shipping", "the standard one", "standard please"},
    "express": {"express", "express shipping", "the express one", "express please"},
}


def _quick_match(choices: dict[str, str], answer: str) -> str | None:
    """The choice for an obvious short answer ("Yes.", "no thanks", "Express"), else None (ask Claude)."""
    a = " ".join(re.sub(r"[^\w\s']", " ", answer.lower()).split())
    hits = [k for k in choices if a in QUICK_WORDS.get(k, set()) | {k}]
    if len(hits) == 1:
        return hits[0]
    # A distinctive choice word said once amid background talk ("... Express ... circuits into worlds") counts.
    # Not for yes / no: those words turn up in any nearby conversation.
    words = a.split()
    found = [k for k in choices if k not in ("yes", "no") and words.count(k) == 1]
    return found[0] if len(found) == 1 else None


def understand(question: str, choices: dict[str, str], answer: str) -> str:
    """Which of `choices` (key -> description) the answer means, or "unclear"."""
    if not answer.strip():
        return "unclear"
    if (quick := _quick_match(choices, answer)) is not None:
        return quick
    try:
        import anthropic

        from robot import _load_env
        _load_env()
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("no ANTHROPIC_API_KEY")
        keys = list(choices) + ["unclear"]
        schema = {"type": "object", "properties": {"choice": {"type": "string", "enum": keys}},
                  "required": ["choice"], "additionalProperties": False}
        listing = "\n".join(f"- {k}: {d}" for k, d in choices.items())
        client = anthropic.Anthropic()
        # fallbacks="default": if a safety classifier declines, the API retries on a fallback model.
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=1000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM,
            output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content":
                       f"Question: {question}\nChoices:\n{listing}\nWhat they said: {answer!r}"}],
        )
        if response.stop_reason in ("refusal", "max_tokens"):
            raise RuntimeError(f"stop_reason {response.stop_reason}")
        text = next(b.text for b in response.content if b.type == "text")
        choice = json.loads(text)["choice"]
        return choice if choice in keys else "unclear"
    except Exception as e:  # network, key, parsing: the conversation must keep going
        print(f"  [understand] Claude unavailable ({e}); using keyword matching")
        return _keyword_fallback(choices, answer)


EMAIL_RE = re.compile(r"[a-z0-9._%+-]+@[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}")
EMAIL_SYSTEM = (
    "You read an email address that someone spoke to VeriArm, a package-sorting robot. The text came from "
    "speech-to-text, so the address may be spelled out or spoken in words (\"at\", \"dot\", \"underscore\", "
    "letters one by one, \"double s\"). Return the address they meant, all lowercase, or an empty string "
    "if they didn't give one."
)


def _email_fallback(answer: str) -> str:
    """'Sakshi dot M at gmail dot com' -> 'sakshi.m@gmail.com' by word replacement; "" if no address."""
    t = f" {answer.lower()} "
    for words, sym in ((r"at the rate|at sign|at", "@"), (r"dot|period|point", "."),
                       (r"underscore", "_"), (r"dash|hyphen|minus", "-")):
        t = re.sub(rf"\s(?:{words})\s", f" {sym} ", t)
    t = re.sub(r"\s+", "", t).strip(".,!?")
    m = EMAIL_RE.search(t)
    return m.group(0) if m else ""


def understand_email(answer: str) -> str:
    """The email address in a spoken answer, lowercase, or "" if there isn't a valid one."""
    if not answer.strip():
        return ""
    try:
        import anthropic

        from robot import _load_env
        _load_env()
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("no ANTHROPIC_API_KEY")
        schema = {"type": "object", "properties": {"email": {"type": "string"}},
                  "required": ["email"], "additionalProperties": False}
        response = anthropic.Anthropic().beta.messages.create(
            model=MODEL,
            max_tokens=1000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=EMAIL_SYSTEM,
            output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": f"What they said: {answer!r}"}],
        )
        if response.stop_reason in ("refusal", "max_tokens"):
            raise RuntimeError(f"stop_reason {response.stop_reason}")
        email = json.loads(next(b.text for b in response.content if b.type == "text"))["email"].strip().lower()
        return email if EMAIL_RE.fullmatch(email) else ""
    except Exception as e:  # network, key, parsing: the conversation must keep going
        print(f"  [understand] Claude unavailable ({e}); reading the email by word replacement")
        return _email_fallback(answer)


if __name__ == "__main__":
    shipping = {"standard": "Standard shipping", "express": "Express shipping", "overnight": "Overnight shipping"}
    print(understand("Which shipping option would you like: standard, express or overnight?", shipping,
                     " ".join(sys.argv[1:]) or "the fast one please"))
