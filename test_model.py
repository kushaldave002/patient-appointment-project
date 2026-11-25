import json
import unittest
import urllib.error

from model import OPENAI_URL, ModelError, complete, redact_identifiers


class RedactTest(unittest.TestCase):
    def test_replaces_phone_and_dob(self):
        text = redact_identifiers("Book 555-0101 on 2026-12-02, DOB 1985-04-12 and keep sk-testkeyvalue")
        self.assertNotIn("555-0101", text)
        self.assertNotIn("1985-04-12", text)
        self.assertNotIn("2026-12-02", text)
        self.assertNotIn("sk-testkeyvalue", text)
        self.assertIn("[phone]", text)
        self.assertIn("[dob]", text)
        self.assertIn("on [date]", text)


class CompleteTest(unittest.TestCase):
    def test_posts_redacted_text_to_openai_only(self):
        captured = {}

        def urlopen(req, timeout=30):
            captured["req"] = req
            captured["timeout"] = timeout
            content = json.dumps(
                {
                    "intent": "book",
                    "specialty": "primary_care",
                    "location": "downtown",
                    "slot_choice": "1",
                    "confirmation": "",
                    "phone": "555-0101",
                    "patientId": "pat_1001",
                }
            )
            raw = json.dumps({"choices": [{"message": {"content": content}}]}).encode()

            class Response:
                def read(self, _n):
                    return raw

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

            return Response()

        result = complete(
            "sk-secretkeyvalue",
            "gpt-4o-mini",
            "My phone is 555-0101 and my dob is 1985-04-12",
            urlopen=urlopen,
        )
        self.assertEqual(captured["req"].full_url, OPENAI_URL)
        self.assertTrue(captured["req"].full_url.startswith("https://api.openai.com/"))
        sent = json.loads(captured["req"].data.decode())
        blob = json.dumps(sent)
        self.assertNotIn("555-0101", blob)
        self.assertNotIn("1985-04-12", blob)
        self.assertNotIn("sk-secretkeyvalue", blob)
        self.assertIn("[phone]", blob)
        self.assertEqual(captured["req"].get_header("Authorization"), "Bearer sk-secretkeyvalue")
        self.assertEqual(result["intent"], "book")
        self.assertEqual(result["specialty"], "primary_care")
        self.assertNotIn("phone", result)
        self.assertNotIn("patientId", result)

    def test_http_error_hides_the_key(self):
        def urlopen(req, timeout=30):
            raise urllib.error.HTTPError(req.full_url, 401, "bad", None, None)

        with self.assertRaises(ModelError) as caught:
            complete("sk-secretkeyvalue", "gpt-4o-mini", "hello", urlopen=urlopen)
        self.assertNotIn("sk-secretkeyvalue", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
