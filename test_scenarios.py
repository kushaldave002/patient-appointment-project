"""End-to-end scenario evaluations with the supplied HTTP handler and synthetic store."""

import importlib.util
import io
import json
import unittest
from contextlib import contextmanager
from pathlib import Path

from assistant import run
from model import redact_identifiers
from scheduling import SchedulingClient
from test_scheduling import http_server
from workflow import Workflow

spec = importlib.util.spec_from_file_location("assignment_mock", Path(__file__).parent / "mock-api" / "server.py")
mock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mock)

BOOK = {"intent": "book", "specialty": "primary_care", "location": "downtown"}
PROVIDERS = {**BOOK, "intent": "provider_lookup"}
IDENTITY = "5550101 04/12/1985"


@contextmanager
def mock_api(outage=False, lose_booking_receipt=False):
    class Handler(mock.Handler):
        store = mock.Store()
        requests = []

        def route(self, method, parsed):
            if outage:
                self.headers["X-Mock-Scenario"] = "api_failure"
            return super().route(method, parsed)

        def respond(self, status, payload):
            self.requests.append((self.command, self.path.partition("?")[0], status))
            if lose_booking_receipt and self.path == "/appointments" and status == 201:
                self.close_connection = True
                return
            super().respond(status, payload)

        def audit(self, method, path, status, ms):
            pass

    with http_server(Handler) as server:
        yield SchedulingClient("http://127.0.0.1:%d" % server.server_address[1]), Handler


def interpreted(*results):
    replies = iter(results)
    def extract(text, context):
        return next(replies)
    return extract


class ScenarioTest(unittest.TestCase):
    def test_accepting_a_handoff_uses_the_offered_reason_once(self):
        cases = [
            (BOOK, "I'd like to book a primary care appointment downtown.",
             ["1990-01-01", "555-9999"], "identity_unclear"),
            ({"intent": "book", "specialty": "dermatology", "location": "lakeside"},
             "I'd like to book a dermatology appointment at Lakeside.",
             ["555-0102 1992-06-03"], "no_availability"),
        ]
        for goal, request, identifiers, reason in cases:
            for acceptance in ("hand it off", "yes", "could someone take over for me?"):
                with self.subTest(reason=reason, acceptance=acceptance), mock_api() as (client, api):
                    flow = Workflow(client, interpreted(goal, {"intent": "human"}))
                    flow.turn(request)
                    for message in identifiers:
                        offer = flow.turn(message)
                    self.assertIn("person", offer)
                    api.requests.clear()
                    receipt = flow.turn(acceptance)
                    self.assertIn("queued", receipt)
                    self.assertEqual(flow.turn(acceptance), receipt)
                    self.assertEqual(api.requests, [("POST", "/handoffs", 201)])
                    self.assertEqual([r["reason"] for r in api.store.handoffs], [reason])
                    if reason == "identity_unclear":
                        self.assertFalse(api.store.handoffs[0].get("patientId"))

    def test_disputed_birthday_blocks_the_old_appointment_lookup(self):
        lookup = {"intent": "appointment_lookup"}
        message = ("I gave you the wrong birthday earlier. The correct date is June 3, 1992. "
                   "Can you check my appointments?")
        for classification in (lookup, {**lookup, "date_role": "birth"}):
            with self.subTest(classification=classification), mock_api() as (client, api):
                flow = Workflow(client, interpreted(lookup, classification))
                flow.turn("Look up my existing appointments.")
                self.assertIn("appt_3001", flow.turn(IDENTITY))
                api.requests.clear()
                reply = flow.turn(message)
                self.assertNotIn("appt_3001", reply)
                self.assertFalse(flow.patient_id)
                self.assertEqual(api.requests, [("GET", "/patients/search", 200)])

    def test_disputing_a_birth_date_without_a_replacement_revokes_consent(self):
        for message, classification in [
            ("My birthday was wrong.", {"intent": "continue"}),
            ("My DOB is February 30, 1992.", {"intent": "continue"}),
            ("I made a typo when I told you when I was born.",
             {"intent": "continue", "identity_correction": "birth"}),
        ]:
            with self.subTest(message=message), mock_api() as (client, api):
                flow = Workflow(client, interpreted(BOOK, classification))
                flow.turn("Book primary care downtown")
                flow.turn(IDENTITY)
                flow.turn("1")
                api.requests.clear()
                self.assertIn("date of birth", flow.turn(message))
                self.assertFalse(flow.patient_id)
                self.assertIsNone(flow.pending)
                flow.turn("yes")
                self.assertEqual(api.requests, [])

    def test_corrected_identifiers_replace_duplicate_matches_before_zip(self):
        lookup = {"intent": "appointment_lookup"}
        for correction in ("gave you the wrong details. My phone is 555-0102 and my DOB is 1992-06-03.",
                           "555-0102 1992-06-03", "70115, actually use 555-0102 and DOB 1992-06-03"):
            with self.subTest(correction=correction), mock_api() as (client, api):
                flow = Workflow(client, interpreted(lookup, {"intent": "continue"}))
                flow.turn("Look up my existing appointments.")
                flow.turn("555-0130")
                self.assertIn("ZIP", flow.turn("1978-09-22"))
                api.requests.clear()
                reply = flow.turn(correction)
                self.assertIn("appt_3002", reply)
                self.assertFalse(flow.candidates)
                self.assertEqual(api.requests, [("GET", "/patients/search", 200),
                                               ("GET", "/patients/pat_1002/appointments", 200)])

    def test_classifier_can_identify_a_birth_date_but_cannot_supply_its_value(self):
        lookup = {"intent": "appointment_lookup"}
        correction = {"intent": "continue", "date_role": "birth", "dob": "1985-04-12"}
        with mock_api() as (client, api):
            flow = Workflow(client, interpreted(lookup, correction))
            flow.turn("Look up my appointments")
            flow.turn(IDENTITY)
            api.requests.clear()
            reply = flow.turn("I was actually born a little later, on June 3, 1992.")
            self.assertIn("couldn't find", reply)
            self.assertFalse(flow.patient_id)
            self.assertEqual(api.requests, [("GET", "/patients/search", 200)])

    def test_policy_interrupts_duplicate_patient_verification(self):
        cases = [
            ("Can I speak with the front desk?", "human", "user_requested"),
            ("Is this rash contagious?", "medical_advice", "medical_advice"),
            ("70115, could someone call me back?", "human", "user_requested"),
            ("Could this spread to my kids?", "medical_advice", "medical_advice"),
        ]
        for message, intent, reason in cases:
            with self.subTest(message=message), mock_api() as (client, api):
                flow = Workflow(client, interpreted(BOOK, {"intent": intent}))
                flow.turn("Book primary care downtown")
                self.assertIn("ZIP", flow.turn("555-0130 1978-09-22"))
                self.assertIn("queued", flow.turn(message))
                self.assertEqual([h["reason"] for h in api.store.handoffs], [reason])
                self.assertFalse(api.store.handoffs[0].get("patientId"))
                self.assertFalse(flow.patient_id)
                self.assertEqual([path for _, path, _ in api.requests], ["/patients/search", "/handoffs"])

    def test_location_correction_replaces_pending_downtown_slot(self):
        with mock_api() as (client, api):
            flow = Workflow(client, interpreted(BOOK, {"intent": "continue", "location": "uptown"}))
            flow.turn("Book primary care downtown")
            flow.turn(IDENTITY)
            flow.turn("1")
            reply = flow.turn("Downtown doesn’t work. Can we do Uptown instead?")
            self.assertIn("Uptown", reply)
            self.assertNotIn("Downtown", reply)
            self.assertEqual({slot["location"] for slot in flow.slots}, {"uptown"})
            self.assertIsNone(flow.pending)
            flow.turn("yes")
            self.assertFalse(any(path == "/appointments" for _, path, _ in api.requests))

    def test_family_medicine_directory_lookup(self):
        messages = [
            "Which primary care providers practice family medicine downtown?",
            "Which downtown primary care doctors offer medication reviews?",
        ]
        for message in messages:
            with self.subTest(message=message), mock_api() as (client, api):
                flow = Workflow(client, interpreted(PROVIDERS))
                reply = flow.turn(message)
                self.assertIn("Dr. Elena Brooks", reply)
                self.assertIn("Dr. Marcus King", reply)
                self.assertEqual(api.requests, [("GET", "/providers", 200)])

    def test_visit_date_and_birth_date_in_one_turn_remain_separate(self):
        with mock_api() as (client, api):
            slot_day = mock.start_time(api.store.slots[0])[:10]
            flow = Workflow(client, interpreted(BOOK, {"intent": "continue", "date_role": "any_date"}))
            flow.turn("Book primary care downtown on %s. My DOB is 1985-04-12 and phone is 555-0101." % slot_day)
            self.assertEqual(flow.patient_id, "pat_1001")
            self.assertEqual(flow.dob, "1985-04-12")
            self.assertEqual({s["startTime"][:10] for s in flow.slots}, {slot_day})
            flow.turn("Actually, any date works for me")
            self.assertGreater(len({s["startTime"][:10] for s in flow.slots}), 1)
            self.assertEqual(sum(path == "/patients/search" for _, path, _ in api.requests), 1)

    def test_explicit_birth_date_correction_reverifies_and_clears_selection(self):
        with mock_api() as (client, api):
            flow = Workflow(client, interpreted(BOOK, {"intent": "continue"}))
            flow.turn("Book a primary care appointment downtown")
            flow.turn(IDENTITY)
            flow.turn("1")
            reply = flow.turn("Actually my date of birth is April 14, 1985.")
            self.assertIn("couldn't find a patient", reply)
            self.assertFalse(flow.patient_id)
            self.assertIsNone(flow.pending)
            self.assertEqual(sum(path == "/patients/search" for _, path, _ in api.requests), 2)

    def test_cli_logs_provider_lookup_booking_and_existing_lookup(self):
        output, log = io.StringIO(), io.StringIO()
        with mock_api() as (client, api):
            flow = Workflow(client, interpreted(PROVIDERS, BOOK, {"intent": "appointment_lookup"}))
            inputs = ["Who sees primary care patients downtown?", "I'd like to see one of them",
                      IDENTITY, "1", "yes?", "2", "yes", "Look up my booked appointment", "quit"]
            run(io.StringIO("\n".join(inputs)), output, flow, log)
            receipt = api.store.appointments[-1]
            self.assertEqual(receipt["providerId"], "prov_2002")
            self.assertEqual(len(api.store.appointments), 3)
        self.assertIn("Dr. Elena Brooks", output.getvalue())
        self.assertIn(receipt["appointmentId"], output.getvalue())
        events = [json.loads(line) for line in log.getvalue().splitlines()]
        self.assertEqual(sum(e["method"] == "MODEL" for e in events), 3)
        self.assertIn("/patients/{patientId}/appointments", [e["path"] for e in events])
        self.assertEqual([e["status"] for e in events if e["path"] == "/appointments"], [201])
        self.assertTrue(any(e["state"] == "booked" and e["method"] == "TURN" for e in events))
        for sensitive in ("pat_1001", "5550101", "04/12/1985", "Maya"):
            self.assertNotIn(sensitive, log.getvalue())

    def test_conflict_filters_failed_slot_and_success_refreshes_next_search(self):
        with mock_api() as (client, api):
            flow = Workflow(client, interpreted(BOOK, BOOK))
            flow.turn("book a visit downtown")
            flow.turn(IDENTITY)
            flow.turn("3")
            after_conflict = flow.turn("yes")
            self.assertIn("taken", after_conflict)
            self.assertEqual([s["slotId"] for s in flow.slots], ["slot_4001", "slot_4002"])
            flow.turn("1")
            self.assertIn("Booked", flow.turn("yes"))
            after_success = flow.turn("I'd like to book another visit")
            self.assertIn("Dr. Marcus King", after_success)
            self.assertEqual([s["slotId"] for s in flow.slots], ["slot_4002"])

    def test_natural_language_selection_uses_only_public_options_as_context(self):
        contexts = []
        replies = iter([BOOK, {"intent": "continue", "slot_choice": "2"}])
        def extract(text, context):
            contexts.append(context)
            return next(replies)
        with mock_api() as (client, api):
            flow = Workflow(client, extract)
            flow.turn("I need a primary care visit downtown")
            flow.turn(IDENTITY)
            flow.turn("1")
            question = flow.turn("Actually, could I see Dr. King instead?")
            self.assertIn("Dr. Marcus King", question)
            self.assertEqual(len(api.store.appointments), 2)
            flow.turn("yes")
            self.assertEqual(api.store.appointments[-1]["providerId"], "prov_2002")
        self.assertEqual(contexts[-1]["options"][1]["number"], 2)
        public = json.dumps(contexts[-1])
        self.assertIn("Dr. Marcus King", public)
        for private in ("Maya", "pat_1001", "1985-04-12", "555-0101"):
            self.assertNotIn(private, public)

    def test_uncertain_consent_and_invalid_change_never_authorize_write(self):
        with mock_api() as (client, api):
            flow = Workflow(client, interpreted(BOOK))
            flow.turn("book downtown")
            flow.turn(IDENTITY)
            flow.turn("1")
            for reply in ("yes?", "ok", "99", "yes"):
                flow.turn(reply)
            self.assertEqual(len(api.store.appointments), 2)
            flow.turn("2")
            flow.turn("no")
            flow.turn("yes")
            self.assertEqual(len(api.store.appointments), 2)

    def test_lost_receipt_reports_uncertainty_even_when_write_committed(self):
        with mock_api(lose_booking_receipt=True) as (client, api):
            flow = Workflow(client, interpreted(BOOK))
            flow.turn("book downtown")
            flow.turn(IDENTITY)
            flow.turn("1")
            reply = flow.turn("yes")
            self.assertIn("couldn't verify whether", reply)
            self.assertIn("queued", reply)
            self.assertEqual(flow.turn("try again"), reply)
            self.assertEqual(len(api.store.appointments), 3)
            self.assertEqual(len(api.store.handoffs), 1)
            self.assertEqual(api.store.handoffs[0]["reason"], "api_failure")
            self.assertIn("outcome unknown", api.store.handoffs[0]["summary"])

    def test_failed_handoff_is_still_unconfirmed_on_next_turn(self):
        with mock_api(outage=True) as (client, api):
            flow = Workflow(client, interpreted(PROVIDERS))
            reply = flow.turn("Find a provider")
            self.assertIn("contact the office", reply)
            self.assertNotIn("queued", reply)
            self.assertEqual(flow.turn("Did someone get it?"), reply)
            self.assertEqual(api.store.handoffs, [])

    def test_unsupported_change_is_handed_off_even_with_a_known_location(self):
        with mock_api() as (client, api):
            flow = Workflow(client, interpreted(BOOK, {"intent": "unsupported", "location": "downtown"}))
            flow.turn("I'd like a primary care appointment")
            flow.turn("Actually I want a surgical procedure downtown")
            self.assertEqual(api.store.handoffs[0]["reason"], "unsupported_request")
            self.assertEqual(len(api.store.appointments), 2)

    def test_duplicate_zip_stays_local_and_mixed_identifiers_are_masked(self):
        seen = []
        def extract(text, context):
            seen.append(text)
            return {"intent": "appointment_lookup"}
        with mock_api() as (client, api):
            flow = Workflow(client, extract)
            flow.turn("Look up my booked appointment, phone 5550130, DOB 09/22/1978")
            self.assertEqual(len(flow.candidates), 2)
            flow.turn("70115")
            self.assertEqual(flow.patient_id, "pat_1003")
        self.assertEqual(seen, ["Look up my booked appointment, phone [phone], DOB [dob]"])
        self.assertEqual(redact_identifiers("April 12, 1985, (202) 555-0101, 70115-1234"),
                         "[date], [phone], [zip]")
        from identity import identity_fields
        self.assertEqual(identity_fields("April 12, 1985")[1], "1985-04-12")
        self.assertEqual(identity_fields("04/12/1985")[1], "1985-04-12")
        self.assertEqual(identity_fields("09-22-1978"), ("", "1978-09-22"))
        self.assertEqual(identity_fields("1978/09/22"), ("", "1978-09-22"))
        self.assertEqual(identity_fields("1978\\09\\22"), ("", "1978-09-22"))
        self.assertEqual(redact_identifiers("09-22-1978"), "[date]")
        self.assertEqual(redact_identifiers("1978\\09\\22"), "[date]")

    def test_identity_keeps_dates_phones_and_other_numbers_apart(self):
        from identity import identity_fields
        self.assertEqual(identity_fields("555-0101"), ("555-0101", ""))
        self.assertEqual(identity_fields("5550101"), ("555-0101", ""))
        self.assertEqual(identity_fields("555.0101"), ("555-0101", ""))
        self.assertEqual(identity_fields("(555) 0101"), ("555-0101", ""))
        self.assertEqual(identity_fields("1985-04-12"), ("", "1985-04-12"))
        self.assertEqual(identity_fields("12 April 1985"), ("", "1985-04-12"))
        self.assertEqual(identity_fields("555-0101 on April 12, 1985"), ("555-0101", "1985-04-12"))
        self.assertEqual(identity_fields("70115"), ("", ""))
        self.assertEqual(identity_fields("2"), ("", ""))
        self.assertNotEqual(identity_fields("(202) 555-0101")[0], "555-0101")


if __name__ == "__main__":
    unittest.main()
