"""Loopback browser interface. Each conversation gets its own scheduling workflow."""

import os
import secrets
import threading
import time
from collections import deque
from html import escape
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from string import Template
from urllib.parse import parse_qs

from assistant import create_workflow, open_log, write_events
from identity import redact_identifiers


PAGE = Template(Path(__file__).with_name("chat.html").read_text(encoding="utf-8"))
STARTERS = [
    ("Find a provider", "I'd like to find a provider."),
    ("Book a visit", "I'd like to book an appointment."),
    ("View my appointments", "Look up my existing appointments."),
]
SESSION_SECONDS = 30 * 60


class Conversation:
    def __init__(self, workflow):
        self.workflow = workflow
        self.messages = deque(maxlen=40)
        self.token = secrets.token_urlsafe(24)
        self.touched = time.monotonic()
        self.lock = threading.Lock()


def render(chat):
    token = '<input type="hidden" name="token" value="%s">' % chat.token
    reset = ""
    if chat.messages:
        reset = '<form method="post" action="/new">%s<button class="reset">Start over</button></form>' % token
    messages = []
    for user_text, reply in chat.messages:
        for label, body in (("You", user_text), ("Assistant", reply)):
            role = "user" if label == "You" else "assistant"
            messages.append('<div class="message %s"><b>%s</b><p>%s</p></div>' %
                            (role, label, escape(body)))
    suggestions = chat.workflow.suggested_replies if messages else STARTERS
    buttons = "".join(
        '<button class="choice" type="submit" name="message" value="%s">%s</button>' %
        (escape(value, quote=True), escape(label)) for label, value in suggestions
    )
    controls = ""
    if buttons:
        controls = '<form method="post" action="/turn" class="choices">%s%s</form>' % (token, buttons)
    if chat.workflow.terminal_reply:
        composer = '<form method="post" action="/new">%s<button class="send">Start a new conversation</button></form>' % token
        reset = ""
    else:
        composer = (
            '<form method="post" action="/turn" class="composer">%s'
            '<label for="message">Your message</label><div class="input-row">'
            '<input id="message" name="message" type="text" maxlength="2000" required '
            'autocomplete="off" placeholder="Type a message…">'
            '<button type="submit" class="send">Send <span aria-hidden="true">↑</span></button></div></form>'
        ) % token
    return PAGE.substitute(
        title="Your conversation" if messages else "How can we help with your visit?",
        intro="" if messages else "<p class='intro'>Choose where to start, or tell the assistant what you need.</p>",
        reset=reset, messages="".join(messages), controls=controls, composer=composer,
    )


def web_handler(workflow_factory, log_fp):
    sessions = {}
    registry_lock = threading.Lock()
    log_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Workflow events are logged separately without cookies or form contents.

        def session(self, create=False):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
            except CookieError:
                pass
            key = cookie["visit"].value if "visit" in cookie else ""
            now = time.monotonic()
            with registry_lock:
                for expired in [k for k, v in sessions.items() if now - v.touched > SESSION_SECONDS]:
                    del sessions[expired]
                if key not in sessions:
                    if not create:
                        return None, None
                    key = secrets.token_urlsafe(32)
                    sessions[key] = Conversation(workflow_factory())
                chat = sessions[key]
                chat.touched = now
                return key, chat

        def respond(self, status, body="", *, cookie=None, location=None):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
            if cookie:
                self.send_header("Set-Cookie", "visit=%s; HttpOnly; SameSite=Strict; Path=/" % cookie)
            if location:
                self.send_header("Location", location)
            self.end_headers()
            if data:
                self.wfile.write(data)

        def local_host(self):
            port = self.server.server_address[1]
            return self.headers.get("Host") in ("127.0.0.1:%d" % port, "localhost:%d" % port)

        def do_GET(self):
            if not self.local_host() or self.path != "/":
                return self.respond(404)
            key, chat = self.session(create=True)
            with chat.lock:
                page = render(chat)
            self.respond(200, page, cookie=key)

        def do_POST(self):
            if not self.local_host() or self.path not in ("/turn", "/new"):
                return self.respond(404)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 32000 or self.headers.get_content_type() != "application/x-www-form-urlencoded":
                    return self.respond(400)
                fields = parse_qs(self.rfile.read(size).decode("utf-8"), max_num_fields=3)
            except (ValueError, UnicodeDecodeError):
                return self.respond(400)
            key, chat = self.session()
            if chat is None:
                return self.respond(409, 'This conversation expired. <a href="/">Start a new conversation</a>.')
            with chat.lock:
                # A form applies only to the prompt that issued it. A double-click,
                # refresh or stale tab cannot confirm a later selection.
                if fields.get("token") != [chat.token]:
                    return self.respond(303, location="/#latest")
                chat.token = secrets.token_urlsafe(24)
                if self.path == "/new":
                    chat.workflow = workflow_factory()
                    chat.messages.clear()
                else:
                    message = fields.get("message", [""])[0].strip()
                    if message and not chat.workflow.terminal_reply:
                        reply = chat.workflow.turn(message)
                        with log_lock:
                            write_events(chat.workflow, log_fp)
                        chat.messages.append((redact_identifiers(message[:2000]), reply))
            self.respond(303, location="/#latest" if self.path == "/turn" else "/")

    return Handler


def main():
    with open_log(os.environ.get("ASSISTANT_LOG", "assistant.log")) as log_fp:
        handler = web_handler(create_workflow, log_fp)
        with ThreadingHTTPServer(("127.0.0.1", 8000), handler) as server:
            print("Open http://127.0.0.1:8000 in your browser. Press Ctrl+C to stop.", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
