import { createCipheriv, createDecipheriv, randomBytes } from 'node:crypto';
import { Service, type Result, Failure } from './service.js';
import { DemoPspAdapter, type ProviderDecision, type CaptureDecision } from './adapter.js';
import { canonical, decode, encode, hash, type Json } from './codec.js';

type Context = {
  operation: string;
  target: string;
  body: Json;
  language: string;
  idem: string;
  configHash: string;
};
class ValidationAdapter extends DemoPspAdapter {
  override charge(c: Json, k: string): ProviderDecision {
    super.charge(c, k);
    return { state: 'CONFIRMED' };
  }
  override payout(c: Json, k: string): ProviderDecision {
    super.payout(c, k);
    return { state: 'CONFIRMED' };
  }
  override capture(c: Json, k: string): CaptureDecision {
    super.capture(c, k);
    return { state: 'CONFIRMED' };
  }
  override refund(c: Json, k: string): ProviderDecision {
    super.refund(c, k);
    return { state: 'CONFIRMED' };
  }
}
class ResultAdapter extends DemoPspAdapter {
  constructor(readonly result: Json) {
    super();
  }
  override charge(): ProviderDecision {
    return this.result;
  }
  override payout(): ProviderDecision {
    return this.result;
  }
  override capture(): CaptureDecision {
    return this.result;
  }
  override refund(): ProviderDecision {
    return this.result;
  }
}

/** All remote calls are outside transaction callbacks. Durable state contains ciphertext, never raw commands. */
export class ProcessorExecutor {
  private timer?: NodeJS.Timeout;
  private running = false;
  private stopping = false;
  constructor(readonly service: Service) {}
  private configHash(): string {
    return hash(canonical({ data: this.service.features.data, aliases: this.service.features.aliases }));
  }
  private encrypt(id: string, ctx: Context): string {
    const iv = randomBytes(12),
      c = createCipheriv('aes-256-gcm', Buffer.from(this.service.config.processorKey, 'base64'), iv);
    c.setAAD(Buffer.from(this.service.config.clientId + '|' + id));
    return Buffer.concat([iv, c.update(encode(ctx)), c.final(), c.getAuthTag()]).toString('base64');
  }
  private decrypt(row: Json): Context {
    const raw = Buffer.from(row.request_encrypted, 'base64');
    const c = createDecipheriv(
      'aes-256-gcm',
      Buffer.from(this.service.config.processorKey, 'base64'),
      raw.subarray(0, 12),
    );
    c.setAAD(Buffer.from(this.service.config.clientId + '|' + row.id));
    c.setAuthTag(raw.subarray(-16));
    const ctx = decode(Buffer.concat([c.update(raw.subarray(12, -16)), c.final()]));
    if (ctx.configHash !== this.configHash())
      throw new Error('Restore the pinned funding configuration before reconciliation');
    return ctx;
  }
  private async remote(path: string, body?: Json): Promise<Json | undefined> {
    const c = this.service.config;
    const r = await fetch(c.processorUrl + path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: {
        'content-type': 'application/json',
        authorization: 'Bearer ' + c.processorToken,
        'x-reference-client': c.clientId,
      },
      body: body === undefined ? undefined : encode(body),
      redirect: 'error',
      signal: AbortSignal.timeout(c.processorTimeoutMs),
    });
    if (body === undefined && r.status === 404) {
      await r.body?.cancel();
      return undefined;
    }
    if (r.status === 202) {
      await r.body?.cancel();
      throw new Error('Unresolved provider result');
    }
    if (!r.ok) {
      await r.body?.cancel();
      throw new Error('Processor transport rejected');
    }
    const chunks: Uint8Array[] = [];
    let size = 0;
    const reader = r.body?.getReader();
    if (!reader) throw new Error('Missing provider response');
    while (true) {
      const x = await reader.read();
      if (x.done) break;
      size += x.value.length;
      if (size > 1048576) {
        await reader.cancel();
        throw new Error('Provider body exceeds limit');
      }
      chunks.push(x.value);
    }
    return decode(Buffer.concat(chunks));
  }
  private decision(op: string, value: Json): Json {
    const allowed =
      op === 'cancel'
        ? ['CANCELLED']
        : ['capture'].includes(op)
          ? ['CONFIRMED', 'REJECTED']
          : ['CONFIRMED', 'PENDING', 'REJECTED'];
    if (
      !value ||
      !allowed.includes(value.state) ||
      typeof value.psp_reference !== 'string' ||
      !value.psp_reference ||
      value.psp_reference.length > 256
    )
      throw new Error('Invalid processor decision');
    if (value.version !== undefined && typeof value.version !== 'number' && typeof value.version !== 'bigint')
      throw new Error('Provider version must be an integer');
    if (value.terminal !== undefined && typeof value.terminal !== 'boolean')
      throw new Error('Provider terminal must be boolean');
    const version = Number(value.version ?? 1);
    if (!Number.isSafeInteger(version) || version < 1 || version > 2147483647)
      throw new Error('Invalid provider version');
    if (value.state === 'REJECTED' && op !== 'initRefund') {
      const name =
        op === 'payout' ? 'PayoutCallbackStateDetail' : op === 'capture' ? undefined : 'StateReasonCode';
      if (name)
        this.service.contract.validate(value.detail, this.service.contract.spec.components.schemas[name]);
      else if (!['BANK_SOFT_ERROR', 'BANK_HARD_ERROR'].includes(value.detail))
        throw new Error('Invalid capture decline');
    }
    if (value.actions !== undefined) {
      if (op !== 'charge' || value.state !== 'PENDING') throw new Error('Actions require a pending charge');
      this.service.contract.validate(value.actions, this.service.contract.spec.components.schemas.Actions);
      for (const action of value.actions)
        if (action.url) {
          const u = new URL(action.url);
          if (!['http:', 'https:'].includes(u.protocol) || u.username || u.password)
            throw new Error('Invalid action URL');
        }
    }
    if (value.amount !== undefined)
      this.service.contract.validate(
        value.amount,
        this.service.contract.spec.components.schemas.CurrencyAmount,
      );
    return {
      amount: value.amount,
      state: value.state,
      psp_reference: value.psp_reference,
      detail: value.detail,
      actions: value.actions,
      version,
      terminal: value.terminal !== false,
    };
  }
  private replay(row: Json): Result {
    return { status: row.response_status, text: row.response_body, headers: row.response_headers };
  }
  private async claim(id: string, explicit = false): Promise<Json | undefined> {
    return this.service.store.transaction(async (db) => {
      const row = (
        await db.query(`SELECT * FROM provider_intents WHERE id=$1 AND client_id=$2 FOR UPDATE`, [
          id,
          this.service.config.clientId,
        ])
      ).rows[0];
      if (
        !row ||
        row.status === 'DONE' ||
        (!explicit && row.status === 'RECONCILE') ||
        (row.lease_until && new Date(row.lease_until).getTime() > Date.now())
      )
        return;
      return (
        await db.query(
          `UPDATE provider_intents SET generation=generation+1,attempts=attempts+1,lease_until=now()+($2*interval '1 millisecond') WHERE id=$1 RETURNING *`,
          [id, this.service.config.processorLeaseMs],
        )
      ).rows[0];
    }, false);
  }
  async execute(op: Json, target: string, body: Json, idem?: string, language = 'en-US'): Promise<Result> {
    if (!idem || idem.length > 256) return this.service.error(op, 400);
    const s = this.service,
      c = s.config,
      name = op.operationId,
      fp = hash(target + '\n' + canonical(body));
    const key = hash(
      c.clientId + '|' + name + '|' + (['charge', 'payout', 'initRefund'].includes(name) ? body.id : idem),
    );
    let fresh = false;
    const row = await s.store.transaction(async (db) => {
      await db.query('SELECT pg_advisory_xact_lock(hashtextextended($1,0))', [key]);
      const mapping = (
        await db.query(
          'SELECT * FROM provider_keys WHERE client_id=$1 AND operation_name=$2 AND idem_key=$3',
          [c.clientId, name, idem],
        )
      ).rows[0];
      if (mapping && mapping.fingerprint !== fp)
        throw new Failure(
          name === 'payout' ? 409 : 400,
          name === 'payout' ? 'DUPLICATE_REQUEST_ERROR' : 'VALIDATION_ERROR',
        );
      let existing = (
        await db.query('SELECT * FROM provider_intents WHERE id=$1', [mapping?.intent_id ?? key])
      ).rows[0];
      if (existing && existing.fingerprint !== fp)
        throw new Failure(
          name === 'payout' ? 409 : 400,
          name === 'payout' ? 'DUPLICATE_REQUEST_ERROR' : 'VALIDATION_ERROR',
        );
      if (!existing) {
        await db.query('SAVEPOINT validate_operation');
        const validation = new Service(c, s.store, s.contract, new ValidationAdapter(s.features));
        const result = await validation.executeTx(db, op, target, body, idem, language);
        await db.query('ROLLBACK TO SAVEPOINT validate_operation');
        const localFinal = result.status >= 400 || result.body?.state?.value === 'REJECTED';
        if (localFinal) await validation.executeTx(db, op, target, body, idem, language);
        const ctx: Context = { operation: name, target, body, idem, language, configHash: this.configHash() };
        const resource = s.resourceKey(name, target, body);
        existing = (
          await db.query(
            `INSERT INTO provider_intents(id,client_id,operation_name,provider_key,resource_key,fingerprint,request_encrypted,status,generation,attempts,lease_until,response_status,response_body,response_headers) VALUES($1,$2,$3,$1,$4,$5,$6,$7,1,1,now()+($8*interval '1 millisecond'),$9,$10,$11) RETURNING *`,
            [
              key,
              c.clientId,
              name,
              resource,
              fp,
              this.encrypt(key, ctx),
              localFinal ? 'DONE' : 'IN_FLIGHT',
              c.processorLeaseMs,
              localFinal ? result.status : null,
              localFinal ? (result.text ?? encode(result.body)) : null,
              JSON.stringify(result.headers ?? {}),
            ],
          )
        ).rows[0];
        if (!localFinal) {
          const original = ['capture', 'cancel', 'initRefund'].includes(name)
            ? (
                await db.query(`SELECT * FROM transactions WHERE client_id=$1 AND kind='charge' AND id=$2`, [
                  c.clientId,
                  name === 'initRefund' ? body.original_transaction_id : resource.slice(7),
                ])
              ).rows[0]
            : undefined;
          const plan = {
            operation: name,
            amount: body.amount?.value ?? body.amount_to_capture?.value ?? original?.amount_e5 ?? 0,
            currency: body.amount?.currency ?? original?.currency,
            account:
              name === 'payout'
                ? body.payer.id
                : (original?.account_id ?? body.authorization?.account_id ?? 'demo-account'),
            payee: body.payee?.account?.account_id,
            original_id: original?.id,
          };
          await db.query('SELECT reference_reserve_intent($1,$2,$3,$4::jsonb)', [
            key,
            c.clientId,
            resource,
            encode(plan),
          ]);
          fresh = true;
        }
      }
      await db.query(
        'INSERT INTO provider_keys(client_id,operation_name,idem_key,fingerprint,intent_id) VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING',
        [c.clientId, name, idem, fp, existing.id],
      );
      return existing;
    }, false);
    if (row.response_status !== null) {
      if (row.status === 'RECONCILE')
        await s.store.pool.query(
          "UPDATE provider_intents SET status='PENDING',failures=0,next_attempt_at=now() WHERE id=$1 AND status='RECONCILE'",
          [row.id],
        );
      return this.replay(row);
    }
    if (fresh) return c.processorPauseAfterPrepare ? { status: 500 } : this.dispatch(row, false);
    const end = Date.now() + c.processorWaitMs;
    do {
      const ready = (await s.store.pool.query('SELECT * FROM provider_intents WHERE id=$1', [row.id]))
        .rows[0];
      if (ready.response_status !== null) return this.replay(ready);
      const claimed = await this.claim(row.id, true);
      if (claimed) return this.dispatch(claimed, true);
      await new Promise((r) => setTimeout(r, 25));
    } while (Date.now() < end);
    return { status: 500 };
  }
  private async dispatch(row: Json, lookup: boolean): Promise<Result> {
    try {
      const ctx = this.decrypt(row);
      let result = lookup ? await this.remote('/operations/' + row.provider_key) : undefined;
      if (!result)
        result = await this.remote('/operations/' + row.provider_key, {
          operation: ctx.operation,
          request: ctx.body,
          resource: row.resource_key.slice(row.resource_key.indexOf(':') + 1),
        });
      const decision = this.decision(ctx.operation, result);
      if (ctx.body.capture_method === 'MANUAL' && decision.state === 'PENDING')
        throw new Error('Manual authorization cannot be pending');
      return await this.finalize(row, decision);
    } catch {
      await this.service.store.pool.query(
        `UPDATE provider_intents SET failures=failures+1,status=CASE WHEN failures+1>=$3 THEN 'RECONCILE' ELSE 'UNCERTAIN' END,lease_until=NULL,next_attempt_at=now()+($4*interval '1 millisecond'),last_error='processor_unresolved' WHERE id=$1 AND generation=$2`,
        [
          row.id,
          row.generation,
          this.service.config.processorMaxFailures,
          this.service.config.processorPollMs,
        ],
      );
      return { status: 500 };
    }
  }
  private async finalize(row: Json, result: Json): Promise<Result> {
    const s = this.service,
      c = s.config,
      ctx = this.decrypt(row),
      op = s.contract.operation(
        'post',
        ctx.operation === 'initRefund'
          ? '/refund'
          : ['capture', 'cancel'].includes(ctx.operation)
            ? '/charge/{id}/' + ctx.operation
            : '/' + ctx.operation,
      );
    return s.store.transaction(async (db) => {
      const current = (await db.query('SELECT * FROM provider_intents WHERE id=$1 FOR UPDATE', [row.id]))
        .rows[0];
      if (current.generation !== row.generation)
        return current.response_status !== null ? this.replay(current) : { status: 500 };
      await db.query("SELECT set_config('reference.intent_id',$1,true)", [row.id]);
      if (Number(current.last_provider_version) >= result.version && current.response_status !== null) {
        await db.query('UPDATE provider_intents SET lease_until=NULL WHERE id=$1 AND generation=$2', [
          row.id,
          row.generation,
        ]);
        return this.replay(current);
      }
      if (current.provider_reference && current.provider_reference !== result.psp_reference)
        throw new Error('Processor reference changed');
      let response: Result;
      const kind =
        ctx.operation === 'initRefund' ? 'refund' : ctx.operation === 'payout' ? 'payout' : 'charge';
      const id = ctx.body.id ?? row.resource_key.slice(7);
      const tx = (
        await db.query('SELECT * FROM transactions WHERE client_id=$1 AND kind=$2 AND id=$3', [
          c.clientId,
          kind,
          id,
        ])
      ).rows[0];
      let updatedAmount: Json = undefined;
      if (result.amount !== undefined) {
        const profile = tx && decode(tx.reference_profile);
        if (
          kind !== 'charge' ||
          !tx ||
          current.response_status === null ||
          tx.state !== 'PENDING' ||
          result.state !== 'CONFIRMED' ||
          profile.callback_mode !== 'deposit' ||
          result.amount.currency !== tx.currency
        )
          throw new Error('Amount adjustment requires pending deposit confirmation');
        updatedAmount = result.amount.value;
      }
      if (current.response_status !== null) {
        if (tx && result.version > Number(current.last_provider_version)) {
          if (tx.state === 'PENDING' && result.state !== 'PENDING')
            await s.complete(db, kind, id, result.state, result.detail, updatedAmount);
          else if (kind === 'refund' && tx.state === 'CONFIRMED' && result.state === 'REJECTED')
            await s.reverseRefund(db, id);
          else if (tx.state === 'REJECTED' && result.state !== 'REJECTED')
            throw new Error('Rejected is terminal');
        }
        response = this.replay(current);
      } else {
        const native = new Service(c, s.store, s.contract, new ResultAdapter(result));
        response = await native.executeTx(db, op, ctx.target, ctx.body, ctx.idem, ctx.language);
        const expected =
          ctx.operation === 'initRefund'
            ? 201
            : result.state === 'REJECTED' && ctx.operation === 'capture'
              ? 422
              : result.state === 'REJECTED' && ctx.operation === 'payout'
                ? ['VALIDATION_ERROR', 'INVALID_PAYMENT_DETAILS_ERROR', 'INSUFFICIENT_BALANCE'].includes(
                    result.detail,
                  )
                  ? 400
                  : 422
                : 200;
        if (response.status !== expected)
          throw new Error('Provider outcome must reconcile; native preconditions changed');
        if (
          response.status < 300 &&
          response.body &&
          ['charge', 'payout', 'initRefund'].includes(ctx.operation)
        ) {
          const field = kind === 'refund' ? 'merchant_reference' : 'psp_reference';
          response.body[field] = result.psp_reference;
          if (result.actions && kind === 'charge') response.body.actions = result.actions;
          s.contract.response(op, response.status, response.body);
          response.text = encode(response.body);
          await db.query(
            'UPDATE transactions SET psp_reference=$4,response_body=$5 WHERE client_id=$1 AND kind=$2 AND id=$3',
            [c.clientId, kind, id, result.psp_reference, response.text],
          );
          await db.query(
            'UPDATE operations SET response_body=$4 WHERE client_id=$1 AND operation_name=$2 AND idem_key=$3',
            [c.clientId, ctx.operation, ctx.idem, response.text],
          );
        }
      }
      await db.query(
        `UPDATE jobs SET status=$4 WHERE client_id=$1 AND tx_kind=$2 AND tx_id=$3 AND job_type='COMPLETION' AND status IN ('PENDING','WAITING','PROVIDER_WAIT')`,
        [c.clientId, kind, id, result.state === 'PENDING' ? 'PROVIDER_WAIT' : 'DONE'],
      );
      const pending =
        result.state === 'PENDING' || (kind === 'refund' && result.state === 'CONFIRMED' && !result.terminal);
      await db.query('UPDATE provider_intents SET provider_reference=$2 WHERE id=$1', [
        row.id,
        result.psp_reference,
      ]);
      await db.query(
        `UPDATE provider_intents SET status=$3,last_provider_version=$4,lease_until=NULL,next_attempt_at=now()+($5*interval '1 millisecond'),response_status=COALESCE(response_status,$6),response_body=COALESCE(response_body,$7),response_headers=CASE WHEN response_status IS NULL THEN $8::jsonb ELSE response_headers END,last_error=NULL,failures=0,updated_at=now() WHERE id=$1 AND generation=$2`,
        [
          row.id,
          row.generation,
          pending ? 'PENDING' : 'DONE',
          result.version,
          c.processorPollMs,
          response.status,
          response.text ?? (response.body === undefined ? '' : encode(response.body)),
          JSON.stringify(response.headers ?? {}),
        ],
      );
      if (result.state === 'PENDING')
        await db.query('UPDATE intent_reservations SET debit_e5=0 WHERE intent_id=$1', [row.id]);
      else {
        await db.query('DELETE FROM intent_reservations WHERE intent_id=$1', [row.id]);
        await db.query('DELETE FROM resource_claims WHERE intent_id=$1', [row.id]);
      }
      return response;
    }, false);
  }
  async tick(): Promise<void> {
    if (this.running || this.stopping) return;
    this.running = true;
    try {
      const c = this.service.config;
      const rows = (
        await this.service.store.pool.query(
          `SELECT id FROM provider_intents WHERE client_id=$1 AND status IN ('IN_FLIGHT','UNCERTAIN','PENDING') AND next_attempt_at<=now() AND (lease_until IS NULL OR lease_until<now()) ORDER BY next_attempt_at LIMIT 4`,
          [c.clientId],
        )
      ).rows;
      for (const x of rows) {
        const row = await this.claim(x.id);
        if (row) await this.dispatch(row, true);
      }
    } catch {
      console.error('processor reconciliation retained for retry');
    } finally {
      this.running = false;
    }
  }
  async event(value: Json): Promise<void> {
    const c = this.service.config;
    const row = (
      await this.service.store.pool.query(
        'SELECT * FROM provider_intents WHERE client_id=$1 AND provider_key=$2',
        [c.clientId, value.provider_key],
      )
    ).rows[0];
    if (!row) throw new Failure(404);
    const result = this.decision(row.operation_name, value);
    const claimed = await this.service.store.transaction(async (db) => {
      const current = (await db.query('SELECT * FROM provider_intents WHERE id=$1 FOR UPDATE', [row.id]))
        .rows[0];
      if (result.version <= Number(current.last_provider_version)) return;
      if (current.provider_reference && current.provider_reference !== result.psp_reference)
        throw new Failure(400);
      if (current.lease_until && new Date(current.lease_until).getTime() > Date.now()) throw new Failure(503);
      return (
        await db.query(
          "UPDATE provider_intents SET generation=generation+1,lease_until=now()+($2*interval '1 millisecond') WHERE id=$1 RETURNING *",
          [row.id, c.processorLeaseMs],
        )
      ).rows[0];
    }, false);
    if (claimed) {
      try {
        await this.finalize(claimed, result);
      } catch {
        await this.service.store.pool.query(
          "UPDATE provider_intents SET lease_until=NULL,status='RECONCILE',last_error='event_unresolved' WHERE id=$1 AND generation=$2",
          [claimed.id, claimed.generation],
        );
        throw new Failure(500);
      }
    }
  }
  start(): void {
    this.timer = setInterval(() => void this.tick(), this.service.config.processorPollMs);
    this.timer.unref();
  }
  async stop(): Promise<void> {
    this.stopping = true;
    if (this.timer) clearInterval(this.timer);
    while (this.running) await new Promise((r) => setTimeout(r, 20));
  }
}
