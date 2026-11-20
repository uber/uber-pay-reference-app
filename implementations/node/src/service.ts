import { randomBytes, randomUUID } from 'node:crypto';
import { checkedAmount, decode, encode, fingerprint, hash, MAX_INT64, type Json } from './codec.js';
import { Contract, ContractError } from './contract.js';
import { DemoPspAdapter, type PspAdapter } from './adapter.js';
import { Store, type Connection } from './store.js';
import type { Config } from './config.js';
import { ReferenceFeatures } from './features.js';
import { safeEqual } from './security.js';

export interface Result {
  status: number;
  body?: Json;
  text?: string;
  headers?: Record<string, string>;
}
export class Failure extends Error {
  constructor(
    readonly status: number,
    readonly detail = 'VALIDATION_ERROR',
  ) {
    super(detail);
  }
}

export class Service {
  readonly features: ReferenceFeatures;
  executor?: import('./processor.js').ProcessorExecutor;
  constructor(
    readonly config: Config,
    readonly store: Store,
    readonly contract: Contract,
    readonly adapter: PspAdapter = new DemoPspAdapter(),
  ) {
    this.features = new ReferenceFeatures(config.root);
    if (adapter.mode !== 'local') throw new Error('Only synthetic local adapters are allowed');
  }

  error(operation: Json, status: number, detail = 'VALIDATION_ERROR'): Result {
    if (status >= 500 && !operation?.responses?.[String(status)]) return { status: 500 };
    if (!operation?.responses?.[String(status)]) status = 400;
    const response = operation?.responses?.[String(status)];
    const schema = response && this.contract.dereference(response).content?.['application/json']?.schema;
    const body = schema
      ? operation.operationId === 'createToken'
        ? {
            error:
              detail.startsWith('invalid_') || detail.startsWith('unsupported_') ? detail : 'invalid_request',
          }
        : { detail }
      : undefined;
    return { status, body };
  }

  async bearer(authorization: string | undefined, scope: string, account?: string): Promise<Json> {
    if (!authorization || !/^Bearer /i.test(authorization) || authorization.length > 4096)
      throw new Failure(401);
    return this.store.transaction(async (db) => {
      const rows = await db.query(
        `SELECT * FROM tokens WHERE token_hash=$1 AND client_id=$2 AND kind='ACCESS'
         AND consumed=false AND expires_at>now()`,
        [hash(authorization.slice(7)), this.config.clientId],
      );
      const token = rows.rows[0];
      if (!token || !token.scope.split(' ').includes(scope) || (account && token.account_id !== account)) {
        throw new Failure(401);
      }
      return token;
    });
  }

  async execute(
    operation: Json,
    target: string,
    body: Json,
    idem: string | undefined,
    language = 'en-US',
  ): Promise<Result> {
    return this.store.transaction((db) => this.executeTx(db, operation, target, body, idem, language));
  }

  async executeTx(
    db: Connection,
    operation: Json,
    target: string,
    body: Json,
    idem: string | undefined,
    language = 'en-US',
  ): Promise<Result> {
    const name = operation.operationId;
    const needsKey = ['charge', 'capture', 'cancel', 'initRefund', 'payout'].includes(name);
    if (needsKey && (!idem || idem.length > 256)) return this.error(operation, 400);
    const fp = fingerprint(target, body);
    if (needsKey) {
      const found = (
        await db.query('SELECT * FROM operations WHERE client_id=$1 AND operation_name=$2 AND idem_key=$3', [
          this.config.clientId,
          name,
          idem,
        ])
      ).rows[0];
      if (found)
        return found.fingerprint === fp
          ? { status: found.status, text: found.response_body, headers: found.response_headers }
          : this.error(
              operation,
              name === 'payout' ? 409 : 400,
              name === 'payout' ? 'DUPLICATE_REQUEST_ERROR' : 'VALIDATION_ERROR',
            );
    }
    if (needsKey)
      await db.query('SELECT reference_assert_unclaimed($1,$2)', [
        this.config.clientId,
        this.resourceKey(name, target, body),
      ]);
    await db.query('SAVEPOINT operation_effect');
    let result: Result;
    try {
      if (
        name === 'createToken' &&
        !['authorization_code', 'refresh_token', 'urn:uberpay:params:oauth:grant-type:wallet-grant'].includes(
          body.grant_type,
        )
      ) {
        throw new Failure(400, 'unsupported_grant_type');
      }
      this.contract.request(operation, body);
      switch (name) {
        case 'charge':
          result = await this.create(db, 'charge', body, fp, language);
          break;
        case 'payout':
          result = await this.create(db, 'payout', body, fp, language);
          break;
        case 'capture':
          result = await this.capture(db, target, body);
          break;
        case 'cancel':
          result = await this.cancel(db, target, body);
          break;
        case 'initRefund':
          result = await this.refund(db, body, fp);
          break;
        case 'findTransaction':
          result = await this.find(db, body);
          break;
        case 'getBalance':
          result = await this.balance(db, body);
          break;
        case 'createToken':
          result = await this.token(db, body);
          break;
        default:
          throw new Failure(400);
      }
    } catch (error) {
      if (error instanceof Failure) {
        await db.query('ROLLBACK TO SAVEPOINT operation_effect');
        result = this.error(operation, error.status, error.detail);
      } else if (
        error instanceof ContractError ||
        error instanceof SyntaxError ||
        error instanceof RangeError
      ) {
        await db.query('ROLLBACK TO SAVEPOINT operation_effect');
        result = this.error(operation, 400);
      } else throw error;
    }
    if (result.status >= 400 && result.body)
      result.body.error_description = this.features.text(language, 'invalid_request');
    result.headers = { ...result.headers, 'Content-Language': this.features.locale(language) };
    try {
      this.contract.response(operation, result.status, result.body, body?.grant_type);
    } catch {
      throw new Error('Internal response contract failure');
    }
    const text = result.body === undefined ? '' : encode(result.body);
    if (needsKey && result.status < 500) {
      await db.query(
        `INSERT INTO operations(client_id,operation_name,idem_key,fingerprint,status,response_body,response_headers,tx_id)
           VALUES($1,$2,$3,$4,$5,$6,$7,$8)`,
        [
          this.config.clientId,
          name,
          idem,
          fp,
          result.status,
          text,
          JSON.stringify(result.headers ?? {}),
          body?.id ?? null,
        ],
      );
    }
    return { ...result, text };
  }

  resourceKey(name: string, target: string, body: Json): string {
    return name === 'payout'
      ? 'payout:' + body.id
      : 'charge:' +
          (name === 'initRefund'
            ? body.original_transaction_id
            : ['capture', 'cancel'].includes(name)
              ? this.idFromTarget(target)
              : body.id);
  }
  private amount(value: Json): bigint {
    try {
      return checkedAmount(value);
    } catch {
      throw new Failure(400);
    }
  }

  private async account(db: Connection, id: string, currency: string): Promise<Json> {
    const row = (
      await db.query(
        'SELECT a.client_id,a.account_id,a.currency,reference_spendable(a.client_id,a.account_id,a.currency) AS available_e5,GREATEST(0,a.balance_e5-COALESCE(d.debt_e5,0))::bigint AS balance_e5 FROM accounts a LEFT JOIN account_debts d USING(client_id,account_id,currency) WHERE a.client_id=$1 AND a.account_id=$2 AND a.currency=$3',
        [this.config.clientId, id, currency],
      )
    ).rows[0];
    if (!row) throw new Failure(404);
    return row;
  }

  private async ledger(
    db: Connection,
    accountId: string,
    currency: string,
    available: bigint,
    balance: bigint,
  ): Promise<void> {
    await db.query('SELECT reference_adjust($1,$2,$3,$4,$5)', [
      this.config.clientId,
      accountId,
      currency,
      available.toString(),
      balance.toString(),
    ]);
  }

  private async transaction(db: Connection, kind: string, id: string): Promise<Json> {
    const row = (
      await db.query('SELECT * FROM transactions WHERE client_id=$1 AND kind=$2 AND id=$3', [
        this.config.clientId,
        kind,
        id,
      ])
    ).rows[0];
    if (!row) throw new Failure(404, 'RESOURCE_NOT_FOUND');
    return row;
  }

  private async create(
    db: Connection,
    kind: 'charge' | 'payout',
    body: Json,
    fp: string,
    language: string,
  ): Promise<Result> {
    const source = kind === 'charge' ? body.source?.id : body.payer?.id;
    if (typeof source !== 'string' || !source) throw new Failure(400);
    const profile = this.features.method(body.funding_method, body.country_iso2, body.amount?.currency);
    if (typeof body.id !== 'string' || !body.id || body.id.length > 256) throw new Failure(400);
    const old = (
      await db.query('SELECT * FROM transactions WHERE client_id=$1 AND kind=$2 AND id=$3', [
        this.config.clientId,
        kind,
        body.id,
      ])
    ).rows[0];
    if (old)
      return old.request_hash === fp
        ? { status: old.response_status, body: decode(old.response_body) }
        : this.error(
            this.contract.operation('post', `/${kind}`),
            kind === 'payout' ? 409 : 400,
            kind === 'payout' ? 'DUPLICATE_REQUEST_ERROR' : 'VALIDATION_ERROR',
          );
    const amount = this.amount(body.amount.value);
    if (kind === 'payout') {
      const payee = body.payee?.account?.account_id;
      if (payee === 'invalid-account' || body.payee?.document?.id === 'invalid')
        throw new Failure(400, 'INVALID_PAYMENT_DETAILS_ERROR');
      const declines: Record<string, string> = {
        'inactive-account': 'INACTIVE_ACCOUNT_ERROR',
        'fraud-account': 'PAYMENT_FRAUD_ERROR',
        'hard-decline-account': 'PAYMENT_PSP_HARD_DECLINE_ERROR',
        'bank-hard-account': 'BANK_HARD_ERROR',
        'merchant-rejected-account': 'MERCHANT_REJECTED',
        'network-error-account': 'PAYMENT_NETWORK_ERROR',
        'psp-error-account': 'PAYMENT_PSP_ERROR',
        'payment-error-account': 'PAYMENT_ERROR',
      };
      if (Object.hasOwn(declines, payee)) throw new Failure(422, declines[payee]);
      await this.account(db, payee, body.amount.currency);
    }
    const accountId = kind === 'charge' ? (body.authorization?.account_id ?? 'demo-account') : body.payer.id;
    const account = await this.account(db, accountId, body.amount.currency);
    let decision;
    decision = await this.adapter[kind](body, hash(`${kind}:${body.id}`));
    const manual = kind === 'charge' && body.capture_method === 'MANUAL';
    if (manual && decision.state === 'PENDING') throw new Failure(400);
    if (account.available_e5 < amount)
      decision = { state: 'REJECTED' as const, detail: 'INSUFFICIENT_BALANCE' };
    if (kind === 'payout' && decision.state === 'REJECTED') {
      const detail = decision.detail ?? 'PAYMENT_ERROR';
      throw new Failure(
        ['VALIDATION_ERROR', 'INVALID_PAYMENT_DETAILS_ERROR', 'INSUFFICIENT_BALANCE'].includes(detail)
          ? 400
          : 422,
        detail,
      );
    }
    const reference = `node-${randomUUID()}`;
    const result: Json = { state: { value: decision.state }, psp_reference: reference };
    if (decision.detail) result.state.detail = decision.detail;
    const state = manual && decision.state === 'CONFIRMED' ? 'AUTHORIZED' : decision.state;
    if (state !== 'REJECTED')
      await this.ledger(db, accountId, body.amount.currency, -amount, state === 'CONFIRMED' ? -amount : 0n);
    const payload: Json = {
      state: decision.finalState ?? 'CONFIRMED',
      detail: decision.finalDetail,
      return_url: body.return_url,
      locale: this.features.locale(language),
      profile,
    };
    if (kind === 'payout') decision.userAction = undefined;
    if (decision.userAction) {
      payload.payment_token = randomBytes(32).toString('hex');
      payload.csrf = randomBytes(32).toString('hex');
      payload.expires_at = Date.now() + this.config.actionTtlMs;
      result.actions =
        decision.userAction === 'web_view_user_input_action'
          ? [
              {
                type: decision.userAction,
                url: `${new URL(this.config.publicBase).origin}/demo/pay?token=${payload.payment_token}`,
              },
            ]
          : [
              {
                type: decision.userAction,
                code: `local-qr:${payload.payment_token}`,
                expiry_time_millis: BigInt(payload.expires_at),
              },
            ];
    }
    await db.query(
      `INSERT INTO transactions(client_id,kind,id,psp_reference,account_id,amount_e5,currency,capture_method,
       funding_method,state,detail,request_hash,response_status,response_body,expires_at)
       VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,200,$13,$14)`,
      [
        this.config.clientId,
        kind,
        body.id,
        reference,
        accountId,
        amount.toString(),
        body.amount.currency,
        manual ? 'MANUAL' : 'AUTOMATIC',
        body.funding_method,
        state,
        decision.detail ?? null,
        fp,
        encode(result),
        state === 'AUTHORIZED' ? new Date(Date.now() + 72 * 3600000) : null,
      ],
    );
    await db.query(
      `UPDATE transactions SET source_account_id=$4,payee_account_id=$5,reference_profile=$6 WHERE client_id=$1 AND kind=$2 AND id=$3`,
      [
        this.config.clientId,
        kind,
        body.id,
        kind === 'charge' ? body.source.id : body.payer.id,
        kind === 'payout' ? body.payee.account.account_id : null,
        encode(profile),
      ],
    );
    const orderId = body.payment_data?.find((item: Json) => item.type === 'order_payment_data')?.order_id;
    if (orderId)
      await db.query('UPDATE transactions SET order_id=$4 WHERE client_id=$1 AND kind=$2 AND id=$3', [
        this.config.clientId,
        kind,
        body.id,
        orderId,
      ]);
    if (kind === 'payout' && state === 'CONFIRMED')
      await this.ledger(db, body.payee.account.account_id, body.amount.currency, amount, amount);
    if (state === 'PENDING') {
      await this.queue(
        db,
        kind,
        body.id,
        'COMPLETION',
        '',
        payload,
        decision.userAction ? 'WAITING' : 'PENDING',
      );
    }
    return { status: 200, body: result };
  }

  private idFromTarget(target: string): string {
    const match = /\/charge\/([^/?]+)\/(?:capture|cancel)$/.exec(target);
    if (!match) throw new Failure(400);
    try {
      return decodeURIComponent(match[1]!);
    } catch {
      throw new Failure(400);
    }
  }

  private async capture(db: Connection, target: string, body: Json): Promise<Result> {
    const tx = await this.transaction(db, 'charge', this.idFromTarget(target));
    await this.expire(db, tx, false);
    if (tx.state !== 'AUTHORIZED') throw new Failure(409, 'INVALID_CHARGE_STATE');
    if (!tx.source_account_id) throw new Failure(400);
    const amount = this.amount(body.amount_to_capture.value);
    if (body.amount_to_capture.currency !== tx.currency) throw new Failure(400);
    if (amount > tx.amount_e5) throw new Failure(400, 'EXCEEDS_CAPTURE_LIMIT');
    if (body.psp_reference && body.psp_reference !== tx.psp_reference) throw new Failure(400);
    const capture = await this.adapter.capture(
      { ...body, funding_method: tx.funding_method },
      hash(`capture:${tx.id}`),
    );
    if (capture.state === 'REJECTED') {
      await this.ledger(db, tx.account_id, tx.currency, tx.amount_e5, 0n);
      await this.setState(db, tx, 'REJECTED');
      return this.error(
        this.contract.operation('post', '/charge/{id}/capture'),
        422,
        capture.detail ?? 'BANK_HARD_ERROR',
      );
    }
    if (capture.state !== 'CONFIRMED') throw new Error('Unexpected capture decision');
    await this.ledger(db, tx.account_id, tx.currency, tx.amount_e5 - amount, -amount);
    await db.query('UPDATE transactions SET amount_e5=$4 WHERE client_id=$1 AND kind=$2 AND id=$3', [
      this.config.clientId,
      tx.kind,
      tx.id,
      amount.toString(),
    ]);
    await this.setState(db, tx, 'CONFIRMED');
    return { status: 200 };
  }

  private async cancel(db: Connection, target: string, body: Json): Promise<Result> {
    const tx = await this.transaction(db, 'charge', this.idFromTarget(target));
    await this.expire(db, tx, false);
    if (tx.state !== 'AUTHORIZED') throw new Failure(409, 'INVALID_CHARGE_STATE');
    if (body.psp_reference && body.psp_reference !== tx.psp_reference) throw new Failure(400);
    await this.adapter.cancel({ id: tx.id, psp_reference: tx.psp_reference }, hash(`cancel:${tx.id}`));
    await this.ledger(db, tx.account_id, tx.currency, tx.amount_e5, 0n);
    await this.setState(db, tx, 'CANCELLED');
    return { status: 200 };
  }

  private async refund(db: Connection, body: Json, fp: string): Promise<Result> {
    const old = (
      await db.query('SELECT * FROM transactions WHERE client_id=$1 AND kind=$2 AND id=$3', [
        this.config.clientId,
        'refund',
        body.id,
      ])
    ).rows[0];
    if (old) {
      if (old.request_hash !== fp) throw new Failure(400);
      return { status: old.response_status, body: decode(old.response_body) };
    }
    const original = await this.transaction(db, 'charge', body.original_transaction_id);
    if (!original.source_account_id) throw new Failure(400);
    if (original.psp_reference !== body.original_merchant_reference || original.state !== 'CONFIRMED')
      throw new Failure(400);
    const amount = this.amount(body.amount.value);
    if (body.amount.currency !== original.currency) throw new Failure(400);
    const refunds = (
      await db.query(
        `SELECT COALESCE(sum(amount_e5),0)::text AS used FROM transactions WHERE client_id=$1 AND kind='refund'
       AND original_id=$2 AND state IN ('PENDING','CONFIRMED')`,
        [this.config.clientId, original.id],
      )
    ).rows[0];
    if (BigInt(refunds.used) + amount > original.amount_e5) throw new Failure(400);
    const decision = await this.adapter.refund(
      { ...body, funding_method: original.funding_method },
      hash(`refund:${body.id}`),
    );
    const pending = decision.state === 'PENDING';
    const state = decision.state;
    const result = { status: state, merchant_reference: `node-refund-${randomUUID()}` };
    if (state === 'CONFIRMED') await this.ledger(db, original.account_id, original.currency, amount, amount);
    await db.query(
      `INSERT INTO transactions(client_id,kind,id,psp_reference,original_id,account_id,amount_e5,currency,
       funding_method,state,request_hash,response_status,response_body)
       VALUES($1,'refund',$2,$3,$4,$5,$6,$7,$8,$9,$10,201,$11)`,
      [
        this.config.clientId,
        body.id,
        result.merchant_reference,
        original.id,
        original.account_id,
        amount.toString(),
        original.currency,
        original.funding_method,
        state,
        fp,
        encode(result),
      ],
    );
    await db.query(
      `UPDATE transactions SET source_account_id=$3,reference_profile=$4 WHERE client_id=$1 AND kind='refund' AND id=$2`,
      [this.config.clientId, body.id, original.source_account_id, original.reference_profile],
    );
    if (pending)
      await this.queue(db, 'refund', body.id, 'COMPLETION', '', {
        state: decision.finalState ?? 'CONFIRMED',
        detail: decision.finalDetail,
      });
    return { status: 201, body: result };
  }

  private async find(db: Connection, query: Json): Promise<Result> {
    if (!query.id || typeof query.id !== 'string') throw new Failure(400);
    const rows = await db.query(
      `SELECT * FROM transactions WHERE client_id=$1 AND id=$2 AND kind IN ('charge','refund') ORDER BY kind`,
      [this.config.clientId, query.id],
    );
    const tx = rows.rows.find(
      (t) => !query.merchant_reference || t.psp_reference === query.merchant_reference,
    );
    if (!tx) throw new Failure(404);
    await this.expire(db, tx, false);
    const body: Json = {
      id: tx.id,
      merchant_reference: tx.psp_reference,
      status: tx.state,
      amount: { value: tx.amount_e5, currency: tx.currency },
    };
    if (tx.state === 'CONFIRMED') body.confirmed_at = tx.updated_at.toISOString();
    return { status: 200, body };
  }

  private async balance(db: Connection, body: Json): Promise<Result> {
    const p = this.features.method(body.funding_method, body.country_iso2);
    const a = await this.account(db, body.account_id, p.currencies[0]);
    return {
      status: 200,
      body: this.adapter.getBalance({ ...a, payment_data: body.payment_data, itemized: p.itemized }),
    };
  }

  async setState(db: Connection, tx: Json, state: string, detail?: string): Promise<void> {
    await db.query(
      `UPDATE transactions SET state=$4,detail=$5,updated_at=now()
      WHERE client_id=$1 AND kind=$2 AND id=$3`,
      [this.config.clientId, tx.kind, tx.id, state, detail ?? null],
    );
    tx.state = state;
    tx.detail = detail;
    tx.updated_at = new Date();
  }

  async expire(db: Connection, tx: Json, force: boolean): Promise<void> {
    if (
      (
        await db.query('SELECT 1 FROM resource_claims WHERE client_id=$1 AND resource_key=$2', [
          this.config.clientId,
          'charge:' + tx.id,
        ])
      ).rowCount
    )
      return;
    if (tx.state === 'AUTHORIZED' && (force || tx.expires_at?.getTime() <= Date.now())) {
      await this.ledger(db, tx.account_id, tx.currency, tx.amount_e5, 0n);
      await this.setState(db, tx, 'CANCELLED');
    }
  }

  async queue(
    db: Connection,
    kind: string,
    id: string,
    jobType: string,
    name: string,
    payload: Json,
    status = 'PENDING',
  ): Promise<void> {
    const sequence =
      jobType === 'CALLBACK'
        ? ((await db.query('SELECT reference_next_event($1,$2,$3) AS n', [this.config.clientId, kind, id]))
            .rows[0].n ?? 0n)
        : 0n;
    const jobId = hash(`${this.config.clientId}:${kind}:${id}:${jobType}:${name}:${sequence}`);
    await db.query(
      `INSERT INTO jobs(id,client_id,tx_kind,tx_id,job_type,callback_name,payload,status,next_attempt_at,event_sequence)
       VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT DO NOTHING`,
      [
        jobId,
        this.config.clientId,
        kind,
        id,
        jobType,
        name,
        encode(payload),
        status,
        new Date(Date.now() + (jobType === 'CALLBACK' ? 0 : this.config.asyncDelayMs)),
        sequence.toString(),
      ],
    );
  }

  async complete(
    db: Connection,
    kind: string,
    id: string,
    state: string,
    rejectionDetail = 'MERCHANT_REJECTED',
    updatedAmount?: Json,
  ): Promise<void> {
    if (!['CONFIRMED', 'REJECTED'].includes(state)) throw new Failure(400);
    const tx = await this.transaction(db, kind, id);
    if (tx.state !== 'PENDING') return;
    const profile = decode(tx.reference_profile || '{}');
    let amount: bigint = tx.amount_e5;
    const originalAmount = amount;
    if (
      updatedAmount !== undefined &&
      kind === 'charge' &&
      state === 'CONFIRMED' &&
      profile.callback_mode === 'deposit'
    ) {
      amount = this.amount(updatedAmount);
      if (amount > originalAmount) throw new Failure(400);
      if (amount !== originalAmount) {
        await this.ledger(db, tx.account_id, tx.currency, originalAmount - amount, 0n);
        await db.query(
          "UPDATE transactions SET amount_e5=$3 WHERE client_id=$1 AND kind='charge' AND id=$2",
          [this.config.clientId, id, amount.toString()],
        );
      }
    }
    if (kind === 'refund') {
      if (state === 'CONFIRMED') await this.ledger(db, tx.account_id, tx.currency, amount, amount);
    } else {
      await this.ledger(
        db,
        tx.account_id,
        tx.currency,
        state === 'REJECTED' ? amount : 0n,
        state === 'CONFIRMED' ? -amount : 0n,
      );
    }
    if (kind === 'payout' && state === 'CONFIRMED')
      await this.ledger(db, tx.payee_account_id, tx.currency, amount, amount);
    const detail = state === 'REJECTED' ? rejectionDetail : undefined;
    await this.setState(db, tx, state, detail);
    if (kind === 'charge' && profile.callback_mode === 'deposit') {
      if (state === 'CONFIRMED') {
        if (amount !== originalAmount)
          await this.queue(db, kind, id, 'CALLBACK', 'updateDeposit', {
            merchant_reference: tx.psp_reference,
            amount: { value: amount, currency: tx.currency },
          });
        await this.queue(db, kind, id, 'CALLBACK', 'confirmDeposit', {
          merchant_reference: tx.psp_reference,
        });
      } else
        await this.queue(db, kind, id, 'CALLBACK', 'cancelDeposit', {
          merchant_reference: tx.psp_reference,
          cancel_reason: rejectionDetail,
        });
      return;
    }
    const name = kind === 'refund' ? 'updateRefund' : kind === 'payout' ? 'payoutCallback' : 'chargeCallback';
    const payload: Json =
      kind === 'refund'
        ? {
            status: state,
            merchant_reference: tx.psp_reference,
            ...(detail ? { failed_reason: 'Synthetic refund rejected by the local processor' } : {}),
          }
        : { state: { value: state, ...(detail ? { detail } : {}) }, psp_reference: tx.psp_reference };
    this.contract.callback(name, id, payload);
    await this.queue(db, kind, id, 'CALLBACK', name, payload);
    if (
      this.config.processorMode !== 'http' &&
      kind === 'refund' &&
      state === 'CONFIRMED' &&
      profile.behavior === 'refund_reversal'
    )
      await this.queue(db, kind, id, 'REVERSAL', '', {});
  }

  async reverseRefund(db: Connection, id: string): Promise<void> {
    const changed = (
      await db.query('SELECT reference_reverse_refund($1,$2) AS changed', [this.config.clientId, id])
    ).rows[0].changed;
    if (changed) {
      const tx = await this.transaction(db, 'refund', id);
      await this.queue(db, 'refund', id, 'CALLBACK', 'updateRefund', {
        status: 'REJECTED',
        merchant_reference: tx.psp_reference,
        failed_reason: 'Synthetic processor reversed the refund; any unrecovered funds are recorded as debt',
      });
    }
  }

  private scopes(value: unknown): string {
    if (typeof value !== 'string' || !value.trim()) throw new Failure(400, 'invalid_scope');
    const scopes = value.split(' ');
    if (scopes.some((x) => !['charge', 'refund', 'get_balance', 'offline_access'].includes(x)))
      throw new Failure(400, 'invalid_scope');
    return [...new Set(scopes)].sort().join(' ');
  }

  async issue(
    db: Connection,
    kind: string,
    scope: string,
    account = 'demo-account',
    redirect: string | null = null,
    state: string | null = null,
    family: string = randomUUID(),
  ): Promise<string> {
    const token = randomBytes(32).toString('base64url');
    const seconds =
      kind === 'ACCESS' || kind === 'CODE' || kind === 'CONSENT' ? 600 : this.config.refreshTtlSeconds;
    await db.query(
      `INSERT INTO tokens(token_hash,kind,client_id,account_id,scope,redirect_uri,state,expires_at,family_id)
       VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)`,
      [
        hash(token),
        kind,
        this.config.clientId,
        account,
        scope,
        redirect,
        state,
        new Date(Date.now() + seconds * 1000),
        family,
      ],
    );
    return token;
  }

  async authorize(query: Json): Promise<{ consent: string; csrf: string; locale: string; details: Json }> {
    if (
      query.client_id !== this.config.clientId ||
      query.response_type !== 'code' ||
      !this.config.allowedRedirects.includes(query.redirect_uri) ||
      typeof query.state !== 'string' ||
      !query.state
    ) {
      throw new Failure(400);
    }
    const scope = this.scopes(query.scope);
    let details: Json;
    try {
      details = decode(query.authorization_details);
    } catch {
      throw new Failure(400);
    }
    this.contract.validate(details, {
      type: 'array',
      items: this.contract.spec.components.schemas.AuthorizationDetail,
    });
    if (!details.length) throw new Failure(400);
    for (const d of details) {
      this.features.method(d.funding_method, d.country_iso2);
      if (d.account_id && d.account_id !== 'demo-account') throw new Failure(400);
    }
    const csrf = randomBytes(32).toString('hex');
    const consent = await this.store.transaction((db) =>
      this.issue(
        db,
        'CONSENT',
        scope,
        'demo-account',
        query.redirect_uri,
        encode({ state: query.state, csrf, details, locale: this.features.locale(details[0].locale) }),
      ),
    );
    return { consent, csrf, details, locale: this.features.locale(details[0].locale) };
  }

  async consent(token: string, csrf: string, decision: string): Promise<string> {
    return this.store.transaction(async (db) => {
      const row = (
        await db.query(
          `SELECT * FROM tokens WHERE token_hash=$1 AND kind='CONSENT' AND client_id=$2 AND consumed=false AND expires_at>now()`,
          [hash(token), this.config.clientId],
        )
      ).rows[0];
      if (!row || !safeEqual(String(decode(row.state).csrf), csrf)) throw new Failure(400);
      if (!['approve', 'deny'].includes(decision)) throw new Failure(400);
      await db.query('UPDATE tokens SET consumed=true WHERE token_hash=$1', [hash(token)]);
      const url = new URL(row.redirect_uri);
      url.searchParams.set('state', decode(row.state).state);
      if (decision === 'approve')
        url.searchParams.set(
          'code',
          await this.issue(
            db,
            'CODE',
            row.scope,
            row.account_id,
            row.redirect_uri,
            encode({ details: decode(row.state).details, locale: decode(row.state).locale }),
            row.family_id,
          ),
        );
      else url.searchParams.set('error', 'access_denied');
      return url.toString();
    });
  }

  private async token(db: Connection, body: Json): Promise<Result> {
    if (
      body.client_id !== this.config.clientId ||
      !safeEqual(body.client_secret ?? '', this.config.clientSecret)
    )
      throw new Failure(400, 'invalid_client');
    let scope: string;
    let account = 'demo-account';
    let family: string | undefined;
    let consentContext: string | null = null;
    if (body.grant_type === 'urn:uberpay:params:oauth:grant-type:wallet-grant') {
      if (body.wallet_reference_data?.payment_identifier !== account) {
        throw new Failure(400, 'invalid_grant');
      }
      try {
        this.features.method(body.funding_method, body.country_iso2);
      } catch {
        throw new Failure(400, 'invalid_grant');
      }
      consentContext = encode({
        details: [{ funding_method: body.funding_method, country_iso2: body.country_iso2 }],
        locale: this.features.locale(),
      });
      scope = this.scopes(body.scope);
      if (body.account_id && body.account_id !== account) throw new Failure(400, 'invalid_grant');
    } else {
      const kind =
        body.grant_type === 'authorization_code'
          ? 'CODE'
          : body.grant_type === 'refresh_token'
            ? 'REFRESH'
            : '';
      if (!kind) throw new Failure(400, 'unsupported_grant_type');
      const value = kind === 'CODE' ? body.code : body.refresh_token;
      const row = (
        await db.query(
          `SELECT * FROM tokens WHERE token_hash=$1 AND kind=$2 AND client_id=$3 AND expires_at>now()`,
          [hash(String(value)), kind, this.config.clientId],
        )
      ).rows[0];
      if (!row || (kind === 'CODE' && body.redirect_uri !== row.redirect_uri))
        throw new Failure(400, 'invalid_grant');
      if (row.consumed) {
        if (kind === 'CODE')
          await db.query('UPDATE tokens SET consumed=true WHERE client_id=$1 AND family_id=$2', [
            this.config.clientId,
            row.family_id,
          ]);
        return { status: 400, body: { error: 'invalid_grant' } };
      }
      family = row.family_id;
      consentContext = row.state;
      scope = row.scope;
      account = row.account_id;
      if (kind === 'CODE')
        await db.query('UPDATE tokens SET consumed=true WHERE token_hash=$1', [hash(value)]);
    }
    if (body.grant_type === 'refresh_token') {
      return {
        status: 200,
        body: {
          access_token: await this.issue(db, 'ACCESS', scope, account, null, consentContext, family),
          token_type: 'bearer',
          expires_in: '600',
        },
      };
    }
    return {
      status: 200,
      body: {
        refresh_token: await this.issue(db, 'REFRESH', scope, account, null, consentContext, family),
        token_type: 'bearer',
        scope,
        expires_in: String(this.config.refreshTtlSeconds),
        authorization_details: [
          {
            type: 'authorized_account',
            account_id: account,
            fingerprint: hash(account),
            display_name: 'Local demo wallet',
          },
        ],
      },
    };
  }
}
