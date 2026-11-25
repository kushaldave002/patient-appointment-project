import io
import re
import unittest
from http.client import HTTPConnection
from urllib.parse import urlencode

from test_scenarios import BOOK, PROVIDERS, interpreted
from test_scheduling import http_server
from test_workflow import APPOINTMENT, PATIENT, PROVIDERS as DIRECTORY, SLOT, SLOT_2, FakeClient, posts
from web import web_handler
from workflow import Workflow


class WebTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeClient()
        self.intents = []
        self.log = io.StringIO()
        handler = web_handler(lambda: Workflow(self.api, interpreted(*self.intents)), self.log)
        server = http_server(handler)
        self.port = server.__enter__().server_address[1]
        self.addCleanup(server.__exit__, None, None, None)

    def request(self, path="/", cookie="", form=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            headers = {"Cookie": cookie}
            if form is None:
                connection.request("GET", path, headers=headers)
            else:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                connection.request("POST", path, urlencode(form), headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()
        finally:
            connection.close()

    def start(self):
        code, headers, page = self.request()
        self.assertEqual(code, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        return headers["Set-Cookie"].split(";")[0], page

    def submit(self, cookie, page, message, path="/turn"):
        token = re.search(r'name="token" value="([^"]+)"', page)[1]
        code, _, _ = self.request(path, cookie, {"message": message, "token": token})
        self.assertEqual(code, 303)
        return self.request(cookie=cookie)[2]

    def test_public_lookup_and_isolated_transcripts(self):
        self.intents.append(PROVIDERS)
        self.api.push(200, {"providers": DIRECTORY})
        cookie, page = self.start()
        page = self.submit(cookie, page, "Which primary care providers are downtown?")
        self.assertIn("Dr. Elena Brooks", page)
        self.assertEqual([call[1] for call in self.api.calls], ["/providers"])
        other_cookie, other_page = self.start()
        self.assertNotEqual(cookie, other_cookie)
        self.assertNotIn("Dr. Elena Brooks", other_page)

    def test_changed_slot_requires_fresh_confirmation_and_replays_only_book_once(self):
        self.intents.append(BOOK)
        self.api.push(200, {"matches": [PATIENT]})
        self.api.push(200, {"slots": [SLOT, SLOT_2]})
        self.api.push(200, {"providers": DIRECTORY})
        self.api.push(201, {"appointment": {**APPOINTMENT, "providerId": SLOT_2["providerId"],
                                          "startTime": SLOT_2["startTime"]}})
        cookie, page = self.start()
        page = self.submit(cookie, page, "Book a primary care visit downtown")
        page = self.submit(cookie, page, "555-0101 1985-04-12")
        self.assertIn("Option 1", page)
        self.assertNotIn("555-0101", page)
        self.assertNotIn("Confirm booking", page)
        first = self.submit(cookie, page, "1")
        self.assertIn("Confirm booking", first)
        page = self.submit(cookie, first, "no")
        second = self.submit(cookie, page, "2")
        self.submit(cookie, first, "yes")
        self.assertEqual(posts(self.api, "/appointments"), [])
        booked = self.submit(cookie, second, "yes")
        self.assertIn(APPOINTMENT["appointmentId"], booked)
        self.submit(cookie, second, "yes")
        self.assertEqual([c[2] for c in posts(self.api, "/appointments")],
                         [{"patientId": PATIENT["patientId"], "slotId": SLOT_2["slotId"], "confirmed": True}])
        self.assertIn('"state":"booked"', self.log.getvalue())

    def test_handoff_and_start_over(self):
        self.api.push(201, {"handoffId": "handoff_9010", "status": "queued"})
        cookie, page = self.start()
        page = self.submit(cookie, page, "I have chest pain. Should I wait?")
        self.assertIn("handoff_9010", page)
        self.assertNotIn('id="message"', page)
        self.assertEqual(posts(self.api, "/handoffs")[0][2]["reason"], "medical_advice")
        fresh = self.submit(cookie, page, "", "/new")
        self.assertNotIn("handoff_9010", fresh)
        self.assertIn('id="message"', fresh)


if __name__ == "__main__":
    unittest.main()
