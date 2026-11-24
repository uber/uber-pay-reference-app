# Owner-completed reference release — November 24, 2026

## Completion boundary

The implementation, accepted reference policies and ERD cover a cloneable
**local/synthetic** PSP reference in Node.js, Java and Go. There is no unresolved
reference-owner backlog. This does not assert external API-owner approval,
production/SOX/privacy certification, real bank/KYC integration or sandbox use.
PR #1 remains draft for human review; completing code is not permission to merge.

U1–U15 and D1–D3 remain in the decision register. O1–O7 finish the remaining
choices: strict default reversal example; charge/refund-only lookup; safe
retention/archive and public limits; stable runtime/instrument identity; stronger
semantic release gates; and immutable/non-root supply chain. See
[the register](../contracts/decisions.json) for rationale and named evidence,
[operations](operations.md) for CLI and retention mechanics, and
[integration](psp-integration.md) for real-adapter field responsibilities.

## Executed evidence

`make verify DATABASE_URL=postgresql://uberpay@127.0.0.1:55432/uberpay` passes the
same129 wire cases per language (387 total), with zero failures/errors/skips and
32/32 declared operation/status pairs each. Python units35; Node14; Java13;
Go race tests/vet; isolated migration up/repeated-up/down; contract hashes/runtime
normalization and lint/format pass. The exact log is
`.artifacts/owner2-final-verify.log`, with machine-readable JSON/JUnit under
`.artifacts/conformance/`.

Independent cases add cold replay and protected unresolved work, malformed/
localized error parity, immediate persisted expiry despite409, OAuth internal
failure redirects and bounded public floods alongside money calls, namespace/key
mismatch, identity stability during client-secret rotation, actual ledger deltas
and provider lookup after crashes/lost responses,100000 historical rows with an
expiry-index plan, and multi-source/currency asynchronous/deposit/expiry journal
production, duplicate effects, simultaneous seals and commit-clock boundaries.

The offline supply-chain gate checks seven registry-verified image digests,
six official Action commit pins, non-root fixture execution and public-only
gateway key mounts. Registry metadata was checked on November24,2026; mutable
source tags are not silently refreshed. Local Docker was unavailable: current-head
CI performs container clone/run smoke; read its result rather than infer it from
native local runs. `aifx verify` finds no monorepo/custom validators here, so the
explicit checks above, not that no-op, are the qualification evidence.

## Adopt rather than bypass safeguards

- Select a fresh CLIENT_ID/Compose project for incompatible historical local state
  or another native runtime. Do not force-bind or delete uncertain financial work.
- Preserve the processor encryption key/configuration and independent instrument
  key. The disclosed development default is synthetic, not a production secret.
- Default refund reversal is synchronous201 CONFIRMED followed by one REJECTED
  callback. The ambiguous async two-update demo requires an explicit test flag;
  any actual local financial inverse is still applied exactly once.
- Public lookup remains charge/refund only per the published specification.
  Reconcile payouts by their authoritative provider key and callback history.
- Supply real wallet identity/KYC, funding/account mappings, processor transport,
  credentials, bilateral report mapping, security/financial controls and external
  qualification before a real deployment. This release intentionally has no such
  serving mode or live traffic to migrate.

## ERD publication evidence

The existing Google Doc is updated in place after successful native verification:
Main ERD, separate Low Level Design and Other considerations. Original tab
identities and integration diagram are preserved. Template answers/dependency/
migration cells and applicability sections are completed, not left as placeholders.
Fresh read-backs and edit evidence are stored outside the application repository
in the sibling context/erd folder. No Google sharing settings or permissions change.
