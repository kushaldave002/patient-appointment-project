import io
import json
import os
import stat
import tempfile
import unittest

from assistant import format_log, open_log, run


class WorkflowDouble:
    def __init__(self):
        self.turns = []
        self._events = []

    def turn(self, text):
        self.turns.append(text)
        return "ok"

    def drain_events(self):
        events = self._events
        self._events = []
        return events


class AssistantTest(unittest.TestCase):
    def test_greeting_happens_before_any_turn(self):
        workflow = WorkflowDouble()
        stdout = io.StringIO()
        run(io.StringIO(""), stdout, workflow, None)
        self.assertIn("provider", stdout.getvalue().lower())
        self.assertEqual(workflow.turns, [])

    def test_quit_stops_the_loop(self):
        workflow = WorkflowDouble()
        stdout = io.StringIO()
        run(io.StringIO("hello\nquit\nmore\n"), stdout, workflow, None)
        self.assertEqual(workflow.turns, ["hello"])
        self.assertIn("Goodbye", stdout.getvalue())
        self.assertNotIn("more", workflow.turns)

    def test_format_log_drops_extra_phone_field(self):
        line = format_log(
            {
                "intent": "book",
                "state": "identify",
                "method": "GET",
                "path": "/patients/search",
                "status": 200,
                "duration_ms": 1,
                "escalation_reason": "",
                "error_code": "",
                "phone": "555-0101",
            }
        )
        self.assertNotIn("555-0101", line)
        self.assertNotIn("phone", line)
        parsed = json.loads(line)
        self.assertEqual(parsed["path"], "/patients/search")

    def test_log_file_is_owner_only(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "assistant.log")
        handle = open_log(path)
        handle.write("{}\n")
        handle.close()
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_format_log_drops_a_line_that_contains_a_phone_or_key(self):
        base = {
            "intent": "book",
            "state": "identify",
            "method": "GET",
            "path": "/patients/search",
            "status": 200,
            "duration_ms": 1,
            "escalation_reason": "",
            "error_code": "",
        }
        leaked = dict(base)
        leaked["path"] = "/patients/search?phone=555-0101"
        self.assertEqual(format_log(leaked), "")
        dated = dict(base)
        dated["error_code"] = "1985-04-12"
        self.assertEqual(format_log(dated), "")
        keyed = dict(base)
        keyed["error_code"] = "sk-secretkeyvalue"
        self.assertEqual(format_log(keyed), "")


if __name__ == "__main__":
    unittest.main()
