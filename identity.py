"""Local parsing and masking for the synthetic scheduling identifiers."""

import re
from datetime import datetime

DOB = re.compile(
    r"\b(?:\d{4}[-/.\\]\d{1,2}[-/.\\]\d{1,2}|\d{1,2}[-/.\\]\d{1,2}[-/.\\]\d{4})\b"
)
PHONE = re.compile(r"(?<![\w-])(?:\+?1[ .-]?)?(?:\(\d{3}\)[ .-]?|\d{3}[ .-]?)?\d{3}[ .-]?\d{4}(?![\w-])")
ZIP = re.compile(r"\b\d{5}(?:-\d{4})?\b")
_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_RECORD_ID = re.compile(r"\b(?:pat|appt|handoff)_[a-zA-Z0-9_-]+\b")
_MONTH = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
_WRITTEN_DATE = re.compile(r"\b(?:" + _MONTH + r"\.?\s+\d{1,2}(?:st|nd|rd|th)?[,]?\s+\d{4}|\d{1,2}\s+" + _MONTH + r"\s+\d{4})\b", re.I)
_DATE = re.compile(DOB.pattern + "|" + _WRITTEN_DATE.pattern, re.I)
_BIRTH_LABEL = r"date of birth|birth\s?date|birthday|dob"
_BIRTH_DATE = re.compile(
    r"(?P<label>\b(?:" + _BIRTH_LABEL + r"|born)\b"
    r"(?:\s+(?:is|was|on))?\s*[:=,]?\s*)(?P<date>" + _DATE.pattern + ")", re.I
)
_ID_LABEL = r"(?:" + _BIRTH_LABEL + r"|phone(?: number)?|(?:patient |identity )?details)"
_WRONG_ID = re.compile(
    r"\b(?:wrong|incorrect)\s+(?P<after>" + _ID_LABEL + r")\b|"
    r"\b(?P<before>" + _ID_LABEL + r")\s+(?:is|was|are|were)\s+(?:wrong|incorrect)\b", re.I
)


def disputed_identifier(text):
    match = _WRONG_ID.search(text)
    if not match:
        return ""
    field = (match["after"] or match["before"]).lower()
    if field.startswith("phone"):
        return "phone"
    return "both" if field.endswith("details") else "birth"


def redact_identifiers(text):
    text = _KEY.sub("[key]", text)
    text = _RECORD_ID.sub("[record]", text)
    text = _BIRTH_DATE.sub(r"\g<label>[dob]", text)
    text = _DATE.sub("[date]", text)
    text = ZIP.sub("[zip]", text)
    return PHONE.sub("[phone]", text)


def identity_fields(text):
    return _parse_phone(text), _parse_dob(text)


def turn_fields(text):
    """An absent labelled DOB is None; an invalid or ambiguous one is empty."""
    births = _BIRTH_DATE.findall(text)
    dob = None
    if births:
        dob = _parse_dob(births[0][1]) if len(births) == 1 else ""
    remaining = _BIRTH_DATE.sub(" ", text)
    dates = [_parse_dob(match.group()) for match in _DATE.finditer(remaining)]
    return _parse_phone(remaining), dob, dates


def _parse_phone(text):
    """Return the fixture phone, a 10-digit number, or nothing.

    Dates and ZIP codes are removed first. A 7-digit number becomes
    ``555-0101``. A 10-digit number is kept intact so it cannot be
    shortened into one of those fixture phones.
    """
    remaining = _WRITTEN_DATE.sub(" ", DOB.sub(" ", text))
    remaining = ZIP.sub(" ", remaining)
    ten = re.search(
        r"(?<!\d)\+?1?[\s.-]?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)",
        remaining,
    )
    if ten:
        digits = re.sub(r"\D", "", ten.group())
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) == 10:
            return digits
    seven = re.search(r"(?<!\d)(?:\(\d{3}\)|\d{3})[\s.-]?\d{4}(?!\d)", remaining)
    if seven:
        digits = re.sub(r"\D", "", seven.group())
        if len(digits) == 7:
            return "%s-%s" % (digits[:3], digits[3:])
    bare = re.search(r"(?<!\d)\d{7}(?!\d)", remaining)
    if bare:
        digits = bare.group()
        return "%s-%s" % (digits[:3], digits[3:])
    return ""


def _parse_dob(text):
    match = DOB.search(text)
    if match:
        parsed = _from_numeric_date(match.group())
        if parsed:
            return parsed
    written = _WRITTEN_DATE.search(text)
    if not written:
        return ""
    raw = re.sub(r"(\d+)(?:st|nd|rd|th)", r"\1", written.group(), flags=re.I)
    raw = raw.replace(".", "").replace(",", "")
    for fmt in ("%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _from_numeric_date(value):
    parts = re.split(r"[-/.\\]", value)
    if len(parts) != 3:
        return ""
    try:
        if len(parts[0]) == 4:
            year, month, day = (int(part) for part in parts)
        else:
            first, second, year = (int(part) for part in parts)
            if first > 12 and second <= 12:
                day, month = first, second
            else:
                month, day = first, second
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return ""
