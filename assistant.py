"""Terminal assistant. OpenAI is called only after the user types a line."""

import json
import os
import sys

from model import complete
from scheduling import SchedulingClient
from workflow import MissingKey, Workflow

from identity import redact_identifiers

LOG_KEYS = (
    "intent",
    "state",
    "method",
    "path",
    "status",
    "duration_ms",
    "escalation_reason",
    "error_code",
)


def format_log(event):
    if not isinstance(event, dict) or not event.get("method"):
        return ""
    safe = {key: event.get(key, "") for key in LOG_KEYS}
    for value in safe.values():
        if isinstance(value, str) and redact_identifiers(value) != value:
            return ""
    try:
        return json.dumps(safe, separators=(",", ":"))
    except TypeError:
        return ""



def write_events(workflow, log_fp):
    for event in workflow.drain_events():
        line = format_log(event)
        if line and log_fp is not None:
            log_fp.write(line + "\n")
            log_fp.flush()


def run(stdin, stdout, workflow, log_fp):
    stdout.write("I can look up a provider or book an appointment.\n")
    _flush(stdout)
    for raw in stdin:
        text = raw.strip()
        if not text:
            continue
        if text.lower() in ("quit", "exit"):
            stdout.write("Goodbye.\n")
            _flush(stdout)
            return
        stdout.write(workflow.turn(text) + "\n")
        _flush(stdout)
        write_events(workflow, log_fp)


def open_log(path):
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "a", encoding="utf-8")


def create_workflow():
    base = os.environ.get("SCHEDULING_API_BASE", "http://127.0.0.1:4010")
    client = SchedulingClient(base)
    api_key = os.environ.get("OPENAI_API_KEY", "")
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

    def extract(text, context):
        if not api_key:
            raise MissingKey()
        return complete(api_key, model, text, context=context)

    return Workflow(client, extract)


def main(argv=None):
    del argv
    try:
        workflow = create_workflow()
    except ValueError as err:
        sys.stderr.write(str(err) + "\n")
        return 2
    log_path = os.environ.get("ASSISTANT_LOG", "assistant.log")
    with open_log(log_path) as log_fp:
        run(sys.stdin, sys.stdout, workflow, log_fp)
    return 0


def _flush(stream):
    flush = getattr(stream, "flush", None)
    if flush:
        flush()


if __name__ == "__main__":
    sys.exit(main())
