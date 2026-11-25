#!/usr/bin/env python3
"""Optional disposable native PostgreSQL instance; no background OS service registration."""

import argparse
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("action", choices=["start", "stop"])
parser.add_argument("--port", type=int, default=55432)
args = parser.parse_args()
bindir = Path(os.environ.get("PG_BIN", "/opt/homebrew/opt/postgresql@17/bin"))


def binary(name):
    result = shutil.which(name) or (
        str(bindir / name) if (bindir / name).exists() else None
    )
    if not result:
        raise SystemExit(
            "Install PostgreSQL17 or set PG_BIN; Docker Compose is the default alternative."
        )
    return result


state = ROOT / ".local/postgres"
state.parent.mkdir(exist_ok=True)
if args.action == "stop":
    if (state / "postmaster.pid").exists():
        subprocess.run(
            [binary("pg_ctl"), "-D", str(state), "stop", "-m", "fast"], check=True
        )
else:
    if not (state / "PG_VERSION").exists():
        subprocess.run(
            [binary("initdb"), "-D", str(state), "-A", "trust", "-U", "uberpay"],
            check=True,
        )
    if not (state / "postmaster.pid").exists():
        subprocess.run(
            [
                binary("pg_ctl"),
                "-D",
                str(state),
                "-l",
                str(ROOT / ".local/postgres.log"),
                "-o",
                f"-p {args.port} -h 127.0.0.1",
                "start",
            ],
            check=True,
        )
    exists = subprocess.run(
        [
            binary("psql"),
            "-h",
            "127.0.0.1",
            "-p",
            str(args.port),
            "-U",
            "uberpay",
            "-d",
            "postgres",
            "-tAc",
            "SELECT 1 FROM pg_database WHERE datname='uberpay'",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    if exists.stdout.strip() != "1":
        subprocess.run(
            [
                binary("createdb"),
                "-h",
                "127.0.0.1",
                "-p",
                str(args.port),
                "-U",
                "uberpay",
                "uberpay",
            ],
            check=True,
        )
    print(
        f"Local-only trust-auth DB: postgresql://uberpay@127.0.0.1:{args.port}/uberpay"
    )
