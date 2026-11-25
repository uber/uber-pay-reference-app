"""Independent coverage gates: wire observations plus passed executable requirements."""

import json

from .contract import ROOT, SPEC, resolve


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


def matrix():
    value = json.loads((ROOT / "conformance/coverage-matrix.json").read_text())
    if value.get("version") != 1:
        raise ValueError("Unsupported coverage matrix version")
    return value


def operations():
    return {
        (method.upper(), path)
        for path, item in SPEC["paths"].items()
        for method in item
        if method in ("get", "post")
    }


def callback_pairs():
    return {
        (name, int(status))
        for name, definition in SPEC["components"]["callbacks"].items()
        for operation in definition.values()
        for status in operation["post"]["responses"]
        if status.isdecimal()
    }


def schema_fields(schema, prefix=()):
    if "$ref" in schema:
        return schema_fields(resolve(schema["$ref"]), prefix)
    result = set()
    for keyword in ("allOf", "oneOf", "anyOf"):
        for child in schema.get(keyword, []):
            result |= schema_fields(child, prefix)
    for name, child in schema.get("properties", {}).items():
        path = prefix + (name,)
        result.add(".".join(path))
        result |= schema_fields(child, path)
    if "items" in schema:
        result |= schema_fields(schema["items"], prefix + ("*",))
    return result


def sample_fields(value, prefix=()):
    result = set()
    if isinstance(value, dict):
        for name, child in value.items():
            path = prefix + (name,)
            result.add(".".join(path))
            result |= sample_fields(child, path)
    elif isinstance(value, list):
        for child in value:
            result |= sample_fields(child, prefix + ("*",))
    return result


def field_report(passed):
    from .validation import PROFILES, request_schema, samples

    expected, seen = set(), set()
    for profile, sample in samples().items():
        method, path, _ = PROFILES[profile]
        fields = schema_fields(request_schema(profile))
        expected |= {(method, path, field) for field in fields}
        if f"test_validation_{profile}" in passed:
            seen |= {(method, path, field) for field in fields & sample_fields(sample)}
    return {
        "declared": len(expected),
        "exercised": len(expected & seen),
        "missing": [list(key) for key in sorted(expected - seen)],
        "scope": "JSON request properties; not response fields or arbitrary field combinations",
    }


def integrity(test_names):
    """Fail on spec drift, stale mappings, duplicate IDs or an unmapped workflow."""
    from .scenario import bundles, load
    from .validation import PROFILES

    requirements = matrix()["requirements"]
    ids = [row["id"] for row in requirements]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate coverage requirement IDs")
    expected_dimensions = {
        "operation": operations(),
        "callback": set(SPEC["components"]["callbacks"]),
        "workflow": {
            scenario["name"] for bundle in bundles() for scenario in load(bundle)
        },
        "validation": set(PROFILES) | {"authorize", "lookup"},
    }
    for dimension, expected in expected_dimensions.items():
        rows = [row for row in requirements if row["dimension"] == dimension]
        actual = [
            (row["method"], row["path"]) if dimension == "operation" else row["subject"]
            for row in rows
        ]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise ValueError(f"Coverage matrix does not match {dimension} inventory")
    for row in requirements:
        if not row.get("tests") or set(row["tests"]) - set(test_names):
            raise ValueError(f"Unresolved executable evidence: {row['id']}")
    callback_tests = {
        test
        for row in requirements
        if row["dimension"] == "callback"
        for test in row["tests"]
    }
    if any(
        f"test_callback_{name}_http_{status}" not in callback_tests
        for name, status in callback_pairs()
    ):
        raise ValueError("Callback response matrix is incomplete")


def qualification(observed, events, cases, *, full_run, validation_observed=()):
    from .validation import expected_cases

    passed = {
        case["name"].rsplit(".", 1)[-1] for case in cases if case["status"] == "passed"
    }
    requirements = []
    for row in matrix()["requirements"]:
        missing = sorted(set(row["tests"]) - passed)
        requirements.append(
            {
                "id": row["id"],
                "dimension": row["dimension"],
                "passed": not missing,
                "missing_tests": missing,
            }
        )
    expected_ops = operations()
    actual_ops = {(method.upper(), path) for method, path, _ in observed}
    expected_callbacks = callback_pairs()
    actual_callbacks = {
        (event["name"], event["status"]) for event in events if not event["error"]
    }
    endpoints = {
        "declared": len(expected_ops),
        "exercised": len(expected_ops & actual_ops),
        "missing": [list(key) for key in sorted(expected_ops - actual_ops)],
    }
    callbacks = {
        "declared": len(expected_callbacks),
        "exercised": len(expected_callbacks & actual_callbacks),
        "missing": [list(key) for key in sorted(expected_callbacks - actual_callbacks)],
    }
    fields = field_report(passed)
    expected_validation = expected_cases()
    actual_validation = set(validation_observed)
    validation = {
        "declared": len(expected_validation),
        "exercised": len(expected_validation & actual_validation),
        "missing": sorted(expected_validation - actual_validation),
        "asserted": sorted(actual_validation),
    }
    statuses = report(observed)
    complete = (
        all(row["passed"] for row in requirements)
        and not any(
            section["missing"]
            for section in (endpoints, callbacks, fields, statuses, validation)
        )
        and all(case["status"] == "passed" for case in cases)
        and bool(cases)
    )
    return {
        "version": 1,
        "scope": matrix()["scope"],
        "full_run": full_run,
        "qualified": full_run and complete,
        "endpoints": endpoints,
        "responses": statuses,
        "callback_responses": callbacks,
        "request_fields": fields,
        "request_validation": validation,
        "requirements": requirements,
        "exclusions": matrix()["exclusions"],
    }
