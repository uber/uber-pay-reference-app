#!/usr/bin/env python3
"""Exercise up/down SQL in an isolated schema, then roll the entire check back."""

import argparse
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    args = parser.parse_args()
    if not args.database_url:
        raise SystemExit(
            "Pass --database-url for a disposable local PostgreSQL database"
        )
    if urlsplit(args.database_url).hostname not in (
        "localhost",
        "127.0.0.1",
        "::1",
        "db",
        "postgres",
    ):
        raise SystemExit("Migration test refuses remote databases")
    schema = "migration_test_" + uuid.uuid4().hex
    with psycopg.connect(args.database_url) as connection:
        try:
            connection.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
            )
            connection.execute(
                sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema))
            )
            up = (ROOT / "storage/migrations/001_init.sql").read_text()
            connection.execute(up)
            connection.execute(up)  # Startup migration must be idempotent.
            tables = connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname=%s", (schema,)
            ).fetchall()
            assert {row[0] for row in tables} == {
                "settlement_entries",
                "settlement_batches",
                "settlement_batch_files",
                "settlement_deliveries",
                "accounts",
                "transactions",
                "operations",
                "jobs",
                "tokens",
                "account_debts",
                "refund_reversals",
                "provider_intents",
                "provider_keys",
                "resource_claims",
                "intent_reservations",
            }
            connection.execute((ROOT / "storage/migrations/001_down.sql").read_text())
            assert (
                connection.execute(
                    "SELECT count(*) FROM pg_tables WHERE schemaname=%s", (schema,)
                ).fetchone()[0]
                == 0
            )
        finally:
            connection.rollback()  # Never retain the test schema or touch reference state.
    print(
        "PASS: isolated PostgreSQL migration up, repeated up, down; test schema rolled back"
    )


if __name__ == "__main__":
    main()
