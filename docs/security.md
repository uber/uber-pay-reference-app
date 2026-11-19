# Security boundaries

This repository handles synthetic local requests only. It must never be exposed as an unaudited payment processor.

- RSA public keys are validated and signed headers parsed with bounded, strict rules. Digest/signature authentication uses raw bytes before money processing; bearer authorization precedes idempotency replay.
- The gateway refuses real serving modes, external callback/token hosts and remote database hosts in local mode. Callback/token clients do not follow redirects. Container-facing service ports bind to loopback on the host.
- `.local/`, `.env`, database files, build outputs and conformance artifacts are ignored. Local private keys are generated with restrictive permissions; real secrets must never be put into fixture files or Docker images.
- `/_test/*` endpoints are **disabled by default**. The harness enables them only with a fresh random secret and `X-Test-Control-Secret`. They are not additional Uber Pay endpoints and are not part of a partner deployment.
- `/demo/*` is an explicit local identity/payment-consent experience, not a user authentication implementation. Consent submissions are POST and CSRF-bound, not GET side effects or automatic approvals.
- Error output and logs must not expose private keys, signatures, bearer/refresh/code values or real account/payee details. Reports contain only synthetic test data.
- Before external execution, add reviewed operation-intent dispatch/reconciliation, real PSP and wallet providers, secret management, key rotation, TLS/edge controls, rate limits and data retention/access controls. The fake provider's in-database critical section must not wrap an external network call.

Report a vulnerability privately to the repository maintainer/owning team. Do not post credentials or exploit material containing personal/payment data in public issues.

## Review follow-up controls

All supported gateways bound SQL lock wait to 1500ms and statements to 5000ms. Authentication and persisted-account ownership checks precede replay. Active and idle Node connection failures are handled without unhandled error events. Browser action capabilities are256 random bits, scoped to a WAITING job, CSRF-protected, single-use and expiring; GET never mints authority from an Uber transaction ID. Logs omit raw errors, credentials, merchant secrets and personal request fields. Redirects use exact configured URIs. Optional quoted signature created/expires are additional bounds only and cannot replace/extend signed Date freshness.

Only synthetic fixtures may be used locally. Merchant secrets/KYC/customer data are transient adapter inputs, not retained by the reference store or logs. Source settlement ID, payee routing ID and order ID remain in the disposable local database until the developer deletes the volume; there is no automatic production TTL/erasure service. A PSP must specify purpose, encryption, access control, approved retention, erasure, KYC and audit policy before external use.
