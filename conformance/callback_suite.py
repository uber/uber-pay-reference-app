"""Per-callback response policies proved at the actual native worker boundary."""

import json
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from . import contract
from .wire import eventually


def callback_cases():
    return json.loads(
        (contract.ROOT / "conformance/fixtures/callbacks.json").read_text()
    )


def statuses(name):
    return sorted(
        int(status)
        for status in next(
            iter(contract.SPEC["components"]["callbacks"][name].values())
        )["post"]["responses"]
        if status.isdecimal()
    )


def callback_test_name(name, status):
    return f"test_callback_{name}_http_{status}"


class CallbackCoverageSuite:
    def callback_record(self, transaction_id):
        with self.db() as db:
            return db.execute(
                """SELECT status,attempts,last_error FROM jobs
                WHERE client_id=%s AND tx_id=%s AND job_type='CALLBACK'""",
                (self.client_id, transaction_id),
            ).fetchone()

    def assert_callback_policy(
        self, name, status, *, retry_after=None, exhausted=False
    ):
        identity = self.uid()
        payload = callback_cases()[name]
        faults = [status] * (5 if exhausted else 1) if status != 200 else []
        self.mock.fail_next(name, identity, *faults)
        if retry_after is not None:
            self.mock.retry_after[(name, identity)] = retry_after
        reply = self.client.control(
            "POST", "callback", {"name": name, "id": identity, "payload": payload}
        )
        self.assertEqual(reply.status, 200, reply.raw)
        retryable = status in (0, 401, 429) or status >= 500
        expected = (
            "FAILED"
            if exhausted
            else "DONE"
            if status == 200 or retryable
            else "RECONCILE"
            if status == 409
            else "FAILED"
        )
        row = eventually(
            lambda: (
                row
                if (row := self.callback_record(identity)) and row[0] == expected
                else None
            ),
            timeout=15,
            description=f"{name} HTTP {status} reaches {expected}",
        )
        events = self.mock.matching(name, identity, accepted=False)
        expected_statuses = faults + ([] if exhausted or not retryable else [200])
        if status == 200:
            expected_statuses = [200]
        self.assertEqual([event["status"] for event in events], expected_statuses)
        self.assertEqual(int(row[1]), len(expected_statuses))
        self.assertTrue(all(event["payload"] == payload for event in events))
        self.assertTrue(all(event["error"] is None for event in events))
        self.assertEqual(
            {event["scope"] for event in events},
            {"payments.payouts" if name == "payoutCallback" else "payments.deposits"},
        )
        keys = {event["idempotency_key"] for event in events}
        if name == "updateRefund":
            self.assertEqual(keys, {None})
        else:
            self.assertEqual(len(keys), 1)
            self.assertNotIn(None, keys)
        if status == 401:
            self.assertNotEqual(
                events[0]["token_fingerprint"], events[1]["token_fingerprint"]
            )
        if retry_after is not None:
            # Allow coarse HTTP-date precision and scheduler jitter, but not immediate retry.
            required = 0.7 if retry_after == "1" else 0.5
            self.assertGreaterEqual(events[1]["time"] - events[0]["time"], required)
        if expected in ("FAILED", "RECONCILE"):
            # Diagnostic wording is implementation-specific; retained, inspectable
            # state and the independently observed HTTP response are the contract.
            self.assertIsInstance(row[2], str)
            self.assertTrue(row[2].strip())
            time.sleep(0.15)
            self.assertEqual(
                len(self.mock.matching(name, identity, accepted=False)), len(events)
            )
        return identity, events


def install_callbacks(cls):
    for name in callback_cases():
        for status in statuses(name):

            def test(self, callback=name, code=status):
                self.assert_callback_policy(callback, code)

            setattr(cls, callback_test_name(name, status), test)
        for kind in (
            "unavailable",
            "lost_response",
            "exhaustion",
            "retry_after_seconds",
            "retry_after_date",
        ):

            def test(self, callback=name, failure=kind):
                if failure == "retry_after_seconds":
                    self.assert_callback_policy(callback, 503, retry_after="1")
                elif failure == "retry_after_date":
                    when = format_datetime(
                        datetime.now(timezone.utc) + timedelta(seconds=2), usegmt=True
                    )
                    self.assert_callback_policy(callback, 503, retry_after=when)
                else:
                    self.assert_callback_policy(
                        callback,
                        0 if failure == "lost_response" else 503,
                        exhausted=failure == "exhaustion",
                    )

            setattr(cls, f"test_callback_{name}_{kind}", test)
