"""Code-owned scheduling steps. The model cannot book, confirm, or identify a patient."""

import re
import time
from datetime import datetime

from identity import ZIP, disputed_identifier, identity_fields, redact_identifiers, turn_fields

from model import ModelError

YES = {"yes", "y", "yeah", "yep", "confirm", "book it"}
NO = {"no", "n", "nope", "cancel"}
_SPECIALTIES = {"primary_care", "dermatology"}
_LOCATIONS = {"downtown", "uptown", "lakeside"}
_MEDICAL = (
    "chest pain",
    "medical advice",
    "should i wait",
    "should i take",
    "emergency",
    "bleeding",
    "dizzy",
)
_SUMMARIES = {
    "identity_unclear": "Patient could not be identified for a scheduling request.",
    "medical_advice": "User asked for medical advice. No advice given.",
    "api_failure": "Scheduling system unavailable.",
    "no_availability": "No appointment slots were available.",
    "unsupported_request": "Request is outside this assistant.",
    "user_requested": "User asked for a person.",
    "other": "Scheduling request could not be completed.",
}
_GOALS = {"provider_lookup", "book", "appointment_lookup"}
_HANDOFF_OFFERS = {
    "no_patient_match": ("identity_unclear", "I couldn't find a patient with those details."),
    "no_availability": ("no_availability", "Nothing is available for that search."),
}


class MissingKey(Exception):
    pass


class Workflow:
    """One conversation. Only this object can authorize a scheduling write."""

    def __init__(self, client, extract):
        self.client = client
        self.extract = extract
        self.events = []
        self.goal = ""
        self.specialty = ""
        self.location = ""
        self.visit_date = ""
        self.phone = ""
        self.dob = ""
        self.patient_id = ""
        self.patient_name = ""
        self.slots = []
        self.pending = None
        self.providers = {}
        self.identity_misses = 0
        self.candidates = []
        self.unavailable_slots = set()
        self.terminal_reply = ""
        self._state = "idle"

    def turn(self, text):
        started = time.monotonic()
        reply = self._turn(text)
        self._event("TURN", duration_ms=round((time.monotonic() - started) * 1000, 1))
        return reply

    def drain_events(self):
        events = self.events
        self.events = []
        return events

    @property
    def suggested_replies(self):
        """Display labels and the ordinary text turn behind each shortcut."""
        if self._state == "confirm" and self.pending:
            return [("Confirm booking", "yes"), ("Choose another time", "no")]
        if self._state == "choose_slot":
            return [("Option %d" % n, str(n)) for n in range(1, len(self.slots) + 1)]
        if self._state == "need_specialty":
            return [("Primary care", "primary care"), ("Dermatology", "dermatology")]
        if self._state in _HANDOFF_OFFERS:
            return [("Hand off to a person", "hand it off")]
        return []

    def _turn(self, text):
        if self.terminal_reply:
            return self.terminal_reply
        if len(text) > 2000:
            if self.pending:
                self._clear_schedule()
            return "Please send a shorter message."
        # A disputed identity must expire even if classification fails on this turn.
        phone, dob, dates = turn_fields(text)
        dispute = disputed_identifier(text)
        self._update_identity(phone, dob, dispute)
        if _medical(text):
            return self._handoff("medical_advice", "I can't help with medical questions.")
        compact = text.strip().lower()
        accepts_offer = compact.rstrip(".!") in {"yes", "y", "yeah", "yep", "yes please", "yes, please"}
        if _wants_person(text) or (self._state in _HANDOFF_OFFERS and accepts_offer):
            return self._requested_handoff()
        confirmation = _confirmation(text)
        if self._state == "booked" and confirmation == "yes":
            return "Your last booking is confirmed. You can look it up or start a new search."
        if self._state == "confirm":
            if confirmation == "yes":
                return self._post_booking()
            if confirmation == "no":
                self.pending = None
                return "That choice was cleared. " + self._show_slots()
            if text.strip().lower().rstrip(".!? ") in YES | {"ok", "okay", "maybe"}:
                return self._ask_confirm(self.pending)

        # Values expected by the current step can be processed entirely locally.
        masked = redact_identifiers(text)
        identity_only = bool(re.fullmatch(r"(?:\[(?:phone|date|zip)\]|\band\b|[\s,;/])+[.!]?", masked.strip()))
        zip_reply = self._state == "verify_zip" and re.fullmatch(
            r"(?:(?:my )?zip(?: code)?(?: is)?\s*[:=]?\s*)?[\d -]+[.!]?", compact)
        local_reply = (
            identity_only or zip_reply or (confirmation and compact != "cancel") or
            (self._state in ("choose_slot", "confirm") and re.fullmatch(r"[0-9]{1,3}|slot_[a-z0-9_]+", compact))
        )
        model = {}
        if not local_reply:
            started = time.monotonic()
            try:
                model = self.extract(masked, self._model_context()) or {}
            except MissingKey:
                self._event("MODEL", status="error", error_code="missing_key", duration_ms=_ms(started))
                return "Set OPENAI_API_KEY in the environment. Nothing was sent."
            except ModelError:
                if self.pending:
                    self._clear_schedule()
                self._event("MODEL", status="error", error_code="model_unavailable", duration_ms=_ms(started))
                return "I couldn't reach the assistant model. Nothing was booked."
            self._event("MODEL", status="ok", duration_ms=_ms(started))

        if not isinstance(model, dict):
            return "Could you rephrase that scheduling request?"
        date_role = model.get("date_role")
        model_dispute = model.get("identity_correction") or ""
        identity_reply = identity_only and (phone or not self.patient_id)
        birth_reply = date_role == "birth" or (
            date_role != "visit" and (identity_reply or "birth" in (dispute, model_dispute))
        )
        if dob is None and dates and birth_reply:
            dob = dates.pop() if len(dates) == 1 else ""
        # These semantic signals can revoke verification. Only locally parsed values replace it.
        self._update_identity(phone, dob, model_dispute if model_dispute != dispute else "")
        intent = model.get("intent", "")
        if intent == "medical_advice":
            return self._handoff("medical_advice", "I can't help with medical questions.")
        if intent == "human":
            return self._requested_handoff()
        specialty = model.get("specialty") or _mentioned_specialty(text)
        mentioned_locations = _LOCATIONS.intersection(re.findall(r"\w+", compact))
        location = model.get("location") or ""
        if not location and len(mentioned_locations) == 1:
            location, = mentioned_locations
        if intent == "unsupported" or specialty not in ("", *_SPECIALTIES) or location not in ("", *_LOCATIONS):
            return self._handoff("unsupported_request", "That service, specialty, or location is not supported.")
        if self.identity_misses and self._state in ("identify", "no_patient_match"):
            if _give_up(text) or confirmation == "no":
                return self._handoff("identity_unclear", "I couldn't find a patient with those details.")

        if intent in _GOALS and intent != self.goal:
            self._clear_schedule()
            self.visit_date = ""
            self.goal = intent
        if not location and len(mentioned_locations) > 1:
            self._clear_schedule()
            return "Which location would you like to search?"
        if (specialty and specialty != self.specialty) or (location and location != self.location):
            self._clear_schedule()
        self.specialty = specialty or self.specialty
        self.location = location or self.location

        if len(dates) > 1 or "" in dates:
            self._clear_schedule()
            return "Please give one valid date and say whether it is your birth date or the visit date."
        visit_date = self.visit_date
        if dates and self.goal == "book":
            visit_date = dates[0]
        elif date_role == "any_date":
            visit_date = ""
        if visit_date != self.visit_date:
            self._clear_schedule()
            self.visit_date = visit_date

        if self.candidates and self.goal in ("book", "appointment_lookup"):
            self._state = "verify_zip"
            return self._resolve_zip(text)
        if self.goal == "provider_lookup":
            return self._lookup()
        if self.goal == "book":
            return self._book(text, model, confirmation)
        if self.goal == "appointment_lookup":
            return self._lookup_appointments(text)
        return "I can look up a provider or book an appointment. Which do you want?"

    def _model_context(self):
        context = {"goal": self.goal, "step": self._state, "specialty": self.specialty, "location": self.location}
        if self._state in ("choose_slot", "confirm"):
            context["options"] = [
                {"number": n, "description": self._describe(slot)} for n, slot in enumerate(self.slots, 1)
            ]
        return context

    def _book(self, text, model, confirmation):
        if self._state == "no_availability":
            if confirmation == "no" or _give_up(text):
                return self._handoff("no_availability", "Nothing is available for that search.")
            return "You can give another date, specialty or location, or ask for a person."
        if self.pending:
            chosen = self._choose(text, model)
            if not chosen:
                self.pending = None
                return self._show_slots()
            self.pending = chosen
            return self._ask_confirm(chosen)
        if not self.specialty:
            self._state = "need_specialty"
            return "Which specialty? You can say primary care or dermatology."
        if not self.patient_id:
            if not (self.phone and self.dob):
                return self._ask_identity()
            return self._search(text)
        if not self.slots:
            return self._availability()
        chosen = self._choose(text, model)
        if not chosen:
            return self._show_slots()
        self.pending = chosen
        return self._ask_confirm(chosen)

    def _ask_identity(self):
        self._state = "identify"
        if self.phone and not self.dob:
            return (
                "What is your date of birth? "
                "For example, 2000-01-31, 01/31/2000, or January 31, 2000."
            )
        if self.dob and not self.phone:
            return "What is your phone number? For example, 555-0199 or 5550199."
        return (
            "What is your phone number and date of birth? "
            "For example, 555-0199 and 2000-01-31."
        )

    def _lookup(self):
        if not self.specialty:
            self._state = "need_specialty"
            return "Which specialty? You can say primary care or dermatology."
        self._state = "lookup"
        query = {"specialty": self.specialty}
        if self.location:
            query["location"] = self.location
        result = self._get("/providers", query)
        if _down(result):
            return self._handoff("api_failure", "The scheduling system is unavailable.")
        if result.get("status") != 200:
            return self._handoff("other", "The scheduling system rejected that request.")
        providers = result.get("body", {}).get("providers")
        if not isinstance(providers, list):
            return self._handoff("api_failure", "The scheduling system is unavailable.")
        kept = []
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            provider_id = provider.get("providerId")
            if isinstance(provider_id, str) and provider_id:
                self.providers[provider_id] = plain(provider.get("name") or provider_id)
            kept.append(provider)
        if not kept:
            return "No providers matched that search."
        return _format_providers(kept)

    def _search(self, text=""):
        self._state = "identify"
        result = self._get("/patients/search", {"phone": self.phone, "dob": self.dob})
        if _down(result):
            return self._handoff("api_failure", "The scheduling system is unavailable.")
        if result.get("status") != 200:
            return self._handoff("other", "The scheduling system rejected that request.")
        matches = result.get("body", {}).get("matches")
        if not isinstance(matches, list):
            return self._handoff("api_failure", "The scheduling system is unavailable.")
        if len(matches) == 0:
            self.identity_misses += 1
            self.phone = self.dob = ""
            self._clear_patient()
            self._state = "no_patient_match"
            if self.identity_misses >= 2:
                return self._handoff(
                    "identity_unclear",
                    "I couldn't find a patient with those details.",
                )
            return (
                "I couldn't find a patient with those details. You can give a "
                "different phone and date of birth, or I can hand this to a person."
            )
        if len(matches) > 1:
            self._clear_patient()
            self._state = "verify_zip"
            self.candidates = [match for match in matches if isinstance(match, dict)]
            if ZIP.findall(text):
                return self._resolve_zip(text)
            return "What is your ZIP code?"
        if not self._accept(matches[0]):
            return self._handoff(
                "identity_unclear",
                "I couldn't find a patient with those details.",
            )
        self.identity_misses = 0
        return self._after_identified()

    def _lookup_appointments(self, text):
        if not self.patient_id:
            if not (self.phone and self.dob):
                return self._ask_identity()
            return self._search(text)
        return self._appointments()

    def _resolve_zip(self, text):
        if _give_up(text):
            self.candidates = []
            return self._handoff(
                "identity_unclear",
                "I couldn't find a patient with those details.",
            )
        found = ZIP.findall(text)
        if not found:
            phone, dob = identity_fields(text)
            if phone and dob:
                kind = "That message has a phone number and a date."
            elif phone:
                kind = "That looks like a phone number."
            elif dob:
                kind = "That looks like a date."
            else:
                kind = ""
            if kind:
                return (
                    kind + " I still need your ZIP code. "
                    "Please use 5 digits, such as 12345."
                )
            if re.search(r"\d{3,}", text):
                return "That ZIP code doesn't look right. Please use 5 digits, such as 12345."
            return "What is your ZIP code?"
        chosen = [match for match in self.candidates if len(found) == 1 and match.get("zipCode") == found[0][:5]]
        self.candidates = []
        if len(chosen) != 1 or not self._accept(chosen[0]):
            return self._handoff(
                "identity_unclear",
                "I still can't tell which record is yours.",
            )
        return self._after_identified()

    def _after_identified(self):
        if self.goal == "appointment_lookup":
            return self._appointments()
        if not self.specialty:
            self._state = "need_specialty"
            return "Which specialty? You can say primary care or dermatology."
        return self._availability()

    def _appointments(self):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", self.patient_id or ""):
            return self._handoff("identity_unclear", "I couldn't find a patient with those details.")
        self._state = "appointments"
        result = self._get("/patients/%s/appointments" % self.patient_id)
        if _down(result):
            return self._handoff("api_failure", "The scheduling system is unavailable.")
        if result.get("status") != 200:
            return self._handoff("other", "The scheduling system rejected that request.")
        rows = result.get("body", {}).get("appointments")
        if not isinstance(rows, list):
            return self._handoff("api_failure", "The scheduling system is unavailable.")
        if not rows:
            return self._no_appointments()
        lines = []
        for row in rows:
            if not isinstance(row, dict) or row.get("patientId") != self.patient_id:
                return self._handoff("api_failure", "The appointment list could not be verified.")
            lines.append(self._appointment_sentence(row))
        if len(lines) == 1:
            return lines[0]
        return "Here are your appointments:\n" + "\n".join(
            "%d. %s" % (index, line) for index, line in enumerate(lines, 1)
        )

    def _no_appointments(self):
        first = self.patient_name.split()[0] if self.patient_name else ""
        if first:
            return "I couldn't find any appointments for you, %s." % first
        return "I couldn't find any appointments for you."

    def _availability(self):
        self._state = "availability"
        query = {"patientId": self.patient_id, "specialty": self.specialty}
        if self.location:
            query["location"] = self.location
        if self.visit_date:
            query.update(startDate=self.visit_date, endDate=self.visit_date)
        result = self._get("/availability", query)
        if _down(result):
            return self._handoff("api_failure", "The scheduling system is unavailable.")
        if result.get("status") != 200:
            return self._handoff("other", "The scheduling system rejected that request.")
        slots = result.get("body", {}).get("slots")
        if not isinstance(slots, list):
            return self._handoff("api_failure", "The scheduling system is unavailable.")
        self.slots = []
        for slot in slots:
            if not isinstance(slot, dict) or slot.get("available") is not True:
                continue
            if not all(isinstance(slot.get(k), str) and slot[k] for k in
                       ("slotId", "providerId", "startTime", "specialty", "location")):
                return self._handoff("api_failure", "An appointment option could not be verified.")
            try:
                start = datetime.fromisoformat(slot["startTime"])
                if start.utcoffset() is None:
                    raise ValueError("missing offset")
            except ValueError:
                return self._handoff("api_failure", "The appointment time could not be verified.")
            if self.visit_date and start.date().isoformat() != self.visit_date:
                continue
            if slot["specialty"] == self.specialty and (not self.location or slot["location"] == self.location):
                if slot["slotId"] not in self.unavailable_slots:
                    self.slots.append(slot)
        if not self.slots:
            self._state = "no_availability"
            if self.visit_date:
                return ("Nothing is available on %s for that search. You can try another date, "
                        "ask for any date, or ask for a person." % self.visit_date)
            return (
                "Nothing is available for that search. You can give another "
                "specialty or location, or I can hand this to a person."
            )
        if any(slot.get("providerId") not in self.providers for slot in self.slots):
            # The public directory is small. Fetch it once to label the offered times.
            directory = self._get("/providers")
            rows = directory.get("body", {}).get("providers")
            if directory["status"] != 200 or not isinstance(rows, list):
                return self._handoff("api_failure", "The provider directory is unavailable.")
            for row in rows:
                if isinstance(row, dict) and row.get("providerId") and row.get("name"):
                    self.providers[row["providerId"]] = plain(row["name"])
        return self._show_slots()

    def _post_booking(self):
        slot_id = "" if not self.pending else self.pending.get("slotId")
        known = {slot.get("slotId") for slot in self.slots}
        if not self.patient_id or slot_id not in known:
            self._clear_schedule()
            return "That selection has expired. Please start a new search."
        self._state = "confirm"
        result = self._post(
            "/appointments",
            {"patientId": self.patient_id, "slotId": slot_id, "confirmed": True},
        )
        status = result.get("status")
        appointment = result.get("body", {}).get("appointment")
        if status == 201 and self._matches_receipt(appointment):
            self.unavailable_slots.add(slot_id)
            self._clear_schedule()
            self.visit_date = ""
            self._state = "booked"
            return "Booked. Your %s visit in %s is scheduled for %s. Reference %s." % (
                plain(appointment["specialty"]).replace("_", " "),
                plain(appointment["location"]).title(),
                _when(appointment["startTime"]),
                plain(appointment["appointmentId"]),
            )
        if status == 409:
            self.unavailable_slots.add(slot_id)
            self._clear_schedule()
            return "That time was just taken. " + self._availability()
        if status == 400:
            return self._handoff("other", "The scheduling system rejected that booking.")
        # A lost or inconsistent receipt does not tell us whether the write committed.
        return self._handoff(
            "api_failure",
            "I couldn't verify whether the appointment was booked. Please check with the office before booking again.",
            summary="Booking outcome unknown. Verify the existing appointment before trying again.",
        )

    def _matches_receipt(self, appointment):
        if not isinstance(appointment, dict) or not appointment.get("appointmentId"):
            return False
        return (
            appointment.get("status") == "scheduled" and appointment.get("patientId") == self.patient_id
            and all(appointment.get(key) == self.pending[key] for key in ("providerId", "specialty", "location", "startTime"))
        )

    def _requested_handoff(self):
        reason, lead = _HANDOFF_OFFERS.get(self._state, ("user_requested", "You asked for a person."))
        return self._handoff(reason, lead)

    def _handoff(self, reason, lead, summary=None):
        self.pending = None
        self._state = "handoff"
        body = {"reason": reason, "summary": summary or _SUMMARIES[reason]}
        if self.patient_id and reason not in ("identity_unclear", "medical_advice"):
            body["patientId"] = self.patient_id
        result = self._post("/handoffs", body, escalation=reason)
        receipt = result.get("body", {})
        if result["status"] == 201 and receipt.get("handoffId") and receipt.get("status") == "queued":
            self._state = "handoff_queued"
            self.terminal_reply = lead + " A person will review your request. Handoff %s is queued." % plain(receipt["handoffId"])
        else:
            self._state = "handoff_unconfirmed"
            self.terminal_reply = lead + " I couldn't confirm a handoff to a person. Please contact the office directly."
        return self.terminal_reply

    def _accept(self, match):
        if not isinstance(match, dict):
            return False
        patient_id = match.get("patientId")
        if not isinstance(patient_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", patient_id):
            return False
        self.identity_misses = 0
        self.patient_id = patient_id
        self.patient_name = (plain(match.get("firstName", "")) + " " + plain(match.get("lastName", ""))).strip()
        return True

    def _choose(self, text, model):
        compact = text.strip().lower().rstrip(".!")
        choice = compact if compact.isascii() and compact.isdigit() else model.get("slot_choice", "")
        if compact in ("first", "earliest", "the first", "the first one", "first one"):
            choice = 1
        if re.fullmatch(r"[0-9]{1,3}", str(choice)):
            index = int(choice) - 1
            return self.slots[index] if 0 <= index < len(self.slots) else None
        return next((s for s in self.slots if s["slotId"] in (compact, choice)), None)

    def _appointment_sentence(self, row):
        who = self.providers.get(row.get("providerId", ""), "")
        specialty = plain(row.get("specialty", "")).replace("_", " ") or "care"
        location = plain(row.get("location", "")).title()
        when = _when(row["startTime"]) if row.get("startTime") else ""
        status = plain(row.get("status", "")).replace("_", " ")
        ref = plain(row.get("appointmentId", ""))
        with_whom = " with %s" % who if who and not str(who).startswith("prov_") else ""
        place = " in %s" % location if location else ""
        moment = " on %s" % when if when else ""
        return "You have a %s %s visit%s%s%s. Reference %s." % (
            status, specialty, with_whom, place, moment, ref,
        )

    def _describe(self, slot):
        who = self.providers.get(slot["providerId"], "")
        if not who or str(who).startswith("prov_"):
            who = ""
        where = plain(slot.get("location", "")).title()
        when = _when(slot["startTime"])
        if who and where:
            return "%s with %s in %s" % (when, who, where)
        if who:
            return "%s with %s" % (when, who)
        if where:
            return "%s in %s" % (when, where)
        return when

    def _show_slots(self):
        self._state = "choose_slot"
        lines = ["Here are the available times:"]
        for index, slot in enumerate(self.slots, 1):
            lines.append("%d. %s." % (index, self._describe(slot)))
        lines.append("Reply with a number to choose one.")
        return "\n".join(lines)

    def _ask_confirm(self, slot):
        self._state = "confirm"
        name = self.patient_name or "you"
        return "Book %s for %s? Reply yes or no." % (self._describe(slot), name)

    def _get(self, path, query=None):
        result = self.client.get(path, query)
        self._note("GET", path, result)
        return result

    def _post(self, path, body, escalation=""):
        result = self.client.post(path, body)
        self._note("POST", path, result, escalation)
        return result

    def _note(self, method, path, result, escalation=""):
        code = ""
        status = result.get("status", "")
        if status not in (200, 201):
            body = result.get("body")
            raw = body.get("code") if isinstance(body, dict) else ""
            if isinstance(raw, str) and re.fullmatch(r"[a-z0-9_]{1,40}", raw):
                code = raw
        if path.startswith("/patients/") and path.endswith("/appointments"):
            path = "/patients/{patientId}/appointments"
        self._event(method, path, status, result.get("duration_ms", 0), code, escalation)

    def _event(self, method="", path="", status="", duration_ms=0, error_code="", escalation_reason=""):
        self.events.append(
            {
                "intent": self.goal,
                "state": self._state,
                "method": method,
                "path": path,
                "status": status,
                "duration_ms": duration_ms,
                "escalation_reason": escalation_reason,
                "error_code": error_code,
            }
        )

    def _update_identity(self, phone, dob, dispute):
        new_phone = phone or ("" if dispute in ("phone", "both") else self.phone)
        if dob is None:
            dob = "" if dispute in ("birth", "both") else self.dob
        if dispute or (new_phone, dob) != (self.phone, self.dob):
            self._clear_patient()
            self.phone, self.dob = new_phone, dob
            if self.goal in ("book", "appointment_lookup"):
                self._state = "identify"

    def _clear_patient(self):
        self.patient_id = ""
        self.patient_name = ""
        self.candidates = []
        self._clear_schedule()

    def _clear_schedule(self):
        self.slots = []
        self.pending = None
        self._state = "idle"


def _ms(started):
    return round((time.monotonic() - started) * 1000, 1)


def _when(value):
    moment = datetime.fromisoformat(value)
    offset = moment.strftime("%z")
    return "%s at %s UTC%s:%s" % (
        moment.strftime("%a, %b %d"), moment.strftime("%I:%M %p").lstrip("0"), offset[:3], offset[3:]
    )


def plain(value):
    text = "".join(ch for ch in str(value) if ch.isprintable() and ch not in "\r\n\t")
    return text.strip()[:300]


def _mentioned_specialty(text):
    lowered = text.lower()
    if "primary care" in lowered or "primary_care" in lowered:
        return "primary_care"
    if "dermatology" in lowered:
        return "dermatology"
    return ""


def _medical(text):
    lowered = text.lower()
    if re.search(r"\b(?:is|are) (?:this|that|my|these|it)\b[^.!?\n]*\bcontagious\b\s*(?:[?!.]|$)", lowered):
        return True
    return any(phrase in lowered for phrase in _MEDICAL)


def _give_up(text):
    lowered = text.lower()
    return any(phrase in lowered for phrase in ("don't have", "do not have", "no other", "don't know", "do not know"))


def _wants_person(text):
    compact = text.strip().lower().rstrip(".!?")
    if re.fullmatch(r"(?:please )?hand (?:it|this) off(?: to (?:a person|a human|the front desk))?(?:,? please)?", compact):
        return True
    if re.fullmatch(r"(?:can|could|may) i (?:please )?(?:talk|speak) (?:to|with) "
                    r"(?:(?:the|a|an) )?(?:front desk|receptionist|person|human|agent|representative)", compact):
        return True
    return compact in {
        "human", "a human", "agent", "a person", "representative", "operator",
        "i want a human", "i want a person", "talk to someone",
    }


def _confirmation(text):
    compact = text.strip().lower().rstrip(".!").strip()
    if compact in YES:
        return "yes"
    if compact in NO:
        return "no"
    return ""


def _format_providers(providers):
    count = len(providers)
    noun = "provider" if count == 1 else "providers"
    lines = ["I found %d %s:" % (count, noun)]
    for index, provider in enumerate(providers, 1):
        lines.append("%d. %s" % (index, _provider_sentence(provider)))
    return "\n".join(lines)


def _provider_sentence(provider):
    name = plain(provider.get("name", "This provider"))
    specialty = plain(provider.get("specialty", "")).replace("_", " ")
    locations = [plain(item).title() for item in provider.get("locations") or [] if item]
    modalities = [_visit_type(item) for item in provider.get("modalities") or []]
    sentence = name
    if specialty:
        sentence += " offers %s" % specialty
    if locations:
        sentence += " in %s" % _join_words(locations)
    if modalities:
        sentence += ", %s" % _join_words(modalities)
    return sentence + "."


def _visit_type(value):
    text = plain(value).replace("_", " ")
    if text == "in person":
        return "in person"
    if text == "virtual":
        return "by video"
    return text


def _join_words(items):
    items = [item for item in items if item]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return "%s and %s" % (items[0], items[1])
    return "%s, and %s" % (", ".join(items[:-1]), items[-1])


def _down(result):
    status = result.get("status")
    return status == 0 or (isinstance(status, int) and status >= 500)
