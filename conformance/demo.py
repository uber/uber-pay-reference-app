"""Send a signed fake-money charge, then query it. No real Uber credentials required."""

import argparse, json, uuid
from datetime import datetime, timezone
from pathlib import Path
from .contract import ROOT
from .wire import Client, keys

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080/uberpay/v1")
    parser.add_argument("--keys", default=".local")
    args = parser.parse_args()
    client = Client(args.url, keys(Path(args.keys)))
    payload = json.loads(
        (ROOT / "conformance/fixtures/charge.json")
        .read_text()
        .replace("$id", uuid.uuid4().hex)
        .replace("$now", datetime.now(timezone.utc).isoformat())
    )
    result = client.request("POST", "/charge", payload, idem=uuid.uuid4().hex)
    print("Charge:", result.status, result.raw.decode())
    if result.status != 200:
        raise SystemExit(1)
    result = client.request("GET", "/find-transaction?id=" + payload["id"])
    print("Lookup:", result.status, result.raw.decode())
    raise SystemExit(0 if result.status == 200 else 1)
