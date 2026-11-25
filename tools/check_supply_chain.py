"""Offline consistency gate for registry-verified image and official-action pins."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check():
    lock = json.loads((ROOT / "contracts/supply-chain-lock.json").read_text())
    files = [
        ROOT / "conformance/Dockerfile",
        *[
            ROOT / "implementations" / lang / "Dockerfile"
            for lang in ("node", "java", "go")
        ],
        ROOT / "compose.yaml",
        ROOT / ".github/workflows/conformance.yml",
    ]
    for file in files:
        text = file.read_text()
        for line in text.splitlines():
            match = re.match(r"\s*(?:FROM|image:)\s+(\S+)", line)
            if match:
                value = match[1]
                name, separator, digest = value.partition("@")
                assert separator and re.fullmatch(r"sha256:[0-9a-f]{64}", digest), (
                    file,
                    value,
                )
                assert lock["images"][name]["digest"] == digest, (file, value)
            action = re.search(r"uses:\s+(\S+)@([^\s]+)", line)
            if action:
                name, commit = action.groups()
                assert re.fullmatch(r"[0-9a-f]{40}", commit), (file, name)
                assert lock["actions"][name]["commit"] == commit
    fixture = (ROOT / "conformance/Dockerfile").read_text()
    assert "USER 10001:10001" in fixture
    compose = (ROOT / "compose.yaml").read_text()
    assert compose.count('volumes: ["public-keys:/keys:ro"]') == 3
    assert "public-keys:/public-keys" in compose
    assert (
        "pull_request_target"
        not in (ROOT / ".github/workflows/conformance.yml").read_text()
    )
    return lock


if __name__ == "__main__":
    data = check()
    print(
        f"PASS: {len(data['images'])} image digests, {len(data['actions'])} action commits, non-root isolated keys"
    )
