"""Independent schema assertions: never validate by calling the gateway's validator."""

import copy
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

ROOT = Path(__file__).resolve().parents[1]
SPEC = json.loads((ROOT / "contracts/runtime.json").read_text())
URI = "urn:uberpay:reference:contract"
REGISTRY = Registry().with_resource(
    URI, Resource.from_contents(SPEC, default_specification=DRAFT202012)
)


def validate(schema, value):
    document = copy.deepcopy(schema)
    document["components"] = SPEC["components"]
    Draft202012Validator(document, format_checker=FormatChecker()).validate(value)
    check_int64(schema, value)


def check_int64(schema, value):
    if "$ref" in schema:
        schema = resolve(schema["$ref"])
    if schema.get("format") == "int64":
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not -(2**63) <= value < 2**63
        ):
            raise AssertionError("int64 overflow or inexact amount")
    for key in ("allOf",):
        for child in schema.get(key, []):
            check_int64(child, value)
    if isinstance(value, dict):
        for key, child in schema.get("properties", {}).items():
            if key in value:
                check_int64(child, value[key])
    if isinstance(value, list):
        for item in value:
            check_int64(schema.get("items", {}), item)


def resolve(ref):
    node = SPEC
    for segment in ref.removeprefix("#/").split("/"):
        node = node[segment.replace("~1", "/").replace("~0", "~")]
    return node


def operation(method, path):
    return SPEC["paths"][path][method.lower()]


def response(method, path, status, payload, *, grant=None):
    op = operation(method, path)
    # Provisional transient-5xx erratum: infrastructure failures must stay retryable.
    if 500 <= status <= 599 and str(status) not in op["responses"]:
        assert payload is None, "Undeclared transient 5xx must have an empty body"
        return
    assert str(status) in op["responses"], f"{method} {path}: undeclared HTTP {status}"
    item = op["responses"][str(status)]
    if "$ref" in item:
        item = resolve(item["$ref"])
    content = item.get("content", {}).get("application/json")
    if not content:
        assert payload is None, f"{path}: expected empty response, got {payload}"
        return
    schema = content["schema"]
    if path == "/token" and status == 200:
        # Upstream response branches overlap. Do not change upstream oneOf to anyOf:
        # select the response variant using the already validated grant request.
        name = {
            "authorization_code": "RefreshTokenResponse",
            "refresh_token": "AccessTokenResponse",
            "urn:uberpay:params:oauth:grant-type:wallet-grant": "WalletGrantRefreshTokenResponse",
        }[grant]
        schema = {"$ref": f"#/components/schemas/{name}"}
    validate(schema, payload)


def callback(name, payload):
    definition = SPEC["components"]["callbacks"][name]
    op = next(iter(definition.values()))["post"]
    validate(op["requestBody"]["content"]["application/json"]["schema"], payload)
