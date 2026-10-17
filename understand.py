"""Understanding answers: Claude maps what someone said to one of the expected choices.

    understand("Would you like the notification emailed?", {"yes": "...", "no": "..."}, "yeah go ahead")
    -> "yes"

Returns one of the choice keys, or "unclear". If Claude can't be reached, a simple keyword match is used.

  python understand.py "the fast one"        try it on the shipping question
"""
import json
import os
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


def understand(question: str, choices: dict[str, str], answer: str) -> str:
    """Which of `choices` (key -> description) the answer means, or "unclear"."""
    if not answer.strip():
        return "unclear"
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


if __name__ == "__main__":
    shipping = {"standard": "Standard shipping", "express": "Express shipping", "overnight": "Overnight shipping"}
    print(understand("Which shipping option would you like: standard, express or overnight?", shipping,
                     " ".join(sys.argv[1:]) or "the fast one please"))
