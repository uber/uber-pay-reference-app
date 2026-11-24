"""Inspect blocked durable work and explicitly re-drive it without test controls.

Database credentials are operator authority. This utility does not accept or log
payment payloads. Re-drive retains response bytes, event identity and reservations.
"""

import argparse
import json
import os
import sys
from urllib.parse import urlsplit

import psycopg
from psycopg.rows import dict_row


def inspect(db, client):
    callbacks = db.execute(
        """SELECT j.id,j.tx_kind,j.tx_id,j.status,j.attempts,j.last_error,
        count(later.id) AS blocked_events FROM jobs j LEFT JOIN jobs later
        ON later.client_id=j.client_id AND later.tx_kind=j.tx_kind
        AND later.tx_id=j.tx_id AND later.job_type='CALLBACK'
        AND later.event_sequence>j.event_sequence AND later.status<>'DONE'
        WHERE j.client_id=%s AND j.job_type='CALLBACK'
        AND j.status IN ('FAILED','RECONCILE')
        GROUP BY j.id ORDER BY j.id""",
        (client,),
    ).fetchall()
    intents = db.execute(
        """SELECT id,operation_name,status,attempts,failures,last_error
        FROM provider_intents WHERE client_id=%s AND status='RECONCILE' ORDER BY id""",
        (client,),
    ).fetchall()
    failures = db.execute(
        """SELECT id,tx_kind,tx_id,job_type,status,attempts,last_error,next_attempt_at
        FROM jobs WHERE client_id=%s AND job_type IN ('COMPLETION','REVERSAL')
        AND last_error IS NOT NULL AND status<>'DONE' ORDER BY next_attempt_at""",
        (client,),
    ).fetchall()
    return {
        "callback_chain_blocked": callbacks,
        "processor_reconcile": intents,
        "worker_failures": failures,
    }


def redrive(db, client, kind, identity, reason):
    if not reason.strip():
        raise ValueError("A nonempty reason is required")
    # Fixed identifiers, not interpolated caller input.
    if kind == "callback":
        result = db.execute(
            """UPDATE jobs SET status='PENDING',lease_until=NULL,
            next_attempt_at=now(),retry_started_at=now(),retry_count_at_start=attempts,
            last_error=NULL WHERE id=%s AND client_id=%s AND job_type='CALLBACK'
            AND status IN ('FAILED','RECONCILE')
            AND (lease_until IS NULL OR lease_until<now()) RETURNING id""",
            (identity, client),
        ).fetchone()
    else:
        result = db.execute(
            """UPDATE provider_intents SET status='UNCERTAIN',lease_until=NULL,
            next_attempt_at=now(),recovery_started_at=NULL,failures=0,
            generation=generation+1,last_error=NULL
            WHERE id=%s AND client_id=%s AND status='RECONCILE'
            AND (lease_until IS NULL OR lease_until<now()) RETURNING id""",
            (identity, client),
        ).fetchone()
    if not result:
        raise ValueError("No eligible work; refuse active, completed or foreign work")
    db.execute(
        """INSERT INTO operator_actions(client_id,work_kind,work_id,reason)
        VALUES(%s,%s,%s,%s)""",
        (client, kind, identity, reason[:512]),
    )
    return {"redriven": identity, "kind": kind}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    p.add_argument("--client", required=True)
    p.add_argument("--redrive", choices=["callback", "intent"])
    p.add_argument("--id")
    p.add_argument("--reason", default="")
    p.add_argument(
        "--maintain",
        action="store_true",
        help="Purge expired tokens and archive safe old hot rows",
    )
    p.add_argument("--batch-size", type=int, default=200)
    args = p.parse_args()
    if not args.database_url or args.redrive and not args.id:
        p.error("database URL and a re-drive work ID are required")
    if args.maintain and args.redrive:
        p.error("Choose inspection, maintenance or re-drive")
    if urlsplit(args.database_url).hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
        "db",
        "postgres",
    }:
        p.error("Reference operations refuses nonlocal databases")
    with psycopg.connect(args.database_url, row_factory=dict_row) as db:
        if args.maintain:
            value = db.execute(
                "SELECT reference_maintain(%s,%s) AS maintenance",
                (args.client, args.batch_size),
            ).fetchone()
        else:
            value = (
                redrive(db, args.client, args.redrive, args.id, args.reason)
                if args.redrive
                else inspect(db, args.client)
            )
    print(json.dumps(value, default=str))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            json.dumps(
                {
                    "error": "reference_operations_failed",
                    "category": type(error).__name__,
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
