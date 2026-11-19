import test from 'node:test';
import assert from 'node:assert/strict';
import { generateKeyPairSync, createHash, sign } from 'node:crypto';
import { resolve } from 'node:path';
import { decode, encode, canonical, checkedAmount, MAX_INT64 } from '../dist/codec.js';
import { parseSignature, verifyRequest } from '../dist/security.js';
import { Contract } from '../dist/contract.js';
import { DemoPspAdapter } from '../dist/adapter.js';
import { localUrl } from '../dist/config.js';
import { Worker } from '../dist/worker.js';

const root = resolve(import.meta.dirname, '../../..');
const contract = new Contract(root);
const { privateKey, publicKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });

function signed(body = Buffer.from('{}'), method = 'POST', target = '/uberpay/v1/charge') {
  const date = new Date().toUTCString();
  const digest = `SHA-256=${createHash('sha256').update(body).digest('base64')}`;
  const payload = `(request-target): ${method.toLowerCase()} ${target}\nhost: localhost:8080\ndate: ${date}\ndigest: ${digest}`;
  const signature = sign('RSA-SHA256', Buffer.from(payload), privateKey).toString('base64');
  return { host: 'localhost:8080', date, digest, signature: `keyId="test",algorithm="rsa-sha256",headers="(request-target) host date digest",signature="${signature}"` };
}

test('E5 int64 values round trip without Number coercion', () => {
  const input = '{"amount":{"value":9007199254740993,"currency":"BRL"}}';
  assert.equal(decode(input).amount.value, 9007199254740993n);
  assert.equal(encode(decode(input)), input);
  assert.equal(canonical(decode('{"b":2,"a":9007199254740993}')), '{"a":9007199254740993,"b":2}');
  assert.equal(decode('{"extra":1.5}').extra, 1.5);
});

test('amount bounds reject zero, fractional, overflow and unsafe Number', () => {
  for (const value of [0n, -1n, 1.1, MAX_INT64 + 1n, Number.MAX_SAFE_INTEGER + 1]) {
    assert.throws(() => checkedAmount(value));
  }
  assert.equal(checkedAmount(MAX_INT64), MAX_INT64);
});

test('JSON parser rejects duplicate/prototype keys', () => {
  for (const text of ['{"a":1,"a":2}', '{"__proto__":{}}', '{"constructor":{}}']) {
    assert.throws(() => decode(text));
  }
});

test('signed request supports exact empty GET body and escaped query', () => {
  for (const [method, target, body] of [
    ['POST', '/uberpay/v1/charge', Buffer.from('{ "amount": 1 }')],
    ['GET', '/uberpay/v1/find-transaction?id=a%2Fb&merchant_reference=x', Buffer.alloc(0)],
  ]) verifyRequest(method, target, signed(body, method, target), body, { test: publicKey });
});

test('signature rejects changed body, wrong target and unknown key', () => {
  const headers = signed();
  assert.throws(() => verifyRequest('POST', '/uberpay/v1/charge', headers, Buffer.from('{ }'), { test: publicKey }));
  assert.throws(() => verifyRequest('POST', '/wrong', headers, Buffer.from('{}'), { test: publicKey }));
  assert.throws(() => verifyRequest('POST', '/uberpay/v1/charge', headers, Buffer.from('{}'), {}));
});

test('malformed Signature parser is bounded and rejects duplicated parameters', () => {
  for (const value of ['keyId="unterminated', 'missing', 'keyId="a",keyId="b"', 'a="x",', 'x'.repeat(9000)]) {
    assert.throws(() => parseSignature(value));
  }
});

test('signature rejects stale and far-future dates', () => {
  const body = Buffer.from('{}');
  const headers = signed(body);
  for (const now of [Date.now() + 1000000, Date.now() - 1000000]) {
    assert.throws(() => verifyRequest('POST', '/uberpay/v1/charge', headers, body, { test: publicKey }, 300, now));
  }
});

test('strict algorithm and declared header order are verified', () => {
  const body = Buffer.from('{}');
  for (const signature of [
    signed(body).signature.replace('rsa-sha256', 'rsa-sha1'),
    signed(body).signature.replace('(request-target) host date digest', '(request-target) date digest'),
  ]) assert.throws(() => verifyRequest('POST', '/uberpay/v1/charge', { ...signed(body), signature }, body, { test: publicKey }));
});

test('request validator checks nested required fields and int64 overflow', () => {
  const currency = contract.spec.components.schemas.CurrencyAmount;
  contract.validate({ value: 9007199254740993n, currency: 'BRL' }, currency);
  for (const value of [{ value: 1n }, { value: MAX_INT64 + 1n, currency: 'BRL' }, { value: '1', currency: 'BRL' }]) {
    assert.throws(() => contract.validate(value, currency));
  }
  assert.doesNotThrow(() => contract.validate({ type: 'Account', id: 'x' }, contract.spec.components.schemas.Account));
  assert.throws(() => contract.validate({ type: 'Account' }, contract.spec.components.schemas.Account));
  const strict = new Contract(root, 'contracts/runtime.json', true);
  assert.throws(() => strict.validate({ type: 'Account', id: 'x' }, strict.spec.components.schemas.Account));
});

test('all six callback builders enforce required payloads and correct scopes', () => {
  const payloads = {
    chargeCallback: { state: { value: 'CONFIRMED' }, psp_reference: 'p' },
    payoutCallback: { state: { value: 'CONFIRMED' }, psp_reference: 'p' },
    updateRefund: { status: 'CONFIRMED' },
    confirmDeposit: {},
    cancelDeposit: { cancel_reason: 'USER_CANCELLED' },
    updateDeposit: { merchant_reference: 'p' },
  };
  for (const [name, payload] of Object.entries(payloads)) {
    const value = contract.callback(name, 'a/b', payload);
    assert.ok(value.path.includes('a%2Fb'));
    assert.equal(value.scope, name === 'payoutCallback' ? 'payments.payouts' : 'payments.deposits');
    assert.equal(value.idempotent, name !== 'updateRefund');
  }
  assert.throws(() => contract.callback('payoutCallback', 'id', { state: { value: 'PENDING' } }));
});

test('grant-selected token response avoids overlapping upstream oneOf', () => {
  contract.response(contract.operation('post', '/token'), 200,
    { access_token: 'a', token_type: 'bearer', expires_in: '600' }, 'refresh_token');
  assert.throws(() => contract.response(contract.operation('post', '/token'), 200,
    { access_token: 'a', token_type: 'Bearer' }, 'refresh_token'));
});

test('fake adapter scenarios cannot silently accept unknown funding methods', () => {
  const adapter = new DemoPspAdapter();
  assert.equal(adapter.charge({ funding_method: 'DEMO_SYNC' }, 'p').state, 'CONFIRMED');
  assert.equal(adapter.charge({ funding_method: 'DEMO_REJECT' }, 'p').state, 'REJECTED');
  assert.equal(adapter.charge({ funding_method: 'DEMO_PENDING_REJECT' }, 'p').finalState, 'REJECTED');
  assert.equal(adapter.charge({ funding_method: 'DEMO_QR' }, 'p').userAction, 'qr_code_input_action');
  assert.throws(() => adapter.charge({ funding_method: 'REAL' }, 'p'));
});

test('outbound configuration refuses real hosts, credentials and redirector URLs', () => {
  for (const value of ['https://api.uber.com', 'http://localhost.attacker.invalid', 'http://user:pass@localhost', 'file:///etc/passwd']) {
    assert.throws(() => localUrl(value));
  }
  assert.equal(localUrl('http://mock-uber:9090').hostname, 'mock-uber');
});

test('stale callback attempt cannot acknowledge a newly reclaimed job', async t => {
  const persisted = { status: 'PROCESSING', attempts: 2 };
  const service = {
    config: {
      tokenUrl: 'http://localhost:9090/token', callbackBase: 'http://localhost:9090',
      uberClientId: 'local', uberClientSecret: 'local', maxAttempts: 5, retryMs: 1,
    },
    contract,
    store: {
      transaction: async fn => fn({
        query: async (sql, params) => {
          // Model a newer lease-holder's record, including the SQL attempt fence.
          if (sql.includes('attempts=$5') && params[4] !== persisted.attempts) return { rowCount: 0 };
          persisted.status = params[1];
          return { rowCount: 1 };
        },
      }),
    },
  };
  t.mock.method(globalThis, 'fetch', async url => String(url).endsWith('/token')
    ? new Response('{"access_token":"local-only","expires_in":600}', { status: 200 })
    : new Response(null, { status: 200 }));
  const worker = new Worker(service);
  const job = { id: 'local-job', tx_id: 'local-tx', callback_name: 'chargeCallback',
    payload: '{"state":{"value":"CONFIRMED"},"psp_reference":"local"}' };
  await worker.deliver({ ...job, attempts: 1 });
  assert.equal(persisted.status, 'PROCESSING');
  await worker.deliver({ ...job, attempts: 2 });
  assert.equal(persisted.status, 'DONE');
});
