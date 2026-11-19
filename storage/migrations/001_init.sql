-- Local reference schema; no real financial data. All money is signed int64 E5.
CREATE TABLE IF NOT EXISTS accounts (
 client_id TEXT NOT NULL, account_id TEXT NOT NULL, currency TEXT NOT NULL,
 available_e5 BIGINT NOT NULL CHECK (available_e5 >= 0), balance_e5 BIGINT NOT NULL CHECK (balance_e5 >= 0),
 PRIMARY KEY(client_id,account_id,currency), CHECK (balance_e5 >= available_e5)
);
CREATE TABLE IF NOT EXISTS transactions (
 client_id TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL, psp_reference TEXT NOT NULL,
 original_id TEXT, account_id TEXT NOT NULL, amount_e5 BIGINT NOT NULL CHECK(amount_e5>0), currency TEXT NOT NULL,
 capture_method TEXT NOT NULL DEFAULT 'AUTOMATIC', funding_method TEXT NOT NULL, state TEXT NOT NULL, detail TEXT,
 request_hash TEXT NOT NULL, response_status INTEGER NOT NULL, response_body TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), expires_at TIMESTAMPTZ,
 PRIMARY KEY(client_id,kind,id), UNIQUE(client_id,kind,psp_reference)
);
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS source_account_id TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS order_id TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS payee_account_id TEXT;
CREATE TABLE IF NOT EXISTS operations (
 client_id TEXT NOT NULL, operation_name TEXT NOT NULL, idem_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
 status INTEGER NOT NULL, response_body TEXT NOT NULL, response_headers JSONB NOT NULL DEFAULT '{}',
 tx_id TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(client_id,operation_name,idem_key)
);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, client_id TEXT NOT NULL, tx_kind TEXT NOT NULL, tx_id TEXT NOT NULL,
 job_type TEXT NOT NULL, callback_name TEXT NOT NULL DEFAULT '', payload TEXT NOT NULL DEFAULT '{}',
 status TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(), lease_until TIMESTAMPTZ, last_error TEXT,
 UNIQUE(client_id,tx_kind,tx_id,job_type,callback_name)
);
CREATE INDEX IF NOT EXISTS jobs_due ON jobs(status,next_attempt_at);
CREATE TABLE IF NOT EXISTS tokens (
 token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, client_id TEXT NOT NULL, account_id TEXT NOT NULL,
 scope TEXT NOT NULL, redirect_uri TEXT, state TEXT, expires_at TIMESTAMPTZ NOT NULL,
 consumed BOOLEAN NOT NULL DEFAULT false, family_id TEXT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
