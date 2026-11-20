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

-- Versioned events and explicit debt make a late refund rejection auditable and
-- idempotent without stealing funds reserved by a different authorized charge.
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS event_sequence BIGINT NOT NULL DEFAULT 0;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS reference_profile TEXT NOT NULL DEFAULT '{}';
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS event_sequence BIGINT NOT NULL DEFAULT 0;
ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_client_id_tx_kind_tx_id_job_type_callback_name_key;
CREATE UNIQUE INDEX IF NOT EXISTS jobs_event_identity
 ON jobs(client_id,tx_kind,tx_id,job_type,callback_name,event_sequence);
CREATE TABLE IF NOT EXISTS account_debts (
 client_id TEXT NOT NULL, account_id TEXT NOT NULL, currency TEXT NOT NULL,
 debt_e5 NUMERIC(40,0) NOT NULL CHECK(debt_e5>=0),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(client_id,account_id,currency)
);
CREATE TABLE IF NOT EXISTS refund_reversals (
 client_id TEXT NOT NULL, refund_id TEXT NOT NULL, amount_e5 BIGINT NOT NULL CHECK(amount_e5>0),
 recovered_e5 BIGINT NOT NULL CHECK(recovered_e5>=0), debt_e5 BIGINT NOT NULL CHECK(debt_e5>=0),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(client_id,refund_id)
);
CREATE OR REPLACE FUNCTION reference_adjust(p_client TEXT,p_account TEXT,p_currency TEXT,p_available BIGINT,p_balance BIGINT)
RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE a accounts%ROWTYPE; debt NUMERIC; av NUMERIC; bal NUMERIC; repay NUMERIC; reserved NUMERIC; credits NUMERIC;
BEGIN
 SELECT * INTO STRICT a FROM accounts WHERE client_id=p_client AND account_id=p_account AND currency=p_currency FOR UPDATE;
 SELECT debt_e5 INTO debt FROM account_debts WHERE client_id=p_client AND account_id=p_account AND currency=p_currency FOR UPDATE;
 debt:=COALESCE(debt,0); av:=a.available_e5::NUMERIC+p_available; bal:=a.balance_e5::NUMERIC+p_balance;
 SELECT COALESCE(sum(debit_e5),0),COALESCE(sum(credit_e5),0) INTO reserved,credits FROM intent_reservations WHERE client_id=p_client AND account_id=p_account AND currency=p_currency AND intent_id<>COALESCE(current_setting('reference.intent_id',true),'');
 IF p_available>0 OR p_balance>0 THEN
   repay:=LEAST(GREATEST(0,av-reserved),debt);av:=av-repay;bal:=bal-repay;debt:=debt-repay;
 END IF;
 IF av<reserved OR bal<av OR bal+credits>9223372036854775807 THEN RAISE EXCEPTION 'Invalid reference ledger adjustment' USING ERRCODE='40001'; END IF;
 UPDATE accounts SET available_e5=av::BIGINT,balance_e5=bal::BIGINT WHERE client_id=p_client AND account_id=p_account AND currency=p_currency;
 IF debt>0 OR EXISTS(SELECT 1 FROM account_debts WHERE client_id=p_client AND account_id=p_account AND currency=p_currency) THEN
  INSERT INTO account_debts(client_id,account_id,currency,debt_e5) VALUES(p_client,p_account,p_currency,debt)
  ON CONFLICT(client_id,account_id,currency) DO UPDATE SET debt_e5=EXCLUDED.debt_e5,updated_at=now();
 END IF;
END $$;
CREATE OR REPLACE FUNCTION reference_next_event(p_client TEXT,p_kind TEXT,p_id TEXT)
RETURNS BIGINT LANGUAGE sql AS $$
 UPDATE transactions SET event_sequence=event_sequence+1 WHERE client_id=p_client AND kind=p_kind AND id=p_id RETURNING event_sequence;
$$;
CREATE OR REPLACE FUNCTION reference_reverse_refund(p_client TEXT,p_id TEXT)
RETURNS BOOLEAN LANGUAGE plpgsql AS $$
DECLARE t transactions%ROWTYPE; a accounts%ROWTYPE; recovered BIGINT;
BEGIN
 SELECT * INTO t FROM transactions WHERE client_id=p_client AND kind='refund' AND id=p_id FOR UPDATE;
 IF NOT FOUND OR t.state<>'CONFIRMED' THEN RETURN FALSE; END IF;
 SELECT * INTO STRICT a FROM accounts WHERE client_id=p_client AND account_id=t.account_id AND currency=t.currency FOR UPDATE;
 recovered:=LEAST(GREATEST(0,a.available_e5::NUMERIC-COALESCE((SELECT sum(debit_e5) FROM intent_reservations WHERE client_id=p_client AND account_id=t.account_id AND currency=t.currency),0)),t.amount_e5)::BIGINT;
 INSERT INTO refund_reversals(client_id,refund_id,amount_e5,recovered_e5,debt_e5)
 VALUES(p_client,p_id,t.amount_e5,recovered,t.amount_e5-recovered) ON CONFLICT DO NOTHING;
 IF NOT FOUND THEN RETURN FALSE; END IF;
 UPDATE accounts SET available_e5=available_e5-recovered,balance_e5=balance_e5-recovered
 WHERE client_id=p_client AND account_id=t.account_id AND currency=t.currency;
 INSERT INTO account_debts(client_id,account_id,currency,debt_e5) VALUES(p_client,t.account_id,t.currency,t.amount_e5-recovered)
 ON CONFLICT(client_id,account_id,currency) DO UPDATE SET debt_e5=account_debts.debt_e5+EXCLUDED.debt_e5,updated_at=now();
 UPDATE transactions SET state='REJECTED',detail='PAYMENT_NETWORK_ERROR',updated_at=now()
 WHERE client_id=p_client AND kind='refund' AND id=p_id;
 RETURN TRUE;
END $$;

-- Durable external-operation executor. Commands, including merchant/KYC input,
-- are authenticated-encrypted; only routing, hashes and results remain in clear.
CREATE TABLE IF NOT EXISTS provider_intents (
 id TEXT PRIMARY KEY, client_id TEXT NOT NULL, operation_name TEXT NOT NULL,
 provider_key TEXT NOT NULL, resource_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
 request_encrypted TEXT NOT NULL, status TEXT NOT NULL,
 generation BIGINT NOT NULL DEFAULT 1, attempts INTEGER NOT NULL DEFAULT 0,
 lease_until TIMESTAMPTZ, next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 response_status INTEGER, response_body TEXT, response_headers JSONB NOT NULL DEFAULT '{}',
 last_error TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(client_id,provider_key)
);
CREATE INDEX IF NOT EXISTS provider_intents_due ON provider_intents(client_id,status,next_attempt_at);
CREATE TABLE IF NOT EXISTS provider_keys (
 client_id TEXT NOT NULL, operation_name TEXT NOT NULL, idem_key TEXT NOT NULL,
 fingerprint TEXT NOT NULL, intent_id TEXT NOT NULL REFERENCES provider_intents(id),
 PRIMARY KEY(client_id,operation_name,idem_key)
);
CREATE TABLE IF NOT EXISTS resource_claims (
 client_id TEXT NOT NULL, resource_key TEXT NOT NULL, intent_id TEXT NOT NULL REFERENCES provider_intents(id),
 PRIMARY KEY(client_id,resource_key)
);
CREATE TABLE IF NOT EXISTS intent_reservations (
 intent_id TEXT NOT NULL REFERENCES provider_intents(id), client_id TEXT NOT NULL,
 account_id TEXT NOT NULL, currency TEXT NOT NULL, debit_e5 BIGINT NOT NULL DEFAULT 0 CHECK(debit_e5>=0),
 credit_e5 BIGINT NOT NULL DEFAULT 0 CHECK(credit_e5>=0), PRIMARY KEY(intent_id,account_id,currency)
);

CREATE OR REPLACE FUNCTION reference_assert_unclaimed(p_client TEXT,p_resource TEXT)
RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE owner TEXT;
BEGIN
 SELECT intent_id INTO owner FROM resource_claims WHERE client_id=p_client AND resource_key=p_resource;
 IF owner IS NOT NULL AND owner<>COALESCE(current_setting('reference.intent_id',true),'') THEN
  RAISE EXCEPTION 'Operation requires reconciliation' USING ERRCODE='55P03';
 END IF;
END $$;

CREATE OR REPLACE FUNCTION reference_reserve_intent(p_intent TEXT,p_client TEXT,p_resource TEXT,p_plan JSONB)
RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE owner TEXT; a accounts%ROWTYPE; debt NUMERIC; reserved NUMERIC; credits NUMERIC;
        n BIGINT; cur TEXT; source TEXT; payee TEXT; op TEXT; t transactions%ROWTYPE;
BEGIN
 INSERT INTO resource_claims(client_id,resource_key,intent_id) VALUES(p_client,p_resource,p_intent) ON CONFLICT DO NOTHING;
 SELECT intent_id INTO owner FROM resource_claims WHERE client_id=p_client AND resource_key=p_resource FOR UPDATE;
 IF owner<>p_intent THEN RAISE EXCEPTION 'Operation in flight' USING ERRCODE='55P03';END IF;
 op:=p_plan->>'operation'; n:=COALESCE((p_plan->>'amount')::BIGINT,0);cur:=COALESCE(p_plan->>'currency','BRL');
 source:=p_plan->>'account';payee:=p_plan->>'payee';
 IF op IN ('capture','cancel','initRefund') THEN
  SELECT * INTO STRICT t FROM transactions WHERE client_id=p_client AND kind='charge' AND id=p_plan->>'original_id' FOR UPDATE;
  IF op IN ('capture','cancel') AND t.state<>'AUTHORIZED' OR op='initRefund' AND t.state<>'CONFIRMED' THEN
   RAISE EXCEPTION 'Resource changed during preparation' USING ERRCODE='40001';
  END IF;
 END IF;
 -- Lock only the participating accounts and only during this short reservation transaction.
 PERFORM 1 FROM accounts WHERE client_id=p_client AND currency=cur AND account_id IN (source,payee) ORDER BY account_id FOR UPDATE;
 IF op IN ('charge','payout') THEN
  SELECT * INTO STRICT a FROM accounts WHERE client_id=p_client AND account_id=source AND currency=cur;
  SELECT COALESCE(sum(debit_e5),0) INTO reserved FROM intent_reservations WHERE client_id=p_client AND account_id=source AND currency=cur;
  SELECT debt_e5 INTO debt FROM account_debts WHERE client_id=p_client AND account_id=source AND currency=cur;
  IF a.available_e5::NUMERIC-COALESCE(debt,0)-reserved<n THEN RAISE EXCEPTION 'Insufficient unreserved funds' USING ERRCODE='40001';END IF;
  INSERT INTO intent_reservations(intent_id,client_id,account_id,currency,debit_e5) VALUES(p_intent,p_client,source,cur,n);
 END IF;
 IF op IN ('payout','initRefund') THEN
  IF op='initRefund' THEN payee:=source;END IF;
  SELECT * INTO STRICT a FROM accounts WHERE client_id=p_client AND account_id=payee AND currency=cur;
  SELECT COALESCE(sum(credit_e5),0) INTO credits FROM intent_reservations WHERE client_id=p_client AND account_id=payee AND currency=cur;
  IF a.balance_e5::NUMERIC+credits+n>9223372036854775807 THEN RAISE EXCEPTION 'Credit headroom exhausted' USING ERRCODE='40001';END IF;
  INSERT INTO intent_reservations(intent_id,client_id,account_id,currency,credit_e5) VALUES(p_intent,p_client,payee,cur,n)
  ON CONFLICT(intent_id,account_id,currency) DO UPDATE SET credit_e5=EXCLUDED.credit_e5;
 END IF;
END $$;

CREATE OR REPLACE FUNCTION reference_spendable(p_client TEXT,p_account TEXT,p_currency TEXT)
RETURNS BIGINT LANGUAGE sql STABLE AS $$
 SELECT GREATEST(0,a.available_e5::NUMERIC
  -COALESCE((SELECT sum(r.debit_e5) FROM intent_reservations r WHERE r.client_id=p_client AND r.account_id=p_account AND r.currency=p_currency AND r.intent_id<>COALESCE(current_setting('reference.intent_id',true),'')),0)
  -GREATEST(0,COALESCE(d.debt_e5,0)-COALESCE((SELECT sum(r.debit_e5) FROM intent_reservations r WHERE r.client_id=p_client AND r.account_id=p_account AND r.currency=p_currency AND r.intent_id=COALESCE(current_setting('reference.intent_id',true),'')),0)))::BIGINT
 FROM accounts a LEFT JOIN account_debts d USING(client_id,account_id,currency)
 WHERE a.client_id=p_client AND a.account_id=p_account AND a.currency=p_currency;
$$;
ALTER TABLE provider_intents ADD COLUMN IF NOT EXISTS last_provider_version BIGINT NOT NULL DEFAULT 0;
ALTER TABLE provider_intents ADD COLUMN IF NOT EXISTS failures INTEGER NOT NULL DEFAULT 0;

ALTER TABLE provider_intents ADD COLUMN IF NOT EXISTS provider_reference TEXT;

-- Reference decision U5: one full/partial capture; preserve original authorization.
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS authorized_e5 BIGINT CHECK(authorized_e5>0);
CREATE OR REPLACE FUNCTION reference_authorized_amount() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.kind='charge' AND NEW.capture_method='MANUAL' THEN NEW.authorized_e5:=NEW.amount_e5;END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS transaction_authorized_amount ON transactions;
CREATE TRIGGER transaction_authorized_amount BEFORE INSERT ON transactions FOR EACH ROW EXECUTE FUNCTION reference_authorized_amount();

-- U15 reference settlement journal. Events are immutable and are not bank settlement.
CREATE TABLE IF NOT EXISTS settlement_entries (
 client_id TEXT NOT NULL, tx_kind TEXT NOT NULL, tx_id TEXT NOT NULL,
 event_type TEXT NOT NULL CHECK(event_type IN ('CONFIRMED','REFUND_REVERSED')),
 source_account_id TEXT NOT NULL, psp_reference TEXT NOT NULL, currency TEXT NOT NULL,
 amount_e5 BIGINT NOT NULL CHECK(amount_e5<>0), occurred_at TIMESTAMPTZ NOT NULL,
 PRIMARY KEY(client_id,tx_kind,tx_id,event_type)
);
CREATE INDEX IF NOT EXISTS settlement_entries_window ON settlement_entries(client_id,occurred_at);
CREATE TABLE IF NOT EXISTS settlement_batches (
 id TEXT PRIMARY KEY, client_id TEXT NOT NULL, period_start TIMESTAMPTZ NOT NULL, period_end TIMESTAMPTZ NOT NULL,
 format_version TEXT NOT NULL, manifest TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(client_id,period_start,period_end,format_version), CHECK(period_end>period_start)
);
CREATE TABLE IF NOT EXISTS settlement_batch_files (
 batch_id TEXT NOT NULL REFERENCES settlement_batches(id), source_account_id TEXT NOT NULL,
 filename TEXT NOT NULL, content TEXT NOT NULL, sha256 TEXT NOT NULL, entry_count BIGINT NOT NULL,
 PRIMARY KEY(batch_id,source_account_id),UNIQUE(batch_id,filename)
);
CREATE TABLE IF NOT EXISTS settlement_deliveries (
 batch_id TEXT NOT NULL REFERENCES settlement_batches(id), destination_hash TEXT NOT NULL,
 manifest_sha256 TEXT NOT NULL, receipt JSONB NOT NULL, delivered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(batch_id,destination_hash)
);
CREATE OR REPLACE FUNCTION reference_settlement_entry() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE t transactions%ROWTYPE; event TEXT; signed_amount BIGINT;
BEGIN
 IF NEW.state='CONFIRMED' AND (TG_OP='INSERT' OR OLD.state<>'CONFIRMED') THEN event:='CONFIRMED';
 ELSIF TG_OP='UPDATE' AND NEW.kind='refund' AND OLD.state='CONFIRMED' AND NEW.state='REJECTED' THEN event:='REFUND_REVERSED';
 ELSE RETURN NULL;END IF;
 -- Shared until commit: a closing export's exclusive lock waits for all earlier
 -- producers. New producers timestamp AFTER this lock, so no late old-period row
 -- can slip behind a sealed batch. No HTTP runs while holding this lock.
 PERFORM pg_advisory_xact_lock_shared(hashtextextended('settlement|'||NEW.client_id,0));
 SELECT * INTO STRICT t FROM transactions WHERE client_id=NEW.client_id AND kind=NEW.kind AND id=NEW.id;
 IF t.source_account_id IS NULL OR t.source_account_id='' THEN
  RAISE EXCEPTION 'Settlement source metadata required' USING ERRCODE='23514';
 END IF;
 signed_amount:=CASE WHEN t.kind='refund' AND event='CONFIRMED' THEN t.amount_e5 ELSE -t.amount_e5 END;
 INSERT INTO settlement_entries(client_id,tx_kind,tx_id,event_type,source_account_id,psp_reference,currency,amount_e5,occurred_at)
 VALUES(t.client_id,t.kind,t.id,event,t.source_account_id,t.psp_reference,t.currency,signed_amount,clock_timestamp())
 ON CONFLICT DO NOTHING;
 RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS transaction_settlement_entry ON transactions;
CREATE CONSTRAINT TRIGGER transaction_settlement_entry AFTER INSERT OR UPDATE ON transactions
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION reference_settlement_entry();
CREATE OR REPLACE FUNCTION reference_immutable_settlement() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Sealed financial evidence is immutable' USING ERRCODE='23514';END $$;
DROP TRIGGER IF EXISTS immutable_settlement_entries ON settlement_entries;
CREATE TRIGGER immutable_settlement_entries BEFORE UPDATE OR DELETE ON settlement_entries FOR EACH ROW EXECUTE FUNCTION reference_immutable_settlement();
DROP TRIGGER IF EXISTS immutable_settlement_batches ON settlement_batches;
CREATE TRIGGER immutable_settlement_batches BEFORE UPDATE OR DELETE ON settlement_batches FOR EACH ROW EXECUTE FUNCTION reference_immutable_settlement();
DROP TRIGGER IF EXISTS immutable_settlement_files ON settlement_batch_files;
CREATE TRIGGER immutable_settlement_files BEFORE UPDATE OR DELETE ON settlement_batch_files FOR EACH ROW EXECUTE FUNCTION reference_immutable_settlement();
