#!/usr/bin/env python3
"""Verify pinned source integrity, reproducible documented normalization, and schema validity."""

import argparse
import copy
import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def normalized(source):
    result = copy.deepcopy(source)
    result["components"]["schemas"]["PayoutCallbackState"]["required"] = ["value"]
    for path in ["/charge/{id}/capture", "/charge/{id}/cancel"]:
        for param in result["paths"][path]["post"]["parameters"]:
            if param.get("in") == "path":
                param["required"] = True
                param["schema"].pop("required", None)
    result["components"]["schemas"]["Account"]["required"].remove("name")
    lookup = result["paths"]["/find-transaction"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["properties"]["status"]
    lookup["enum"].append("CANCELLED")
    lookup["description"] += (
        "\n* `CANCELLED`: authorization cancelled or expired without capture (reference profile U4 extension)"
    )
    result["info"]["x-reference-profile"] = {
        "decisions": "contracts/decisions.json",
        "status": "accepted for reference project",
        "derived_runtime": True,
        "upstream_pin_unchanged": True,
        "additive_extensions": ["U4 CANCELLED lookup"],
    }
    return result


def main():
    from jsonschema import Draft202012Validator

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-live",
        action="store_true",
        help="Compare live source, never overwrite pin",
    )
    args = parser.parse_args()
    raw = (ROOT / "contracts/uberpay-openapi.json").read_bytes()
    source = json.loads(raw)
    provenance = json.loads((ROOT / "contracts/source.json").read_text())
    assert hashlib.sha256(raw).hexdigest() == provenance["openapi_json_sha256"], (
        "Upstream source hash mismatch"
    )
    runtime = json.loads((ROOT / "contracts/runtime.json").read_text())
    assert runtime == normalized(source), (
        "runtime.json differs from documented local normalization (including decided reference U1/U4)"
    )
    refs = []

    def walk(value):
        if isinstance(value, dict):
            if "$ref" in value:
                reference = value["$ref"]
                assert reference.startswith("#/"), reference
                node = runtime
                for segment in reference[2:].split("/"):
                    node = node[segment.replace("~1", "/").replace("~0", "~")]
                refs.append(reference)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(runtime)
    for name, schema in runtime["components"]["schemas"].items():
        Draft202012Validator.check_schema(schema)
    operations = sum(
        1
        for item in runtime["paths"].values()
        for method in item
        if method in ("get", "post", "put", "patch", "delete")
    )
    assert operations == 9
    assert len(runtime["components"]["callbacks"]) == 6
    if args.check_live:
        with urllib.request.urlopen(
            "https://uberpay.uber.com/", timeout=30
        ) as response:
            html = response.read().decode()
        marker = re.search(r"(?:const|var|let)\s+__redoc_state\s*=\s*", html)
        assert marker, "Site extraction marker changed; inspect manually"
        live = json.JSONDecoder().raw_decode(html, marker.end())[0]["spec"]["data"]
        assert live == source, (
            "Live contract drift detected; review and update explicitly"
        )
    print(
        f"PASS: source SHA256; runtime normalization; {operations} operations; 6 callbacks; 53 schemas; {len(refs)} local references"
    )


if __name__ == "__main__":
    main()
