"""Fills a call's Outcome, Call booked for and Key points by having Claude read the transcript.

Used when ANTHROPIC_API_KEY is set. It does not depend on Bolna returning extracted data: after an answered call,
the transcript Bolna already gives us is sent to Claude, which returns the three fields as JSON.
"""

import json
import logging

import anthropic

from .config import Settings

log = logging.getLogger(__name__)

MODEL = "claude-haiku-5-5"  # fast and inexpensive; enough for three fields per call

SYSTEM = (
    "You read transcripts of outbound sales phone calls made by an AI agent for an Indian business. The customer "
    "may speak any Indian language, English, or a mix. Fill the fields from what was actually said; never guess.\n"
    "- outcome: interested if the customer showed interest, asked for details or prices, agreed to a "
    "visit/demo/meeting, or asked to be called back; not_interested if they declined or were not keen; "
    "wrong_number only if they said it is the wrong number or not the person called.\n"
    "- callback_time: the date and time the customer agreed to for a call back, visit, demo or meeting, written "
    "in English as they said it (for example \"Saturday 10 am\", \"tomorrow evening\"). Empty string if no time "
    "was agreed.\n"
    "- key_points: one short sentence in English with the customer's main questions, objections or commitments. "
    "Empty string if they said nothing of substance."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": ["interested", "not_interested", "wrong_number"]},
        "callback_time": {"type": "string"},
        "key_points": {"type": "string"},
    },
    "required": ["outcome", "callback_time", "key_points"],
    "additionalProperties": False,
}


class CallExtractor:
    def __init__(self, api_key: str):
        self._client = anthropic.Anthropic(api_key=api_key)

    def extract(self, transcript: str, business_name: str | None = None) -> dict | None:
        """{"outcome", "callback_time", "key_points"} for a call, or None if Claude couldn't be used."""
        context = f"Business: {business_name}\n\n" if business_name else ""
        try:
            response = self._client.messages.create(
                model=MODEL,
                max_tokens=4000,
                system=SYSTEM,
                messages=[{"role": "user",
                           "content": f"{context}<transcript>\n{transcript}\n</transcript>"}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            )
        except anthropic.AuthenticationError:
            log.error("ANTHROPIC_API_KEY was rejected; call results can't be filled in")
            return None
        except anthropic.APIStatusError as exc:
            log.warning("Claude could not read the call (HTTP %s): %s", exc.status_code, exc.message)
            return None
        except anthropic.APIConnectionError as exc:
            log.warning("could not reach Claude: %s", exc)
            return None
        if response.stop_reason in ("refusal", "max_tokens"):
            log.warning("Claude did not return call results (stop_reason=%s)", response.stop_reason)
            return None
        text = next((block.text for block in response.content if block.type == "text"), "")
        try:
            data = json.loads(text)
        except ValueError:
            log.warning("Claude returned call results that aren't JSON")
            return None
        return {key: (data.get(key) or "").strip() or None for key in ("outcome", "callback_time", "key_points")}


def make_extractor(settings: Settings) -> CallExtractor | None:
    return CallExtractor(settings.anthropic_api_key) if settings.anthropic_api_key else None
