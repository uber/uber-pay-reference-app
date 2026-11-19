import { decode, encode, type Json } from './codec.js';
import { Service } from './service.js';

export class Worker {
  private timer?: NodeJS.Timeout;
  private running = false;
  private stopped = false;
  private readonly tokens = new Map<string, { value: string; expires: number }>();
  private readonly renewing = new Map<string, Promise<string>>();
  constructor(readonly service: Service) {}

  start(): void {
    this.timer = setInterval(() => {
      void this.tick();
    }, this.service.config.workerIntervalMs);
    this.timer.unref();
    void this.tick();
  }
  async stop(): Promise<void> {
    this.stopped = true;
    if (this.timer) clearInterval(this.timer);
    while (this.running) await new Promise((resolve) => setTimeout(resolve, 20));
  }
  private async token(scope: string): Promise<string> {
    const cached = this.tokens.get(scope);
    if (cached && cached.expires > Date.now()) return cached.value;
    const inFlight = this.renewing.get(scope);
    if (inFlight) return inFlight;
    const request = (async () => {
      const config = this.service.config;
      const response = await fetch(config.tokenUrl, {
        method: 'POST',
        redirect: 'error',
        signal: AbortSignal.timeout(5000),
        headers: { 'content-type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({
          grant_type: 'client_credentials',
          client_id: config.uberClientId,
          client_secret: config.uberClientSecret,
          scope,
        }),
      });
      if (!response.ok) throw new Error('Callback credential exchange failed');
      const packet = decode(await response.text());
      const lifetime = Number(packet.expires_in);
      if (typeof packet.access_token !== 'string' || !Number.isFinite(lifetime) || lifetime <= 0)
        throw new Error('Malformed credential response');
      this.tokens.set(scope, {
        value: packet.access_token,
        expires: Date.now() + Math.max(0, lifetime - Math.min(60, lifetime / 10)) * 1000,
      });
      return packet.access_token as string;
    })();
    this.renewing.set(scope, request);
    try {
      return await request;
    } finally {
      this.renewing.delete(scope);
    }
  }
  async tick(): Promise<void> {
    if (this.running || this.stopped) return;
    this.running = true;
    try {
      const { store, config } = this.service;
      await store.transaction(async (db) => {
        const expiring = await db.query(
          `SELECT * FROM transactions WHERE client_id=$1 AND state='AUTHORIZED' AND expires_at<=now()`,
          [config.clientId],
        );
        for (const tx of expiring.rows) await this.service.expire(db, tx, false);
        const actions = await db.query(
          `SELECT * FROM jobs WHERE client_id=$1 AND job_type='COMPLETION' AND status='WAITING'`,
          [config.clientId],
        );
        for (const action of actions.rows) {
          const payload = decode(action.payload);
          if (Number(payload.expires_at) <= Date.now()) {
            await this.service.complete(db, action.tx_kind, action.tx_id, 'REJECTED', 'MERCHANT_EXPIRED');
            await db.query(`UPDATE jobs SET status='DONE',attempts=attempts+1 WHERE id=$1`, [action.id]);
          }
        }
        const pending = await db.query(
          `SELECT * FROM jobs WHERE client_id=$1 AND job_type='COMPLETION' AND status='PENDING'
           AND next_attempt_at<=now() ORDER BY next_attempt_at LIMIT 10`,
          [config.clientId],
        );
        for (const job of pending.rows) {
          await this.service.complete(
            db,
            job.tx_kind,
            job.tx_id,
            decode(job.payload).state,
            decode(job.payload).detail,
            decode(job.payload).amount_e5,
          );
          await db.query(`UPDATE jobs SET status='DONE',attempts=attempts+1 WHERE id=$1`, [job.id]);
        }
        const reviews = await db.query(
          `SELECT * FROM jobs j WHERE j.client_id=$1 AND j.job_type='REVERSAL' AND j.status='PENDING' AND j.next_attempt_at<=now()`,
          [config.clientId],
        );
        for (const review of reviews.rows) {
          await this.service.reverseRefund(db, review.tx_id);
          await db.query("UPDATE jobs SET status='DONE' WHERE id=$1", [review.id]);
        }
      });
      const job = await store.transaction(async (db) => {
        const row = (
          await db.query(
            `SELECT * FROM jobs j WHERE client_id=$1 AND job_type='CALLBACK'
           AND ((status='PENDING' AND next_attempt_at<=now()) OR (status='PROCESSING' AND lease_until<now()))
           AND NOT EXISTS(SELECT 1 FROM jobs older WHERE older.client_id=j.client_id AND older.tx_kind=j.tx_kind AND older.tx_id=j.tx_id AND older.job_type='CALLBACK' AND older.event_sequence<j.event_sequence AND older.status<>'DONE')
           ORDER BY next_attempt_at,event_sequence LIMIT 1 FOR UPDATE SKIP LOCKED`,
            [config.clientId],
          )
        ).rows[0];
        if (!row) return undefined;
        await db.query(
          `UPDATE jobs SET status='PROCESSING',attempts=attempts+1,lease_until=now()+interval '30 seconds' WHERE id=$1`,
          [row.id],
        );
        row.attempts++;
        return row;
      });
      if (job) await this.deliver(job);
    } catch {
      // No headers, tokens, body, or raw SQL errors in logs.
      console.error('worker iteration failed; durable work retained');
    } finally {
      this.running = false;
    }
  }

  private async deliver(job: Json): Promise<void> {
    const { config, store, contract } = this.service;
    let status: number | undefined;
    try {
      const payload = decode(job.payload);
      const callback = contract.callback(job.callback_name, job.tx_id, payload);
      const token = await this.token(callback.scope);
      const headers: Record<string, string> = {
        'content-type': 'application/json',
        authorization: `Bearer ${token}`,
      };
      if (callback.idempotent) headers['x-idempotency-key'] = job.id;
      const response = await fetch(config.callbackBase + callback.path, {
        method: 'POST',
        redirect: 'error',
        signal: AbortSignal.timeout(5000),
        headers,
        body: encode(payload),
      });
      status = response.status;
      await response.body?.cancel();
      if (status === 401) this.tokens.delete(callback.scope);
    } catch {
      /* Network/credential failures are retryable, not a second PSP effect. */
    }
    const retryable = status === undefined || status === 401 || status === 429 || status >= 500;
    const state =
      status !== undefined && status >= 200 && status < 300
        ? 'DONE'
        : status === 409
          ? 'RECONCILE'
          : retryable && job.attempts < config.maxAttempts
            ? 'PENDING'
            : 'FAILED';
    const delay =
      Math.min(60000, config.retryMs * 2 ** Math.min(job.attempts - 1, 10)) +
      (Math.random() * config.retryMs) / 5;
    await store.transaction(async (db) => {
      await db.query(
        `UPDATE jobs SET status=$2,next_attempt_at=$3,lease_until=NULL,last_error=$4
         WHERE id=$1 AND status='PROCESSING' AND attempts=$5`,
        [
          job.id,
          state,
          new Date(Date.now() + delay),
          status === undefined ? 'network_or_credential_failure' : `HTTP ${status}`,
          job.attempts,
        ],
      );
    });
  }
}
