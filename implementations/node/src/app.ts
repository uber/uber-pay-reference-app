import express, { type Request, type Response } from 'express';
import type { Config } from './config.js';
import { Contract, ContractError } from './contract.js';
import { decode, encode, hash, type Json } from './codec.js';
import { safeEqual, verifyRequest } from './security.js';
import { Failure, Service, type Result } from './service.js';
import { Store } from './store.js';
import { ProcessorExecutor } from './processor.js';

const html = (value: string): string =>
  value.replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]!,
  );
function send(res: Response, result: Result): void {
  res.status(result.status);
  for (const [key, value] of Object.entries(result.headers ?? {})) res.setHeader(key, value);
  const body = result.text ?? (result.body === undefined ? '' : encode(result.body));
  if (body) res.type('application/json').send(body);
  else res.end();
}
function oneQuery(req: Request): Json {
  const query = req.originalUrl.split('?', 2)[1] ?? '';
  try {
    decodeURIComponent(query.replace(/\+/g, ' '));
  } catch {
    throw new Failure(400);
  }
  const params = new URL(req.originalUrl, 'http://localhost').searchParams;
  const out: Json = {};
  for (const [key, value] of params) {
    if (Object.hasOwn(out, key)) throw new Failure(400);
    out[key] = value;
  }
  return out;
}

export function createApp(
  config: Config,
  store: Store,
  contract = new Contract(config.root, config.contractFile, config.strictAccountName),
) {
  const service = new Service(config, store, contract);
  if (config.processorMode === 'http') service.executor = new ProcessorExecutor(service);
  const app = express();
  app.disable('x-powered-by');
  app.set('trust proxy', false);
  app.use((req, res, next) => {
    res.setHeader('Content-Language', service.features.locale(req.get('accept-language')));
    res.setHeader('Cache-Control', 'no-store');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('Referrer-Policy', 'no-referrer');
    res.setHeader(
      'Content-Security-Policy',
      "default-src 'none'; form-action 'self'; frame-ancestors 'none'",
    );
    next();
  });
  app.use(express.raw({ type: () => true, limit: 1048576 }));
  app.get('/health', async (_req, res) => {
    try {
      await store.pool.query('SELECT 1');
      res.json({ status: 'ok', language: 'node', contract_version: '1.4' });
    } catch {
      res.status(503).json({ status: 'unavailable' });
    }
  });

  app.get(config.basePath + '/authorize', async (req, res) => {
    let query: Json;
    try {
      query = oneQuery(req);
      if (query.client_id !== config.clientId || !config.allowedRedirects.includes(query.redirect_uri))
        throw new Failure(400);
      const { consent, csrf, locale, details } = await service.authorize(query);
      const text = (key: string) => html(service.features.text(locale, key));
      res.setHeader('Content-Language', locale);
      const methods = details
        .map(
          (d: Json) =>
            `<li>${text('method')}: ${html(d.funding_method)} — ${text('country')}: ${html(d.country_iso2)}</li>`,
        )
        .join('');
      res.type('html').send(`<!doctype html><html><head><title>Local demo wallet consent</title></head><body>
<h1>${text('consent_title')}</h1><p>${text('synthetic')}</p><ul>${methods}</ul><p>${text('scopes')}: ${html(query.scope)}</p>
<form method="post" action="/demo/consent"><input type="hidden" name="consent_token" value="${html(consent)}">
<input type="hidden" name="csrf" value="${html(csrf)}"><button name="decision" value="approve">${text('approve')}</button>
<button name="decision" value="deny">${text('deny')}</button></form></body></html>`);
    } catch (error) {
      if (
        query?.client_id === config.clientId &&
        config.allowedRedirects.includes(query.redirect_uri) &&
        (error instanceof Failure || error instanceof SyntaxError || error instanceof ContractError)
      ) {
        const target = new URL(query.redirect_uri);
        target.searchParams.set(
          'error',
          error instanceof Failure && error.detail === 'invalid_scope' ? 'invalid_scope' : 'invalid_request',
        );
        if (typeof query.state === 'string') target.searchParams.set('state', query.state);
        res.redirect(302, target.toString());
      } else res.status(error instanceof Failure ? 400 : 500).send('Authorization request failed');
    }
  });
  app.post('/demo/consent', async (req, res) => {
    try {
      const p = new URLSearchParams(req.body.toString());
      const url = await service.consent(
        p.get('consent_token') ?? '',
        p.get('csrf') ?? '',
        p.get('decision') ?? '',
      );
      res.redirect(302, url);
    } catch {
      res.status(400).send('Invalid or expired consent');
    }
  });
  app.get(['/demo/pay', '/demo/action'], async (req, res) => {
    try {
      const token = oneQuery(req).token;
      const job = (
        await store.pool.query(
          `SELECT payload FROM jobs WHERE client_id=$1 AND status='WAITING' AND payload::jsonb->>'payment_token'=$2`,
          [config.clientId, token],
        )
      ).rows[0];
      if (!job) throw new Failure(400);
      const payload = decode(job.payload);
      if (Number(payload.expires_at) <= Date.now()) throw new Failure(400);
      const language = payload.locale ?? service.features.locale(req.get('accept-language'));
      const text = (key: string) => html(service.features.text(language, key));
      res.setHeader('Content-Language', language);
      const amount = payload.profile?.allow_amount_update
        ? `<label>${text('amount')}<input name="amount_e5" inputmode="numeric"></label>`
        : '';
      res.type('html').send(`<!doctype html><html><head><title>Complete local payment</title></head><body>
<h1>${text('payment_title')}</h1><p>${text('synthetic')}</p><form method="post" action="/demo/complete">
<input type="hidden" name="payment_token" value="${html(token)}"><input type="hidden" name="csrf" value="${html(payload.csrf)}">
${amount}<button name="decision" value="approve">${text('confirm')}</button><button name="decision" value="deny">${text('reject')}</button></form></body></html>`);
    } catch {
      res.status(400).send('Invalid payment session');
    }
  });
  app.post('/demo/complete', async (req, res) => {
    try {
      const form = new URLSearchParams(req.body.toString());
      const returnUrl = await store.transaction(async (db) => {
        const job = (
          await db.query(
            `SELECT * FROM jobs WHERE client_id=$1 AND status='WAITING' AND payload::jsonb->>'payment_token'=$2`,
            [config.clientId, form.get('payment_token')],
          )
        ).rows[0];
        if (!job || !['approve', 'deny'].includes(form.get('decision') ?? '')) throw new Failure(400);
        const payload = decode(job.payload);
        if (Number(payload.expires_at) <= Date.now()) throw new Failure(400);
        if (!safeEqual(payload.csrf, form.get('csrf') ?? '')) throw new Failure(400);
        payload.state = form.get('decision') === 'approve' ? 'CONFIRMED' : 'REJECTED';
        payload.detail = 'USER_CANCELLED';
        const amount = form.get('amount_e5');
        if (amount) {
          if (!payload.profile?.allow_amount_update || !/^[1-9][0-9]*$/.test(amount)) throw new Failure(400);
          const tx = (
            await db.query(
              "SELECT amount_e5 FROM transactions WHERE client_id=$1 AND kind='charge' AND id=$2",
              [config.clientId, job.tx_id],
            )
          ).rows[0];
          if (BigInt(amount) > tx.amount_e5) throw new Failure(400);
          payload.amount_e5 = BigInt(amount);
        }
        await db.query(`UPDATE jobs SET payload=$2,status='PENDING',next_attempt_at=now() WHERE id=$1`, [
          job.id,
          encode(payload),
        ]);
        return payload.return_url;
      });
      if (config.allowedRedirects.includes(returnUrl)) res.redirect(302, returnUrl);
      else res.status(200).send('Local payment completion scheduled');
    } catch {
      res.status(400).send('Invalid payment completion');
    }
  });

  app.post('/adapter/events', async (req, res) => {
    if (!service.executor || !safeEqual(req.get('authorization') ?? '', 'Bearer ' + config.processorToken)) {
      res.status(404).end();
      return;
    }
    try {
      await service.executor.event(decode(req.body));
      res.status(204).end();
    } catch (e) {
      res.status(e instanceof Failure ? e.status : 400).end();
    }
  });

  app.use('/_test', async (req, res) => {
    if (!config.enableControl || !safeEqual(req.get('X-Test-Control-Secret') ?? '', config.controlSecret)) {
      res.status(404).end();
      return;
    }
    try {
      if (req.method === 'GET' && req.path === '/state') {
        const state = await store.transaction(async (db) => {
          const values: Json = {};
          for (const table of ['accounts', 'transactions', 'jobs']) {
            // Names are a fixed internal allowlist, not user input.
            values[table] = (
              await db.query(`SELECT * FROM ${table} WHERE client_id=$1`, [config.clientId])
            ).rows;
          }
          return values;
        });
        send(res, { status: 200, body: state });
        return;
      }
      const body = decode(req.body);
      await store.transaction(async (db) => {
        if (req.path === '/complete') {
          const kind = body.kind ?? 'charge';
          if (
            !['charge', 'refund', 'payout'].includes(kind) ||
            !['CONFIRMED', 'REJECTED'].includes(body.state)
          )
            throw new Failure(400);
          const job = (
            await db.query(
              `SELECT * FROM jobs WHERE client_id=$1 AND tx_kind=$2 AND tx_id=$3 AND job_type='COMPLETION'`,
              [config.clientId, kind, body.id],
            )
          ).rows[0];
          if (!job) throw new Failure(404);
          if (['WAITING', 'PENDING'].includes(job.status)) {
            await db.query(`UPDATE jobs SET status='PENDING',payload=$2,next_attempt_at=now() WHERE id=$1`, [
              job.id,
              encode({ ...decode(job.payload), state: body.state }),
            ]);
          }
        } else if (req.path === '/reverse-refund') {
          await service.reverseRefund(db, body.id);
        } else if (req.path === '/expire') {
          const tx = (
            await db.query(`SELECT * FROM transactions WHERE client_id=$1 AND kind='charge' AND id=$2`, [
              config.clientId,
              body.id,
            ])
          ).rows[0];
          if (!tx) throw new Failure(404);
          await service.expire(db, tx, true);
        } else if (req.path === '/callback') {
          contract.callback(body.name, body.id, body.payload);
          await service.queue(db, `test-${body.name}`, body.id, 'CALLBACK', body.name, body.payload);
        } else throw new Failure(404);
      });
      send(res, { status: 200, body: { ok: true } });
    } catch (error) {
      res.status(error instanceof Failure ? error.status : 400).end();
    }
  });

  app.use(async (req, res) => {
    const suffix = req.path.startsWith(config.basePath + '/') ? req.path.slice(config.basePath.length) : '';
    const path = /^\/charge\/[^/]+\/(capture|cancel)$/.test(suffix)
      ? suffix.replace(/^\/charge\/[^/]+/, '/charge/{id}')
      : suffix;
    const op = contract.operation(req.method, path);
    if (!op) {
      res.status(404).end();
      return;
    }
    let body: Json;
    try {
      verifyRequest(
        req.method,
        req.originalUrl,
        req.headers,
        Buffer.isBuffer(req.body) ? req.body : Buffer.alloc(0),
        { [config.keyId]: config.publicKey },
        config.skewSeconds,
      );
    } catch {
      send(res, service.error(op, 401));
      return;
    }
    try {
      if (req.get('content-type')?.split(';')[0]?.trim().toLowerCase() !== 'application/json')
        throw new Failure(400);
      body = req.method === 'GET' ? oneQuery(req) : decode(req.body);
      if (!body || typeof body !== 'object' || Array.isArray(body)) throw new Failure(400);
      for (const entry of op.parameters ?? []) {
        const param = contract.dereference(entry);
        if (param.in === 'header' && param.required && !req.get(param.name)) throw new Failure(400);
        if (param.in === 'query' && param.required && typeof body[param.name] !== 'string')
          throw new Failure(400);
      }
      const scope =
        op.operationId === 'getBalance'
          ? 'get_balance'
          : op.operationId === 'initRefund'
            ? 'refund'
            : 'charge';
      const moneyAuth = ['charge', 'capture', 'cancel', 'initRefund'].includes(op.operationId);
      const bearerNeeded =
        op.operationId === 'getBalance' ||
        (moneyAuth &&
          (config.oauthRequired ||
            body.authorization?.type === 'access_token_authorization' ||
            !!req.get('authorization')));
      if (bearerNeeded) {
        let account = op.operationId === 'getBalance' ? body.account_id : body.authorization?.account_id;
        const resourceId = ['capture', 'cancel'].includes(op.operationId)
          ? decodeURIComponent(req.path.split('/').at(-2)!)
          : op.operationId === 'initRefund'
            ? body.original_transaction_id
            : undefined;
        if (resourceId)
          account = (
            await store.pool.query(
              `SELECT account_id FROM transactions WHERE client_id=$1 AND kind='charge' AND id=$2`,
              [config.clientId, resourceId],
            )
          ).rows[0]?.account_id;
        await service.bearer(req.get('authorization'), scope, account);
      }
      const execute =
        service.executor && ['charge', 'capture', 'cancel', 'initRefund', 'payout'].includes(op.operationId)
          ? service.executor
          : service;
      const result = await execute.execute(
        op,
        req.path,
        body,
        req.get('x-idempotency-key'),
        req.get('accept-language'),
      );
      send(res, result);
    } catch (error) {
      const status =
        error instanceof Failure
          ? error.status
          : error instanceof SyntaxError || error instanceof ContractError
            ? 400
            : 500;
      send(res, service.error(op, status, error instanceof Failure ? error.detail : 'VALIDATION_ERROR'));
    }
  });
  app.use((error: Json, _req: Request, res: Response, _next: express.NextFunction) => {
    res.status(error?.type === 'entity.too.large' ? 413 : 400).end();
  });
  return { app, service };
}
