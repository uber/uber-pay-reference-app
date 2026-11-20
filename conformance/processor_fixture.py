"""Independent local processor: SQLite survives gateway death; no gateway imports.

Only hashes, synthetic routing metadata and decisions are stored, never raw commands,
merchant credentials or KYC. Test barriers are outside the SQLite transaction.
"""

import hashlib
import json
import secrets
import socket
import sqlite3
import tempfile
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Barrier:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()


class ProcessorFixture:
    def __init__(self, path=None, *, port=0, token=None):
        self.directory = tempfile.TemporaryDirectory(prefix="uberpay-processor-")
        self.path = path or str(Path(self.directory.name) / "effects.sqlite")
        self.token = token or secrets.token_hex(24)
        self.lock = threading.RLock()
        self.barriers = deque()
        self.drop_responses = 0
        self.lookups = []
        with self.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS operations (
                client TEXT, key TEXT, command_hash TEXT, operation TEXT, resource TEXT,
                funding TEXT, decision TEXT, effects INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(client,key))""")
        self.server = ThreadingHTTPServer(("127.0.0.1", port), self.handler())
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def db(self):
        return sqlite3.connect(self.path, timeout=5)

    def start(self):
        self.thread.start()
        return self

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        with self.lock:
            for barrier in self.barriers:
                barrier.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.directory.cleanup()

    def block_next(self):
        barrier = Barrier()
        with self.lock:
            self.barriers.append(barrier)
        return barrier

    def entries(self, client):
        with self.db() as db:
            return [
                dict(
                    key=k,
                    operation=op,
                    resource=resource,
                    result=json.loads(result),
                    effects=n,
                )
                for k, op, resource, result, n in db.execute(
                    "SELECT key,operation,resource,decision,effects FROM operations WHERE client=?",
                    (client,),
                )
            ]

    def set_result(
        self, client, key, state, *, version, detail=None, terminal=True, amount=None
    ):
        with self.db() as db:
            value = json.loads(
                db.execute(
                    "SELECT decision FROM operations WHERE client=? AND key=?",
                    (client, key),
                ).fetchone()[0]
            )
            value.update(state=state, version=version, terminal=terminal)
            if amount is not None:
                value["amount"] = amount
            else:
                value.pop("amount", None)
            if detail:
                value["detail"] = detail
            else:
                value.pop("detail", None)
            db.execute(
                "UPDATE operations SET decision=? WHERE client=? AND key=?",
                (json.dumps(value), client, key),
            )
        return dict(provider_key=key, **value)

    def handler(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, status, value=None):
                raw = b"" if value is None else json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def identity(self):
                if not secrets.compare_digest(
                    self.headers.get("Authorization", ""), "Bearer " + fixture.token
                ):
                    self.reply(401)
                    return None
                parts = self.path.split("/")
                if len(parts) != 3 or parts[1] != "operations" or len(parts[2]) != 64:
                    self.reply(404)
                    return None
                return self.headers.get("X-Reference-Client", ""), parts[2]

            def do_GET(self):
                identity = self.identity()
                if identity is None:
                    return
                with fixture.db() as db:
                    entry = db.execute(
                        "SELECT decision FROM operations WHERE client=? AND key=?",
                        identity,
                    ).fetchone()
                with fixture.lock:
                    fixture.lookups.append(identity)
                self.reply(
                    200 if entry else 404, json.loads(entry[0]) if entry else None
                )

            def do_POST(self):
                identity = self.identity()
                if identity is None:
                    return
                size = int(self.headers.get("Content-Length", 0))
                if not 0 < size <= 1048576:
                    return self.reply(400)
                try:
                    command = json.loads(self.rfile.read(size))
                    digest = hashlib.sha256(
                        json.dumps(
                            command,
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ).encode()
                    ).hexdigest()
                    op, request, resource = (
                        command["operation"],
                        command["request"],
                        command["resource"],
                    )
                    with fixture.db() as db:
                        db.execute("BEGIN IMMEDIATE")
                        old = db.execute(
                            "SELECT command_hash,decision FROM operations WHERE client=? AND key=?",
                            identity,
                        ).fetchone()
                        if old:
                            if old[0] != digest:
                                return self.reply(409)
                            result = json.loads(old[1])
                        else:
                            funding = request.get("funding_method", "DEMO_SYNC")
                            if op in ("initRefund", "capture", "cancel"):
                                parent = db.execute(
                                    "SELECT funding FROM operations WHERE client=? AND operation='charge' AND resource=?",
                                    (identity[0], resource),
                                ).fetchone()
                                funding = parent[0] if parent else funding
                            state, detail = "CONFIRMED", None
                            if funding.startswith("DEMO_PENDING_REJECT_"):
                                state, detail = (
                                    "PENDING",
                                    funding.removeprefix("DEMO_PENDING_REJECT_"),
                                )
                            elif funding.startswith("DEMO_REJECT_"):
                                state, detail = (
                                    "REJECTED",
                                    funding.removeprefix("DEMO_REJECT_"),
                                )
                            elif funding in (
                                "DEMO_PENDING",
                                "DEMO_PENDING_REJECT",
                                "DEMO_WEBVIEW",
                                "DEMO_QR",
                                "DEMO_DEPOSIT",
                            ):
                                state = "PENDING"
                            if op == "initRefund" and funding == "DEMO_REFUND_REJECT":
                                state = "REJECTED"
                            if op == "capture":
                                state = "CONFIRMED"
                                if funding in (
                                    "DEMO_CAPTURE_HARD",
                                    "DEMO_CAPTURE_SOFT",
                                ):
                                    state, detail = (
                                        "REJECTED",
                                        "BANK_HARD_ERROR"
                                        if funding.endswith("HARD")
                                        else "BANK_SOFT_ERROR",
                                    )
                            if op == "cancel":
                                state = "CANCELLED"
                            result = {
                                "state": state,
                                "psp_reference": "processor-" + identity[1][:24],
                                "version": 1,
                                "terminal": state != "PENDING",
                            }
                            if detail and state == "REJECTED":
                                result["detail"] = detail
                            db.execute(
                                "INSERT INTO operations(client,key,command_hash,operation,resource,funding,decision) VALUES(?,?,?,?,?,?,?)",
                                (
                                    *identity,
                                    digest,
                                    op,
                                    resource,
                                    funding,
                                    json.dumps(result),
                                ),
                            )
                    with fixture.lock:
                        barrier = (
                            fixture.barriers.popleft()
                            if fixture.barriers and not old
                            else None
                        )
                        drop = fixture.drop_responses > 0
                        if drop:
                            fixture.drop_responses -= 1
                    if barrier:
                        barrier.entered.set()
                        barrier.release.wait(timeout=12)
                    if drop:
                        self.close_connection = True
                        try:
                            self.connection.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        return
                    self.reply(200, result)
                except (ValueError, KeyError, TypeError):
                    self.reply(400)

        return Handler


if __name__ == "__main__":
    import argparse
    import os

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9080)
    parser.add_argument("--state", default=".local/processor.sqlite")
    args = parser.parse_args()
    token = os.environ.get("PROCESSOR_AUTH_TOKEN", "")
    if len(token) < 32:
        parser.error("Set PROCESSOR_AUTH_TOKEN to at least 32 random characters")
    Path(args.state).parent.mkdir(parents=True, exist_ok=True)
    fixture = ProcessorFixture(args.state, port=args.port, token=token).start()
    os.chmod(args.state, 0o600)
    print(
        f"Synthetic durable processor fixture on {fixture.url}; no real funds",
        flush=True,
    )
    try:
        fixture.thread.join()
    except KeyboardInterrupt:
        fixture.close()
