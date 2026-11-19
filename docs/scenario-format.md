# Shared scenario format v1

`conformance/scenario.py` interprets versioned JSON steps; it does not dispatch
Python test names or evaluate expressions. `conformance/fixtures/scenarios.json`
and `recovery-scenarios.json` contain ten declarative workflows. They run unchanged
against Node, Java and Go, alongside independent Python regression/fault probes.
The suite is deliberately **not described as wholly JSON**: the deterministic
processor barriers and multi-process lease tests also use Python orchestration.

A bundle is `{ "version": 1, "scenarios": [{ "name": "unique", "steps": [...] }] }`.
A value exactly `${binding.path}` preserves its JSON type, including integers above
2^53; embedded interpolation creates a string. Array indices work in field paths.
Unknown bindings and unsupported operations/versions fail rather than skip.

| Step `op` | Meaning |
|---|---|
| `http` (default) | method/path, fixture or payload, deep `set`, request options, exact status/header/body assertions, grant-selected schema, typed `bind` |
| `let`, `assert` | Typed bindings, generated UUID, exact equality; no arbitrary code evaluation |
| `parallel` | Explicit branches, isolated binding copies, collected branch results |
| `restart` | Graceful or hard native-process restart; durable namespace retained |
| `form`, `form_submit` | Follow only the current local gateway origin; parse hidden capability/CSRF fields and submit an explicit decision |
| `callback`, `callback_fault` | Await and assert a specific version/payload; inject receiver statuses |
| `with_sql_fault` | Scoped allowlisted-table trigger using SQLSTATE40001/55P03, always removed in `finally` |

HTTP `expect.body` uses dotted paths; `expect.headers` is case insensitive.
`contract_path` selects the declared template (e.g. `/charge/{id}/capture`), and
schema validation is independent of gateway code. Dynamic references are bound,
not broadly normalized away. Versioned scenarios cover charge/lookup, manual
capture/cancel, pending callbacks, refunds, typed replay under concurrency,
transient SQL recovery with the same key, action restart and ordered reversal.

Run `make conformance LANGUAGE=all`. The release run rejects skips and missing
operation/status pairs. A `--match` development run does not qualify a release.
JSON/JUnit reports preserve parent cases and failing subscenarios.
