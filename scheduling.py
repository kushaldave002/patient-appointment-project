"""HTTP client for the local mock scheduling API.

Talks only to http://127.0.0.1 or http://localhost. Does not follow redirects
and does not send an Authorization header.
"""

import json
import time
from http.client import HTTPConnection, HTTPException
from urllib.parse import urlencode, urlsplit

MAX_BYTES = 1_000_000
_HOSTS = ("127.0.0.1", "localhost")


def validate_base(url):
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "http" or host not in _HOSTS:
        raise ValueError("Scheduling API must be http on 127.0.0.1 or localhost")
    if parts.username or parts.password:
        raise ValueError("Scheduling API URL cannot include credentials")
    if parts.query or parts.fragment or parts.path not in ("", "/"):
        raise ValueError("Scheduling API URL cannot include a path, query, or fragment")
    if parts.port is None:
        raise ValueError("Scheduling API URL must include a port")
    return "http://%s:%d" % (host, parts.port)


class SchedulingClient:
    def __init__(self, base_url, timeout=5):
        self.base_url = validate_base(base_url)
        self.timeout = timeout

    def get(self, path, query=None):
        return self._call("GET", path, query=query)

    def post(self, path, body):
        return self._call("POST", path, body=body)

    def _call(self, method, path, query=None, body=None):
        parts = urlsplit(self.base_url)
        target = path
        if query:
            target = path + "?" + urlencode(query)
        payload = None
        headers = {"Accept": "application/json", "Connection": "close"}
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        started = time.monotonic()
        conn = HTTPConnection(parts.hostname, parts.port, timeout=self.timeout)
        try:
            conn.request(method, target, body=payload, headers=headers)
            response = conn.getresponse()
            status = response.status
            raw = response.read(MAX_BYTES + 1)
        except (OSError, HTTPException):
            return {
                "status": 0,
                "body": {
                    "code": "downstream_unavailable",
                    "message": "Scheduling system is temporarily unavailable.",
                },
                "duration_ms": _ms(started),
            }
        finally:
            conn.close()
        if len(raw) > MAX_BYTES:
            parsed = {"code": "invalid_response", "message": "Response was too large."}
        else:
            parsed = _json_object(raw)
        return {"status": status, "body": parsed, "duration_ms": _ms(started)}


def _json_object(raw):
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"code": "invalid_response", "message": "Response was not JSON."}
    if not isinstance(parsed, dict):
        return {"code": "invalid_response", "message": "Response was not a JSON object."}
    return parsed


def _ms(started):
    return round((time.monotonic() - started) * 1000, 1)
