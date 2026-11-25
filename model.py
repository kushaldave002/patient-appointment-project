"""Interpret a redacted turn using the current workflow step as context."""

import json
import re
import urllib.error
import urllib.request

from identity import redact_identifiers

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
MAX_BYTES = 1_000_000

_INTENTS = {
    "provider_lookup",
    "book",
    "medical_advice",
    "human",
    "appointment_lookup",
    "unsupported",
    "continue",
}
_SPECIALTIES = {"primary_care", "dermatology"}
_LOCATIONS = {"downtown", "uptown", "lakeside"}
_SYSTEM = (
    "Return one JSON object with intent, specialty, location, slot_choice, date_role, identity_correction. "
    "Intent: provider_lookup for finding clinicians, appointment_lookup for existing bookings, book for a new "
    "appointment, continue for a reply to the current step, human, medical_advice, or unsupported. "
    "Questions about symptoms or treatment and requests for a person (including front desk staff) take priority "
    "at every step, even while verifying a ZIP. Accepting an offered handoff means human. "
    "Finding family medicine providers is a primary_care directory request. "
    "Use unsupported for a requested "
    "specialty, location or service outside the supported options, including reschedule and cancellation. "
    "Use continue for an ambiguous reply or a correction within the active task. "
    "Extract only the desired value in a correction, excluding rejected locations and specialties. "
    "Availability on a requested day is a booking search. For an unlabeled [date], set date_role to visit or birth "
    "according to what the user means. A corrected birthday remains a birth date after verification. "
    "When the user disputes prior identifying information, set identity_correction to birth, phone, or both, "
    "even if they have not supplied replacements. Otherwise leave it empty. "
    "Use any_date when the user removes a date restriction. "
    "Specialty: primary_care or dermatology. "
    "Location: downtown, uptown or lakeside. For an explicit slot choice return its one-based number, "
    "otherwise leave it empty. Leave absent fields empty. Never return identity, consent, or advice."
)


class ModelError(Exception):
    pass


def complete(api_key, model, user_text, urlopen=urllib.request.urlopen, timeout=30, *, context=None):
    redacted = redact_identifiers(user_text)
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 120,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _SYSTEM + " Current workflow: " + json.dumps(context or {"step": "idle"})},
            {"role": "user", "content": redacted},
        ],
    }
    raw_body = json.dumps(payload).encode()
    request = urllib.request.Request(
        OPENAI_URL,
        data=raw_body,
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as err:
        if err.fp is not None:
            err.close()
        raise ModelError("The model request failed.") from None
    except Exception:
        raise ModelError("The model request failed.") from None
    if len(raw) > MAX_BYTES:
        raise ModelError("The model request failed.")
    try:
        parsed = json.loads(raw.decode())
        content = parsed["choices"][0]["message"]["content"]
        fields = json.loads(content)
    except (KeyError, IndexError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        raise ModelError("The model request failed.") from None
    if not isinstance(fields, dict):
        raise ModelError("The model request failed.")
    return _fields(fields)


def _fields(payload):
    intent = _token(payload.get("intent"))
    if intent not in _INTENTS:
        intent = "unsupported"
    specialty = _enum(payload.get("specialty"), _SPECIALTIES, underscore=True)
    location = _enum(payload.get("location"), _LOCATIONS, underscore=False)
    if payload.get("specialty") and not specialty and intent in ("book", "provider_lookup", "continue"):
        intent = "unsupported"
    if payload.get("location") and not location and intent in ("book", "provider_lookup", "continue"):
        intent = "unsupported"
    slot_choice = _slot(payload.get("slot_choice"))
    return {
        "intent": intent,
        "specialty": specialty,
        "location": location,
        "slot_choice": slot_choice,
        "date_role": _enum(payload.get("date_role"), {"birth", "visit", "any_date"}, underscore=False),
        "identity_correction": _enum(payload.get("identity_correction"), {"birth", "phone", "both"}, underscore=False),
    }


def _token(value):
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def _enum(value, allowed, underscore):
    text = _token(value)
    if underscore:
        text = text.replace(" ", "_")
    if text in allowed:
        return text
    return ""


def _slot(value):
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text) > 40:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        return ""
    return text
