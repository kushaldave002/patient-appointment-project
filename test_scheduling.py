import json
import socket
from contextlib import contextmanager
from unittest.mock import patch
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scheduling import SchedulingClient


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._reply()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.server.bodies.append(self.rfile.read(length))
        self._reply()

    def _reply(self):
        self.server.paths.append(self.path)
        self.server.headers.append(dict(self.headers))
        if self.server.mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://example.com/stolen")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        raw = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        return


@contextmanager
def http_server(handler):
    """Exercise the HTTP wire format even on hosts that prohibit TCP listeners."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler, bind_and_activate=False)
    with server:
        try:
            server.server_bind()
            server.server_activate()
        except PermissionError:
            workers = []
            def connect(connection):
                client, peer = socket.socketpair()
                def handle():
                    with peer:
                        server.finish_request(peer, ("127.0.0.1", 0))
                thread = threading.Thread(target=handle, daemon=True)
                thread.start()
                workers.append(thread)
                connection.sock = client
            with patch("scheduling.HTTPConnection.connect", connect):
                yield server
            for worker in workers:
                worker.join(timeout=2)
        else:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield server
            finally:
                server.shutdown()
                thread.join()


class SchedulingClientTest(unittest.TestCase):
    def serve(self, mode="ok"):
        context = http_server(_Handler)
        server = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        server.mode = mode
        server.paths, server.headers, server.bodies = [], [], []
        return server

    def test_accepts_loopback_http(self):
        server = self.serve()
        port = server.server_address[1]
        client = SchedulingClient("http://127.0.0.1:%d" % port)
        result = client.get("/providers", {"specialty": "primary_care"})
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["body"], {"ok": True})
        self.assertGreaterEqual(result["duration_ms"], 0)
        self.assertEqual(server.paths, ["/providers?specialty=primary_care"])

    def test_rejects_foreign_host(self):
        with self.assertRaises(ValueError):
            SchedulingClient("https://api.openai.com")
        with self.assertRaises(ValueError):
            SchedulingClient("http://example.com:4010")

    def test_rejects_userinfo_and_query_on_base(self):
        with self.assertRaises(ValueError):
            SchedulingClient("http://user:pass@127.0.0.1:4010")
        with self.assertRaises(ValueError):
            SchedulingClient("http://127.0.0.1:4010/?x=1")

    def test_request_has_no_authorization_header(self):
        server = self.serve()
        port = server.server_address[1]
        client = SchedulingClient("http://localhost:%d" % port)
        client.post("/appointments", {"patientId": "pat_1001", "slotId": "slot_4001", "confirmed": True})
        names = {k.lower() for k in server.headers[0]}
        self.assertNotIn("authorization", names)
        self.assertNotIn("x-api-key", names)

    def test_redirect_is_not_followed(self):
        server = self.serve(mode="redirect")
        port = server.server_address[1]
        client = SchedulingClient("http://127.0.0.1:%d" % port, timeout=1)
        result = client.get("/providers")
        self.assertEqual(result["status"], 302)
        self.assertEqual(server.paths, ["/providers"])

    def test_connection_failure_is_status_zero(self):
        client = SchedulingClient("http://127.0.0.1:1", timeout=0.2)
        result = client.get("/providers")
        self.assertEqual(result["status"], 0)
        self.assertEqual(result["body"]["code"], "downstream_unavailable")


if __name__ == "__main__":
    unittest.main()
