"""Release gate for the pinned contract's 32 declared operation/status pairs."""

from .contract import SPEC


def report(observed):
    expected = {
        (method.upper(), path, int(status))
        for path, item in SPEC["paths"].items()
        for method, operation in item.items()
        if method in ("get", "post")
        for status in operation.get("responses", {})
        if status.isdecimal()
    }
    seen = expected & observed
    return {
        "declared": len(expected),
        "exercised": len(seen),
        "missing": [
            {"method": m, "path": p, "status": s} for m, p, s in sorted(expected - seen)
        ],
    }
