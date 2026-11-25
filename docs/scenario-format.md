# Shared testing, coverage and scenario format

## Run and interpret the suite

Follow the [native prerequisites and setup](../README.md#run-the-same-conformance-suite-against-all-three-languages).
Use a disposable local PostgreSQL database; the harness injects scoped SQL faults
and controls gateway processes. From the repository root:

```sh
make conformance LANGUAGE=go       # alternatives: node, java or all
```

For the whole repository, after installing all three native toolchains:

```sh
make build
make verify
```

Reports are written to `.artifacts/conformance/`:

| File | Purpose |
|---|---|
| `summary.json` | Combined per-language test results and coverage assessments |
| `<language>.json` / `<language>.xml` | Individual results, callback observations and JUnit output |
| `<language>.coverage.json` | Qualification decision, missing evidence and explicit exclusions |
| `<language>.log` | Native gateway diagnostics using synthetic data only |

Full runs require every planned test to pass without skips and every coverage
dimension below to be satisfied. A filtered `--match` run is useful for debugging
but always reports `qualified: false`.

## Corpus and coverage gates

The [machine-readable matrix](../conformance/coverage-matrix.json) connects public
operations, callbacks, workflows, validation profiles and risk-based lifecycle
requirements to executable tests. These tests run unchanged against every language.

| Surface | Required evidence |
|---|---|
| Public operations | All nine endpoints, including the browser/redirect authorization flow |
| Inbound responses | Every numeric operation/status pair declared by the pinned runtime contract |
| Callback responses | Every declared status for each callback; authentication refresh, permanent failure, conflict/reconciliation and transient recovery are asserted independently |
| Callback resilience | Per-callback lost response, HTTP 503, retry exhaustion and integer/HTTP-date `Retry-After`; preserve payload and applicable idempotency key |
| Request validation | Schema-derived missing/null/type/enum/length/format/int64 cases, nonpositive-money rejection, required headers, query failures and valid controls |
| Request fields | Every declared JSON request property represented in a successfully exercised input profile, across authorization/payment-data and token-grant alternatives |
| Lifecycle behavior | Named money, ownership, concurrency, expiry, crash, callback ordering, localization, retention and settlement invariants |

Validation cases issue receipts only after their wire assertions succeed. The gate
checks those individual receipts as well as passed test names; discovering a test
or hitting an HTTP status is not sufficient. Invalid-input batches also verify
that account balances do not change. Optional payout identity/address/document
fields and refund order data are checked at the independent processor boundary
using a hash of the full received command, without storing raw identity data.

`GET /authorize` has no response statuses declared in the pinned spec, so the
inbound status counter alone does not cover it. Its endpoint/workflow/validation
requirements are separate. Callback response coverage is also separate from
inbound response coverage.

| Fixture | Purpose |
|---|---|
| `scenarios.json` | Six core charge, capture/cancel, lookup and payout workflows |
| `recovery-scenarios.json` | Four SQL, duplicate, restart and reversal workflows |
| `integration-scenarios.json` | Six OAuth/balance, payout callback and opt-in legacy deposit workflows |
| `validation-inputs.json` | Rich synthetic body/query examples for all input profiles; negative cases derive from the contract at runtime |
| `callbacks.json` | Schema-valid payload examples for all six callback types |
| `charge.json`, `payout.json`, `balance.json`, `wallet-grant.json` | Reusable request payloads |
| `invalid-charge.json` | Additional hand-authored money/format regressions |
| `published-authentication-charge.json` | Unmodified upstream example, including its documented missing required fields |

The scope is the local synthetic reference, not exhaustive field permutations,
all country/currency combinations, real national-document validation, real
identity/KYC/SCA, production load testing or Uber sandbox certification.
Response-field permutations are not exhaustively enumerated. The matrix and
generated reports retain those exclusions explicitly.

## Scenario format v1

`conformance/scenario.py` interprets versioned JSON steps; it does not dispatch
Python test names or evaluate expressions. The three `*scenarios.json` bundles
contain sixteen declarative workflows. Each is discovered as its own named test
against Node, Java and Go, alongside independent Python regression/fault probes.
The suite is deliberately **not described as wholly JSON**: the deterministic
processor barriers and multi-process lease tests also use Python orchestration.

A bundle is `{ "version": 1, "scenarios": [{ "name": "unique", "steps": [...] }] }`.
A value exactly `${binding.path}` preserves its JSON type, including integers above
2^53; embedded interpolation creates a string. Array indices work in field paths.
Unknown bindings and unsupported operations/versions fail rather than skip.

| Step `op` | Meaning |
|---|---|
| `http` (default) | method/path, fixture or payload, deep `set`, structured `query`, request options, exact status/header/body assertions, grant-selected schema, typed `bind` |
| `let`, `assert` | Typed bindings, generated UUID, exact equality; no arbitrary code evaluation |
| `parallel` | Explicit branches, isolated binding copies, collected branch results |
| `restart` | Graceful or hard native-process restart; durable namespace retained |
| `form`, `form_submit` | Follow only the current local gateway origin; parse hidden capability/CSRF fields and submit an explicit decision |
| `callback`, `callback_fault` | Await and assert a specific version/payload; inject receiver statuses |
| `with_sql_fault` | Scoped allowlisted-table trigger using SQLSTATE40001/55P03, always removed in `finally` |

Structured query objects/arrays are JSON-encoded before URL encoding, supporting
`authorization_details` without hand-built escaping. `browser: true` selects
explicit HTML assertions instead of JSON response validation. `save` retains the
page for `form` with `page`; the form must contain capability/CSRF fields.
`bind_redirect` extracts exactly one named redirect query value, such as an
authorization code. `expect.redirect` checks the registered scheme/authority/path
and expected correlation parameters. OAuth grant response schemas are selected
with the step's `grant`.

HTTP `expect.body` uses dotted paths; `expect.headers` is case insensitive.
`contract_path` selects the declared template (e.g. `/charge/{id}/capture`), and
schema validation is independent of gateway code. Dynamic references are bound,
not broadly normalized away. Versioned scenarios cover charge/lookup, manual
capture/cancel, pending callbacks, refunds, typed replay under concurrency,
transient SQL recovery with the same key, action restart and ordered reversal.

## Extend the corpus

Add a version-1 workflow to a `*scenarios.json` bundle and add its
`test_workflow_<name>` evidence to the matrix's workflow and relevant operation /
callback rows. New public operations or callback statuses require matrix evidence;
the inventory check rejects unmapped contract changes. Extend
`validation-inputs.json` when introducing a new optional request field so its
valid value and negative mutations are exercised. Keep complex barriers and
multi-process faults in Python and map their named tests to lifecycle requirements.

Run `make conformance LANGUAGE=all` before qualifying changes. JSON/JUnit reports
preserve individual workflow results and failing subscenarios.
