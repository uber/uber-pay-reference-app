"""Local-only Uber callback receiver with independent contract/auth checks and faults."""

import argparse
import json
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from . import contract

PROFILE = json.loads(
    (contract.ROOT / "contracts/implementation-profile.json").read_text()
)


class MockUber:
    def __init__(self, host="127.0.0.1", port=9090):
        self.lock = threading.RLock()
        self.events, self.tokens = [], {}
        self.faults = defaultdict(deque)
        self.token_requests = 0
        self.barriers = defaultdict(deque)
        self.server = ThreadingHTTPServer((host, port), self.handler())
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def fail_next(self, name, transaction_id, *statuses):
        with self.lock:
            self.faults[(name, transaction_id)].extend(statuses)

    def block_callback(self, name, transaction_id):
        from .processor_fixture import Barrier

        barrier = Barrier()
        with self.lock:
            self.barriers[(name, transaction_id)].append(barrier)
        return barrier

    def matching(self, name, transaction_id, accepted=True):
        with self.lock:
            return [
                event
                for event in self.events
                if event["name"] == name
                and event["id"] == transaction_id
                and (not accepted or 200 <= event["status"] < 300)
            ]

    def handler(self):
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, status, value=None):
                body = json.dumps(value).encode() if value is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                if urlsplit(self.path).path in ("/health", "/return"):
                    return self.reply(200, {"mode": "local", "funds": "synthetic"})
                self.reply(404)

            def do_POST(self):
                size = int(self.headers.get("Content-Length", "0"))
                if size > 1048576:
                    return self.reply(413)
                raw = self.rfile.read(size)
                if self.path == "/oauth/token":
                    try:
                        value = (
                            json.loads(raw)
                            if "application/json"
                            in self.headers.get("Content-Type", "")
                            else {k: v[0] for k, v in parse_qs(raw.decode()).items()}
                        )
                        if (
                            value.get("client_id") != "demo-uber-client"
                            or value.get("client_secret") != "demo-uber-secret"
                        ):
                            return self.reply(401)
                        if value.get("grant_type") != "client_credentials" or value.get(
                            "scope"
                        ) not in ("payments.deposits", "payments.payouts"):
                            return self.reply(400)
                        token = secrets.token_urlsafe(24)
                        with mock.lock:
                            mock.tokens[token] = (value["scope"], time.time() + 30)
                            mock.token_requests += 1
                        return self.reply(
                            200,
                            {
                                "access_token": token,
                                "token_type": "Bearer",
                                "expires_in": 30,
                            },
                        )
                    except (ValueError, KeyError):
                        return self.reply(400)
                name = transaction_id = None
                for callback, config in PROFILE["callbacks"].items():
                    pattern = re.escape(config["path"]).replace(
                        re.escape("{id}"), "([^/]+)"
                    )
                    match = re.fullmatch(pattern, urlsplit(self.path).path)
                    if match:
                        name, transaction_id = callback, unquote(match[1])
                        break
                if not name:
                    return self.reply(404)
                status, error, payload, scope = 200, None, None, None
                try:
                    payload = json.loads(raw)
                    contract.callback(name, payload)
                    supplied = self.headers.get("Authorization", "").removeprefix(
                        "Bearer "
                    )
                    with mock.lock:
                        scope, expiration = mock.tokens.get(supplied, (None, 0))
                    if (
                        expiration < time.time()
                        or scope != PROFILE["callbacks"][name]["scope"]
                    ):
                        status, error = 401, "wrong or expired callback scope"
                    if PROFILE["callbacks"][name][
                        "idempotency_header"
                    ] and not self.headers.get("X-Idempotency-Key"):
                        status, error = 400, "missing callback idempotency key"
                    if not PROFILE["callbacks"][name][
                        "idempotency_header"
                    ] and self.headers.get("X-Idempotency-Key"):
                        status, error = 400, "unexpected callback idempotency key"
                except Exception as exc:
                    status, error = 400, str(exc)[:300]
                with mock.lock:
                    if status == 200 and mock.faults[(name, transaction_id)]:
                        status = mock.faults[(name, transaction_id)].popleft()
                    mock.events.append(
                        {
                            "name": name,
                            "id": transaction_id,
                            "payload": payload,
                            "status": status,
                            "scope": scope,
                            "error": error,
                            "idempotency_key": self.headers.get("X-Idempotency-Key"),
                        }
                    )
                with mock.lock:
                    barrier = (
                        mock.barriers[(name, transaction_id)].popleft()
                        if mock.barriers[(name, transaction_id)]
                        else None
                    )
                if barrier:
                    barrier.entered.set()
                    barrier.release.wait(timeout=12)
                self.reply(status)

        return Handler


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9090)
    args = parser.parse_args()
    receiver = MockUber("0.0.0.0", args.port).start()
    print(
        f"Mock Uber listening on :{args.port}; synthetic credentials only", flush=True
    )
    try:
        receiver.thread.join()
    except KeyboardInterrupt:
        receiver.close()
