"""Independent, schema-derived negative requests for each public input profile.

Generate only single-fault mutations of valid inputs. The pinned contract remains
the oracle; adopter-specific identity/KYC rules are explicitly outside this corpus.
"""

import copy
import json

from . import contract

WALLET_GRANT = "urn:uberpay:params:oauth:grant-type:wallet-grant"
PROFILES = {
    "charge": ("POST", "/charge", None),
    "charge_access_token": ("POST", "/charge", None),
    "capture": ("POST", "/charge/{id}/capture", None),
    "cancel": ("POST", "/charge/{id}/cancel", None),
    "refund": ("POST", "/refund", None),
    "balance": ("POST", "/balance", None),
    "payout": ("POST", "/payout", None),
    "token_code": ("POST", "/token", "RefreshTokenRequest"),
    "token_refresh": ("POST", "/token", "AccessTokenRequest"),
    "token_wallet": ("POST", "/token", "WalletGrantRefreshTokenRequest"),
}


def shape(schema, sample):
    if "$ref" in schema:
        return shape(contract.resolve(schema["$ref"]), sample)
    merged = {k: v for k, v in schema.items() if k not in ("allOf", "oneOf", "anyOf")}
    children = list(schema.get("allOf", []))
    for keyword in ("oneOf", "anyOf"):
        if keyword in schema:
            matches = []
            for candidate in schema[keyword]:
                try:
                    contract.validate(candidate, sample)
                    matches.append(candidate)
                except Exception:
                    pass
            if not matches:
                raise ValueError("Baseline does not select a valid schema branch")
            children.append(matches[0])
    for child in children:
        child = shape(child, sample)
        merged["properties"] = merged.get("properties", {}) | child.get(
            "properties", {}
        )
        merged["required"] = sorted(
            set(merged.get("required", [])) | set(child.get("required", []))
        )
        merged.update(
            {k: v for k, v in child.items() if k not in ("properties", "required")}
        )
    return merged


def request_schema(profile):
    method, path, name = PROFILES[profile]
    return (
        {"$ref": "#/components/schemas/" + name}
        if name
        else contract.operation(method, path)["requestBody"]["content"][
            "application/json"
        ]["schema"]
    )


def replace(payload, path, value=None, *, delete=False):
    result = copy.deepcopy(payload)
    if not path:
        return copy.deepcopy(value)
    parent = result
    for key in path[:-1]:
        parent = parent[key]
    if delete:
        del parent[path[-1]]
    else:
        parent[path[-1]] = copy.deepcopy(value)
    return result


def mutations(schema, sample):
    """Enumerate required/null/type/enum/declared-bound cases, including nested data."""
    cases = []

    def add(path, rule, value=None, *, delete=False):
        cases.append(
            {
                "id": ".".join(map(str, path or ["$"])) + ":" + rule,
                "path": list(path),
                "rule": rule,
                "delete": delete,
                "value": value,
            }
        )

    def visit(declared, value, path=()):
        node = shape(declared, value)
        kind = node.get("type")
        if kind:
            if not node.get("nullable"):
                add(path, "null")
            wrong = {
                "object": [],
                "array": {},
                "string": 7,
                "integer": "7",
                "number": "7",
                "boolean": "true",
            }[kind]
            add(path, "type", wrong)
        if "enum" in node:
            add(path, "enum", "__not_a_declared_value__")
        if kind == "object":
            for name in node.get("required", []):
                if name not in value:
                    raise ValueError(f"Baseline lacks required field: {path + (name,)}")
                add(path + (name,), "required", delete=True)
            for name, child in node.get("properties", {}).items():
                if name in value:
                    visit(child, value[name], path + (name,))
        elif kind == "array":
            for index, item in enumerate(value):
                visit(node.get("items", {}), item, path + (index,))
        elif kind == "integer":
            add(path, "fraction", 1.5)
            add(path, "boolean", True)
            if node.get("format") == "int64":
                add(path, "int64_high", 2**63)
                add(path, "int64_low", -(2**63) - 1)
        elif kind == "string":
            if node.get("minLength", 0):
                add(path, "min_length", "x" * (node["minLength"] - 1))
            if "maxLength" in node:
                add(path, "max_length", "x" * (node["maxLength"] + 1))
            if node.get("format") == "date-time":
                add(path, "format", "not-an-iso-timestamp")

    contract.validate(schema, sample)
    visit(schema, sample)
    # Each generated request must independently violate the selected schema.
    for case in cases:
        bad = replace(sample, case["path"], case["value"], delete=case["delete"])
        try:
            contract.validate(schema, bad)
        except Exception:
            continue
        raise ValueError(f"Mutation is not a negative contract case: {case['id']}")
    return cases


def case_payload(sample, case):
    return replace(sample, case["path"], case["value"], delete=case["delete"])


def samples():
    """Stable synthetic shapes; execution substitutes fresh IDs and real local grants."""
    return json.loads(
        (contract.ROOT / "conformance/fixtures/validation-inputs.json").read_text()
    )["profiles"]


def query_samples():
    return json.loads(
        (contract.ROOT / "conformance/fixtures/validation-inputs.json").read_text()
    )["query_profiles"]


def authorize_cases(query):
    cases = []
    for name in query:
        for blank in (False, True):
            bad = dict(query)
            if blank:
                bad[name] = ""
            else:
                del bad[name]
            cases.append((f"{name}:{'empty' if blank else 'required'}", bad))
    details = json.loads(query["authorization_details"])
    for case in mutations(
        {
            "type": "array",
            "items": {"$ref": "#/components/schemas/AuthorizationDetail"},
        },
        details,
    ):
        cases.append(
            (
                case["id"],
                query
                | {
                    "authorization_details": json.dumps(case_payload(details, case)),
                },
            )
        )
    return cases + [
        ("scope:unknown", query | {"scope": "not-a-scope"}),
        ("response_type:enum", query | {"response_type": "token"}),
        ("details:empty_array", query | {"authorization_details": "[]"}),
        (
            "redirect:unregistered",
            query | {"redirect_uri": "https://invalid.example/return"},
        ),
    ]


def required_headers(profile):
    method, path, _ = PROFILES[profile]
    values = []
    for parameter in contract.operation(method, path).get("parameters", []):
        if "$ref" in parameter:
            parameter = contract.resolve(parameter["$ref"])
        if parameter.get("in") == "header" and parameter.get("required"):
            values.append(parameter["name"])
    return values


def expected_cases():
    """Semantic receipts required after assertions, not just test discovery."""
    result = set()
    for profile, sample in samples().items():
        result |= {
            profile + "|" + c["id"] for c in mutations(request_schema(profile), sample)
        }
        result |= {
            profile + "|header_" + kind + ":" + name
            for name in required_headers(profile)
            for kind in ("missing", "blank")
        }
        if "amount" in sample or "amount_to_capture" in sample:
            result |= {profile + "|positive_money:" + str(value) for value in (0, -1)}
        result.add(profile + "|control")
    result |= {
        "authorize|" + name for name, _ in authorize_cases(query_samples()["authorize"])
    }
    result.add("authorize|control")
    result |= {
        "lookup|" + name
        for name in ("required", "empty", "not_found", "reference_mismatch", "control")
    }
    result |= {
        "boundaries|" + name
        for name in (
            "description:256",
            "description:257",
            "money:1",
            f"money:{2**63 - 1}",
        )
    }
    return result
