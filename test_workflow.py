import json
import unittest

from model import ModelError
from workflow import MissingKey, Workflow


PATIENT = {
    "patientId": "pat_1001",
    "firstName": "Maya",
    "lastName": "Chen",
    "dateOfBirth": "1985-04-12",
    "phone": "555-0101",
    "zipCode": "70112",
}
PATIENT_2 = {
    "patientId": "pat_1002",
    "firstName": "Jordan",
    "lastName": "Lee",
    "dateOfBirth": "1992-06-03",
    "phone": "555-0102",
    "zipCode": "70118",
}
SLOT = {
    "slotId": "slot_4001",
    "providerId": "prov_2001",
    "specialty": "primary_care",
    "location": "downtown",
    "startTime": "2026-12-01T09:00:00-05:00",
    "available": True,
}
SLOT_2 = {
    "slotId": "slot_4002",
    "providerId": "prov_2002",
    "specialty": "primary_care",
    "location": "downtown",
    "startTime": "2026-12-01T11:00:00-05:00",
    "available": True,
}
PROVIDERS = [
    {
        "providerId": "prov_2001",
        "name": "Dr. Elena Brooks",
        "specialty": "primary_care",
        "locations": ["downtown", "uptown"],
        "modalities": ["in_person", "virtual"],
    },
    {
        "providerId": "prov_2002",
        "name": "Dr. Marcus King",
        "specialty": "primary_care",
        "locations": ["downtown"],
        "modalities": ["in_person"],
    },
]
APPOINTMENT = {
    "appointmentId": "appt_9002",
    "patientId": "pat_1001",
    "providerId": "prov_2001",
    "specialty": "primary_care",
    "location": "downtown",
    "startTime": SLOT["startTime"],
    "status": "scheduled",
}


BOOK = {"intent": "book", "specialty": "primary_care", "location": "downtown"}
LOOKUP = {"intent": "provider_lookup", "specialty": "primary_care", "location": "downtown"}

class FakeClient:
    def __init__(self):
        self.calls = []
        self._queued = []

    def push(self, status, body):
        self._queued.append({"status": status, "body": body, "duration_ms": 1.0})

    def get(self, path, query=None):
        self.calls.append(("GET", path, query))
        return self._queued.pop(0)

    def post(self, path, body):
        self.calls.append(("POST", path, body))
        return self._queued.pop(0)


def scripted(*items):
    queue = list(items)

    def extract(_text, _context):
        if queue:
            return queue.pop(0)
        return {}

    return extract


def posts(client, path):
    return [call for call in client.calls if call[0] == "POST" and call[1] == path]


def gets(client, path):
    return [call for call in client.calls if call[0] == "GET" and call[1] == path]


class WorkflowTest(unittest.TestCase):
    def test_visit_date_refines_verified_search_and_expires_old_confirmation(self):
        client = FakeClient()
        dec_2 = {**SLOT_2, "startTime": "2026-12-02T11:00:00-05:00"}
        client.push(200, {"matches": [PATIENT]})
        client.push(200, {"slots": [SLOT, dec_2]})
        client.push(200, {"providers": PROVIDERS})
        client.push(200, {"slots": [SLOT, dec_2]})
        workflow = Workflow(client, scripted(BOOK, {"intent": "continue"}))
        workflow.turn("Book primary care downtown")
        workflow.turn("555-0101 1985-04-12")
        workflow.turn("1")
        reply = workflow.turn("Do you have any visits on December 2, 2026?")
        self.assertEqual(workflow.patient_id, PATIENT["patientId"])
        self.assertEqual(workflow.dob, PATIENT["dateOfBirth"])
        self.assertEqual(len(gets(client, "/patients/search")), 1)
        self.assertEqual(gets(client, "/availability")[-1][2], {
            "patientId": PATIENT["patientId"], "specialty": "primary_care", "location": "downtown",
            "startDate": "2026-12-02", "endDate": "2026-12-02",
        })
        self.assertIn("Dec 02", reply)
        self.assertNotIn("Dec 01", reply)
        self.assertIsNone(workflow.pending)
        workflow.turn("yes")
        self.assertEqual(posts(client, "/appointments"), [])

    def test_provider_lookup_uses_api_names_and_skips_identity(self):
        client = FakeClient()
        client.push(200, {"providers": PROVIDERS})
        workflow = Workflow(client, scripted(LOOKUP))
        reply = workflow.turn("Which primary care providers are downtown?")
        self.assertIn("Dr. Elena Brooks", reply)
        self.assertIn("Dr. Marcus King", reply)
        self.assertNotIn("phone", reply.lower())
        self.assertEqual(gets(client, "/providers")[0][2]["specialty"], "primary_care")
        self.assertEqual(gets(client, "/providers")[0][2]["location"], "downtown")
        self.assertEqual(gets(client, "/patients/search"), [])

    def test_booking_posts_only_after_explicit_yes(self):
        client = FakeClient()
        client.push(200, {"matches": [PATIENT]})
        client.push(200, {"slots": [SLOT, SLOT_2]})
        client.push(200, {"providers": PROVIDERS})
        client.push(201, {"appointment": APPOINTMENT})
        workflow = Workflow(client, scripted(BOOK))
        asked = workflow.turn("I want to book a primary care appointment downtown")
        self.assertIn("phone", asked.lower())
        self.assertEqual(client.calls, [])
        shown = workflow.turn("555-0101 1985-04-12")
        self.assertIn("Dr. Elena Brooks", shown)
        self.assertNotIn("appt_9002", shown)
        confirm = workflow.turn("1")
        self.assertIn("yes or no", confirm.lower())
        self.assertIn("Maya Chen", confirm)
        self.assertIn("9:00 AM UTC-05:00", confirm)
        self.assertEqual(posts(client, "/appointments"), [])
        booked = workflow.turn("yes")
        self.assertIn("appt_9002", booked)
        self.assertIn("scheduled", booked)
        body = posts(client, "/appointments")[0][2]
        self.assertEqual(
            body,
            {"patientId": "pat_1001", "slotId": "slot_4001", "confirmed": True},
        )
        self.assertIs(body["confirmed"], True)

    def test_yes_without_pending_slot_does_not_book(self):
        client = FakeClient()
        workflow = Workflow(client, scripted(BOOK))
        reply = workflow.turn("yes")
        self.assertNotIn("booked", reply.lower())
        self.assertEqual(posts(client, "/appointments"), [])

    def test_model_phone_does_not_search(self):
        client = FakeClient()
        workflow = Workflow(
            client,
            scripted({**BOOK, "phone": "555-0101", "dob": "1985-04-12"}),
        )
        reply = workflow.turn("I want to book a primary care appointment")
        self.assertEqual(client.calls, [])
        self.assertIn("phone", reply.lower())

    def test_model_patient_id_and_yes_do_not_book(self):
        client = FakeClient()
        workflow = Workflow(
            client,
            scripted(
                {
                    "intent": "book",
                    "patientId": "pat_1001",
                    "slot_choice": "slot_4001",
                    "confirmation": "yes",
                }
            ),
        )
        reply = workflow.turn("please")
        self.assertNotIn("booked", reply.lower())
        self.assertEqual(posts(client, "/appointments"), [])
        self.assertEqual(gets(client, "/patients/search"), [])

    def test_user_yes_books_even_if_the_model_switches_intent(self):
        client = FakeClient()
        client.push(200, {"matches": [PATIENT]})
        client.push(200, {"slots": [SLOT]})
        client.push(200, {"providers": PROVIDERS})
        client.push(201, {"appointment": APPOINTMENT})
        workflow = Workflow(
            client,
            scripted(BOOK, {}, {}, {"intent": "provider_lookup", "confirmation": "no"}),
        )
        workflow.turn("I want to book a primary care appointment downtown")
        workflow.turn("555-0101 1985-04-12")
        workflow.turn("1")
        reply = workflow.turn("yes")
        self.assertIn("appt_9002", reply)
        self.assertEqual(len(gets(client, "/providers")), 1)
        body = posts(client, "/appointments")[0][2]
        self.assertEqual(body["slotId"], "slot_4001")
        self.assertIs(body["confirmed"], True)

    def test_ok_is_not_confirmation(self):
        client = FakeClient()
        client.push(200, {"matches": [PATIENT]})
        client.push(200, {"slots": [SLOT]})
        client.push(200, {"providers": PROVIDERS})
        workflow = Workflow(
            client,
            scripted(BOOK, {}, {"confirmation": "yes", "slot_choice": "slot_4001"}),
        )
        workflow.turn("I want to book a primary care appointment downtown")
        workflow.turn("555-0101 1985-04-12")
        workflow.turn("1")
        reply = workflow.turn("ok")
        self.assertNotIn("appt_9002", reply)
        self.assertEqual(posts(client, "/appointments"), [])

    def test_partial_identity_asks_only_for_the_missing_field(self):
        client = FakeClient()
        client.push(200, {"matches": [PATIENT]})
        client.push(200, {"slots": [SLOT]})
        client.push(200, {"providers": PROVIDERS})
        workflow = Workflow(client, scripted({"intent": "book"}))
        workflow.turn("I want to book a primary care appointment downtown")
        phone_only = workflow.turn("555-0101")
        self.assertIn("date of birth", phone_only.lower())
        self.assertNotIn("phone number", phone_only.lower())
        self.assertEqual(client.calls, [])
        shown = workflow.turn("1985-04-12")
        self.assertIn("Dr. Elena Brooks", shown)
        self.assertNotIn("date of birth", shown.lower())
        self.assertEqual(len(gets(client, "/patients/search")), 1)
        self.assertEqual(gets(client, "/availability")[0][2]["specialty"], "primary_care")
        self.assertEqual(gets(client, "/availability")[0][2]["location"], "downtown")

    def test_date_only_asks_for_the_phone(self):
        client = FakeClient()
        workflow = Workflow(client, scripted({"intent": "book"}))
        workflow.turn("I want to book a primary care appointment")
        reply = workflow.turn("1985-04-12")
        self.assertIn("phone number", reply.lower())
        self.assertNotIn("date of birth", reply.lower())
        self.assertEqual(client.calls, [])

    def test_new_phone_keeps_the_saved_date_of_birth(self):
        client = FakeClient()
        client.push(200, {"matches": [PATIENT]})
        client.push(200, {"slots": [SLOT]})
        client.push(200, {"providers": PROVIDERS})
        client.push(200, {"matches": []})
        workflow = Workflow(client, scripted(BOOK))
        workflow.turn("I want to book a primary care appointment downtown")
        workflow.turn("555-0101 1985-04-12")
        reply = workflow.turn("555-0102")
        search = gets(client, "/patients/search")[-1][2]
        self.assertEqual(search["phone"], "555-0102")
        self.assertEqual(search["dob"], "1985-04-12")
        self.assertNotIn("1985-04-12", reply)
        self.assertNotIn("what is your date of birth", reply.lower())

    def test_changing_phone_searches_the_new_patient(self):
        client = FakeClient()
        client.push(200, {"matches": [PATIENT]})
        client.push(200, {"slots": [SLOT]})
        client.push(200, {"providers": PROVIDERS})
        client.push(200, {"matches": [PATIENT_2]})
        client.push(200, {"slots": [SLOT_2]})
        workflow = Workflow(client, scripted(BOOK))
        workflow.turn("I want to book a primary care appointment downtown")
        workflow.turn("555-0101 1985-04-12")
        workflow.turn("555-0102 1992-06-03")
        searches = gets(client, "/patients/search")
        self.assertEqual(searches[1][2]["phone"], "555-0102")
        self.assertEqual(searches[1][2]["dob"], "1992-06-03")
        availability = gets(client, "/availability")
        self.assertEqual(availability[1][2]["patientId"], "pat_1002")
        self.assertEqual(posts(client, "/appointments"), [])

    def test_empty_search_retries_once_then_hands_off(self):
        client = FakeClient()
        client.push(200, {"matches": []})
        client.push(200, {"matches": []})
        client.push(201, {"handoffId": "handoff_9003", "status": "queued"})
        workflow = Workflow(client, scripted(BOOK))
        workflow.turn("I want to book a primary care appointment")
        first = workflow.turn("555-9999 1990-01-01")
        self.assertIn("couldn't find", first.lower())
        self.assertNotIn("555-9999", first)
        self.assertNotIn("1990-01-01", first)
        self.assertEqual(posts(client, "/handoffs"), [])
        second = workflow.turn("555-9998 1991-02-02")
        self.assertIn("handoff_9003", second)
        body = posts(client, "/handoffs")[0][2]
        self.assertEqual(body["reason"], "identity_unclear")
        self.assertNotIn("patientId", body)
        blob = json.dumps(body)
        self.assertNotIn("555-9998", blob)
        self.assertNotIn("1991-02-02", blob)

    def test_give_up_after_empty_search_hands_off_without_another_search(self):
        client = FakeClient()
        client.push(200, {"matches": []})
        client.push(201, {"handoffId": "handoff_9004", "status": "queued"})
        workflow = Workflow(client, scripted(BOOK))
        workflow.turn("I want to book a primary care appointment")
        workflow.turn("555-9999 1990-01-01")
        reply = workflow.turn("I don't have another number")
        self.assertEqual(len(gets(client, "/patients/search")), 1)
        self.assertEqual(posts(client, "/handoffs")[0][2]["reason"], "identity_unclear")
        self.assertIn("handoff_9004", reply)

    def test_multiple_matches_hide_patient_details(self):
        client = FakeClient()
        client.push(
            200,
            {
                "matches": [
                    {
                        "patientId": "pat_1003",
                        "firstName": "Avery",
                        "lastName": "Patel",
                        "phone": "555-0130",
                        "dateOfBirth": "1978-09-22",
                        "zipCode": "70115",
                    },
                    {
                        "patientId": "pat_1004",
                        "firstName": "Avery",
                        "lastName": "Patel",
                        "phone": "555-0130",
                        "dateOfBirth": "1978-09-22",
                        "zipCode": "70005",
                    },
                ]
            },
        )
        workflow = Workflow(client, scripted(BOOK))
        workflow.turn("I want to book a primary care appointment")
        reply = workflow.turn("555-0130 1978-09-22")
        self.assertIn("zip code", reply.lower())
        for secret in ("Avery", "Patel", "70115", "70005", "pat_1003", "555-0130", "1978-09-22"):
            self.assertNotIn(secret, reply)
        self.assertEqual(posts(client, "/handoffs"), [])
        self.assertEqual(gets(client, "/availability"), [])

    def test_invalid_zip_explains_the_format_and_waits(self):
        client = FakeClient()
        client.push(
            200,
            {
                "matches": [
                    {"patientId": "pat_1003", "firstName": "Avery", "lastName": "Patel", "zipCode": "70115"},
                    {"patientId": "pat_1004", "firstName": "Avery", "lastName": "Patel", "zipCode": "70005"},
                ]
            },
        )
        client.push(200, {"slots": [SLOT]})
        client.push(200, {"providers": PROVIDERS})
        workflow = Workflow(client, scripted(BOOK))
        workflow.turn("I want to book a primary care appointment")
        workflow.turn("555-0130 1978-09-22")
        reply = workflow.turn("700051")
        self.assertIn("doesn't look right", reply.lower())
        self.assertIn("5 digits", reply.lower())
        for secret in ("70115", "70005", "Avery", "Patel"):
            self.assertNotIn(secret, reply)
        self.assertEqual(posts(client, "/handoffs"), [])
        self.assertEqual(gets(client, "/availability"), [])
        shown = workflow.turn("70005")
        self.assertEqual(gets(client, "/availability")[0][2]["patientId"], "pat_1004")
        self.assertIn("Dr. Elena Brooks", shown)

    def test_phone_typed_at_the_zip_step_does_not_forget_the_date(self):
        client = FakeClient()
        client.push(
            200,
            {
                "matches": [
                    {"patientId": "pat_1003", "firstName": "Avery", "lastName": "Patel", "zipCode": "70115"},
                    {"patientId": "pat_1004", "firstName": "Avery", "lastName": "Patel", "zipCode": "70005"},
                ]
            },
        )
        workflow = Workflow(client, scripted({"intent": "appointment_lookup"}))
        workflow.turn("Look up my existing appointments.")
        asked = workflow.turn("09-22-1978")
        self.assertIn("phone number", asked.lower())
        self.assertNotIn("date of birth", asked.lower())
        zipped = workflow.turn("555-0130")
        self.assertIn("zip", zipped.lower())
        again = workflow.turn("555-0130")
        self.assertNotIn("date of birth", again.lower())
        self.assertNotIn("or a date", again.lower())
        self.assertIn("looks like a phone number", again.lower())
        self.assertIn("zip", again.lower())
        self.assertEqual(workflow.dob, "1978-09-22")
        self.assertEqual(workflow.phone, "555-0130")

    def test_empty_slots_are_not_invented(self):
        client = FakeClient()
        client.push(200, {"matches": [PATIENT_2]})
        client.push(200, {"slots": []})
        client.push(201, {"handoffId": "handoff_9006", "status": "queued"})
        workflow = Workflow(client, scripted({"intent": "book", "specialty": "dermatology", "location": "lakeside"}))
        workflow.turn("I want to book a dermatology appointment at lakeside")
        reply = workflow.turn("555-0102 1992-06-03")
        self.assertIn("nothing is available", reply.lower())
        self.assertNotIn("slot_", reply)
        query = gets(client, "/availability")[0][2]
        self.assertEqual(query["specialty"], "dermatology")
        self.assertEqual(query["location"], "lakeside")
        self.assertEqual(posts(client, "/appointments"), [])
        handed = workflow.turn("no")
        self.assertEqual(posts(client, "/handoffs")[0][2]["reason"], "no_availability")
        self.assertIn("handoff_9006", handed)

    def test_conflict_does_not_claim_a_booking(self):
        client = FakeClient()
        client.push(200, {"matches": [PATIENT]})
        client.push(200, {"slots": [SLOT, SLOT_2]})
        client.push(200, {"providers": PROVIDERS})
        client.push(409, {"code": "slot_taken", "message": "That slot was just taken."})
        client.push(200, {"slots": [SLOT_2]})
        workflow = Workflow(client, scripted(BOOK))
        workflow.turn("I want to book a primary care appointment downtown")
        workflow.turn("555-0101 1985-04-12")
        workflow.turn("1")
        reply = workflow.turn("yes")
        self.assertNotIn("Booked", reply)
        self.assertIn("Dr. Marcus King", reply)
        self.assertIn("taken", reply.lower())

    def test_inconsistent_201_receipt_requires_verification(self):
        client = FakeClient()
        client.push(200, {"matches": [PATIENT]})
        client.push(200, {"slots": [SLOT]})
        client.push(200, {"providers": PROVIDERS})
        client.push(201, {"appointment": {**APPOINTMENT, "patientId": "another_patient"}})
        client.push(201, {"handoffId": "handoff_9013", "status": "queued"})
        flow = Workflow(client, scripted(BOOK))
        for text in ("Book a visit", "555-0101 1985-04-12", "1"):
            flow.turn(text)
        reply = flow.turn("yes")
        self.assertIn("couldn't verify whether", reply)
        self.assertNotIn(APPOINTMENT["appointmentId"], reply)
        self.assertEqual(posts(client, "/handoffs")[0][2]["reason"], "api_failure")
        self.assertEqual(flow.turn("yes"), reply)
        self.assertEqual(len(posts(client, "/appointments")), 1)

    def test_outage_hands_off_after_one_attempt(self):
        client = FakeClient()
        client.push(503, {"code": "downstream_unavailable", "message": "down"})
        client.push(201, {"handoffId": "handoff_9007", "status": "queued"})
        workflow = Workflow(client, scripted(BOOK))
        workflow.turn("I want to book a primary care appointment")
        reply = workflow.turn("555-0101 1985-04-12")
        self.assertEqual(len(gets(client, "/patients/search")), 1)
        self.assertEqual(posts(client, "/handoffs")[0][2]["reason"], "api_failure")
        self.assertIn("handoff_9007", reply)
        self.assertNotIn("555-0101", reply)

    def test_failed_handoff_still_tells_the_user(self):
        client = FakeClient()
        client.push(0, {"code": "downstream_unavailable", "message": "down"})
        client.push(503, {"code": "downstream_unavailable", "message": "down"})
        workflow = Workflow(client, scripted(LOOKUP))
        reply = workflow.turn("Which primary care providers are downtown?")
        self.assertIn("person", reply.lower())
        self.assertNotIn("Booked", reply)
        self.assertEqual(len(gets(client, "/providers")), 1)

    def test_medical_advice_does_not_book_or_repeat_symptoms(self):
        client = FakeClient()
        client.push(200, {"matches": [PATIENT]})
        client.push(200, {"slots": [SLOT]})
        client.push(200, {"providers": PROVIDERS})
        client.push(201, {"handoffId": "handoff_9008", "status": "queued"})
        workflow = Workflow(
            client,
            scripted(BOOK, {}, {}, {"intent": "book", "confirmation": "yes"}),
        )
        workflow.turn("I want to book a primary care appointment downtown")
        workflow.turn("555-0101 1985-04-12")
        workflow.turn("1")
        reply = workflow.turn("I have chest pain, should I wait? yes")
        self.assertNotIn("chest pain", reply.lower())
        self.assertNotIn("Booked", reply)
        self.assertEqual(posts(client, "/appointments"), [])
        body = posts(client, "/handoffs")[0][2]
        self.assertEqual(body["reason"], "medical_advice")
        self.assertNotIn("chest pain", json.dumps(body).lower())
        self.assertNotIn("patientId", body)

    def test_identity_answer_continues_when_model_says_unsupported(self):
        client = FakeClient()
        client.push(200, {"matches": []})
        workflow = Workflow(
            client,
            scripted(BOOK, {"intent": "unsupported", "specialty": "cardiology"}),
        )
        workflow.turn("I want to book a primary care appointment downtown")
        reply = workflow.turn("555-0130, 2000-12-20")
        self.assertNotIn("outside this assistant", reply.lower())
        self.assertNotIn("not supported", reply.lower())
        self.assertEqual(posts(client, "/handoffs"), [])
        search = gets(client, "/patients/search")[0][2]
        self.assertEqual(search["phone"], "555-0130")
        self.assertEqual(search["dob"], "2000-12-20")
        self.assertIn("couldn't find", reply.lower())

    def test_slot_number_continues_when_model_says_unsupported(self):
        client = FakeClient()
        client.push(200, {"matches": [PATIENT]})
        client.push(200, {"slots": [SLOT]})
        client.push(200, {"providers": PROVIDERS})
        workflow = Workflow(
            client,
            scripted(BOOK, {}, {"intent": "unsupported"}),
        )
        workflow.turn("I want to book a primary care appointment downtown")
        workflow.turn("555-0101 1985-04-12")
        reply = workflow.turn("1")
        self.assertIn("yes or no", reply.lower())
        self.assertEqual(posts(client, "/handoffs"), [])
        self.assertEqual(posts(client, "/appointments"), [])

    def test_unsupported_specialty_does_not_search(self):
        client = FakeClient()
        client.push(201, {"handoffId": "handoff_9009", "status": "queued"})
        workflow = Workflow(client, scripted({"intent": "book", "specialty": "cardiology"}))
        reply = workflow.turn("I want to book cardiology")
        self.assertTrue(all(call[1] == "/handoffs" for call in client.calls))
        self.assertEqual(posts(client, "/handoffs")[0][2]["reason"], "unsupported_request")
        self.assertIn("not supported", reply.lower())

    def test_zip_code_selects_one_patient_and_hides_the_other(self):
        client = FakeClient()
        client.push(
            200,
            {
                "matches": [
                    {
                        "patientId": "pat_1003",
                        "firstName": "Avery",
                        "lastName": "Patel",
                        "phone": "555-0130",
                        "dateOfBirth": "1978-09-22",
                        "zipCode": "70115",
                    },
                    {
                        "patientId": "pat_1004",
                        "firstName": "Avery",
                        "lastName": "Patel",
                        "phone": "555-0130",
                        "dateOfBirth": "1978-09-22",
                        "zipCode": "70005",
                    },
                ]
            },
        )
        client.push(200, {"slots": [SLOT]})
        client.push(200, {"providers": PROVIDERS})
        workflow = Workflow(
            client,
            scripted(BOOK, {}, {"intent": "unsupported", "specialty": "cardiology"}),
        )
        workflow.turn("I want to book a primary care appointment")
        workflow.turn("555-0130 1978-09-22")
        reply = workflow.turn("70115")
        self.assertIn("Dr. Elena Brooks", reply)
        self.assertNotIn("Avery", reply)
        self.assertNotIn("70005", reply)
        self.assertEqual(gets(client, "/availability")[0][2]["patientId"], "pat_1003")
        self.assertNotIn("70115", json.dumps(workflow.events))
        self.assertEqual(posts(client, "/handoffs"), [])

    def test_unknown_zip_hands_off_without_patient_details(self):
        client = FakeClient()
        client.push(
            200,
            {
                "matches": [
                    {"patientId": "pat_1003", "firstName": "Avery", "lastName": "Patel", "zipCode": "70115"},
                    {"patientId": "pat_1004", "firstName": "Avery", "lastName": "Patel", "zipCode": "70005"},
                ]
            },
        )
        client.push(201, {"handoffId": "handoff_9011", "status": "queued"})
        workflow = Workflow(client, scripted(BOOK))
        workflow.turn("I want to book a primary care appointment")
        workflow.turn("555-0130 1978-09-22")
        reply = workflow.turn("00000")
        self.assertIn("handoff_9011", reply)
        self.assertEqual(posts(client, "/handoffs")[0][2]["reason"], "identity_unclear")
        for secret in ("Avery", "Patel", "70115", "70005", "pat_1003"):
            self.assertNotIn(secret, reply)
            self.assertNotIn(secret, json.dumps(posts(client, "/handoffs")[0][2]))
        self.assertEqual(gets(client, "/availability"), [])

    def test_appointment_lookup_uses_identity_then_the_api(self):
        client = FakeClient()
        client.push(200, {"matches": [PATIENT]})
        client.push(
            200,
            {
                "appointments": [
                    {
                        "appointmentId": "appt_3001",
                        "patientId": "pat_1001",
                        "providerId": "prov_2001",
                        "specialty": "primary_care",
                        "location": "downtown",
                        "startTime": "2026-12-08T14:00:00-05:00",
                        "status": "scheduled",
                    }
                ]
            },
        )
        workflow = Workflow(client, scripted({"intent": "appointment_lookup"}))
        asked = workflow.turn("Look up my appointment")
        self.assertIn("phone", asked.lower())
        self.assertEqual(client.calls, [])
        reply = workflow.turn("555-0101 1985-04-12")
        self.assertIn("appt_3001", reply)
        self.assertIn("primary care", reply)
        self.assertIn("2:00 PM UTC-05:00", reply)
        self.assertNotIn("2026-12-08T14:00:00-05:00", reply)
        self.assertEqual(gets(client, "/patients/pat_1001/appointments")[0][1], "/patients/pat_1001/appointments")
        self.assertEqual(posts(client, "/appointments"), [])
        self.assertNotIn("555-0101", reply)

    def test_appointment_lookup_with_no_rows_invents_nothing(self):
        client = FakeClient()
        client.push(200, {"matches": [PATIENT_2]})
        client.push(200, {"appointments": []})
        workflow = Workflow(client, scripted({"intent": "appointment_lookup"}))
        workflow.turn("Look up my appointment")
        reply = workflow.turn("555-0102 1992-06-03")
        self.assertIn("couldn't find any appointments for you", reply.lower())
        self.assertNotIn("appt_", reply)

    def test_unknown_slot_is_rejected(self):
        client = FakeClient()
        client.push(200, {"matches": [PATIENT]})
        client.push(200, {"slots": [SLOT]})
        client.push(200, {"providers": PROVIDERS})
        workflow = Workflow(client, scripted(BOOK, {}, {"slot_choice": "slot_evil"}))
        workflow.turn("I want to book a primary care appointment downtown")
        workflow.turn("555-0101 1985-04-12")
        reply = workflow.turn("slot_evil")
        self.assertIn("Dr. Elena Brooks", reply)
        self.assertEqual(posts(client, "/appointments"), [])

    def test_events_omit_phone_dob_and_query(self):
        client = FakeClient()
        client.push(200, {"matches": [PATIENT]})
        client.push(200, {"slots": [SLOT]})
        client.push(200, {"providers": PROVIDERS})
        workflow = Workflow(client, scripted(BOOK))
        workflow.turn("I want to book a primary care appointment downtown")
        workflow.turn("555-0101 1985-04-12")
        blob = json.dumps(workflow.events)
        self.assertNotIn("555-0101", blob)
        self.assertNotIn("1985-04-12", blob)
        self.assertNotIn("Maya", blob)
        for event in workflow.events:
            self.assertNotIn("?", event["path"])
            self.assertEqual(
                set(event),
                {
                    "intent",
                    "state",
                    "method",
                    "path",
                    "status",
                    "duration_ms",
                    "escalation_reason",
                    "error_code",
                },
            )

    def test_missing_key_sends_nothing(self):
        client = FakeClient()

        def extract(_text, _context):
            raise MissingKey()

        reply = Workflow(client, extract).turn("I want to book a primary care appointment")
        self.assertIn("OPENAI_API_KEY", reply)
        self.assertEqual(client.calls, [])

    def test_model_error_sends_nothing(self):
        client = FakeClient()

        def extract(_text, _context):
            raise ModelError("The model request failed. sk-secretkeyvalue")

        reply = Workflow(client, extract).turn("I want to book a primary care appointment")
        self.assertNotIn("sk-secretkeyvalue", reply)
        self.assertIn("nothing was booked", reply.lower())
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
