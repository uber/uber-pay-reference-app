import { createHash, createVerify, timingSafeEqual, type KeyLike } from 'node:crypto';
import type { IncomingHttpHeaders } from 'node:http';

function single(headers: IncomingHttpHeaders, key: string): string {
  const v = headers[key];
  if (typeof v !== 'string' || !v) throw new Error(`Missing ${key}`);
  return v;
}

export function parseSignature(header: string): Record<string, string> {
  if (header.length > 8192 || header.trimEnd().endsWith(',')) throw new Error('Invalid Signature length or trailing delimiter');
  const out: Record<string, string> = {};
  const matcher = /\s*([A-Za-z][A-Za-z0-9]*)="([^"\\\r\n]*)"\s*(?:,|$)/gy;
  let pos = 0;
  while (pos < header.length) {
    matcher.lastIndex = pos;
    const match = matcher.exec(header);
    if (!match || match.index !== pos) throw new Error('Malformed Signature');
    const name = match[1]!;
    if (out[name] !== undefined) throw new Error('Duplicate Signature field');
    out[name] = match[2]!;
    pos = matcher.lastIndex;
  }
  if (Object.keys(out).some(k => !['keyId','algorithm','headers','signature','created','expires'].includes(k)) || !out.keyId || !out.algorithm || !out.headers || !out.signature) {
    throw new Error('Incomplete Signature');
  }
  return out;
}

export function verifyRequest(
  method: string, target: string, headers: IncomingHttpHeaders, body: Buffer,
  keys: Record<string, KeyLike>, skewSeconds = 300, now = Date.now(),
): void {
  const digest = single(headers, 'digest');
  const expected = `SHA-256=${createHash('sha256').update(body).digest('base64')}`;
  const a = Buffer.from(digest);
  const b = Buffer.from(expected);
  if (a.length !== b.length || !timingSafeEqual(a, b)) throw new Error('Digest mismatch');
  const date = single(headers, 'date');
  const timestamp = Date.parse(date);
  if (!Number.isFinite(timestamp) || new Date(timestamp).toUTCString() !== date || Math.abs(now - timestamp) > skewSeconds * 1000) {
    throw new Error('Date outside freshness window');
  }
  const signature = parseSignature(single(headers, 'signature'));
  if (signature.algorithm !== 'rsa-sha256' || signature.headers !== '(request-target) host date digest') {
    throw new Error('Unsupported signature algorithm or header order');
  }
  for (const field of ['created','expires']) {
    const value = signature[field];
    if (value !== undefined && (!/^\d{1,12}$/.test(value) || !Number.isSafeInteger(Number(value)))) throw new Error('Invalid signature time');
  }
  if (signature.created !== undefined && Math.abs(now / 1000 - Number(signature.created)) > skewSeconds) throw new Error('Stale created time');
  if (signature.expires !== undefined && (Number(signature.expires) < now / 1000 || Number(signature.expires) < Number(signature.created ?? 0))) throw new Error('Expired signature');
  const key = Object.hasOwn(keys, signature.keyId!) ? keys[signature.keyId!] : undefined;
  if (!key) throw new Error('Unknown keyId');
  if (!/^[A-Za-z0-9+/]+={0,2}$/.test(signature.signature!)) throw new Error('Invalid signature encoding');
  const payload = `(request-target): ${method.toLowerCase()} ${target}\nhost: ${single(headers, 'host')}\ndate: ${date}\ndigest: ${digest}`;
  if (!createVerify('RSA-SHA256').update(payload).verify(key, signature.signature!, 'base64')) {
    throw new Error('Invalid signature');
  }
}

export function safeEqual(a: string, b: string): boolean {
  const aa = Buffer.from(a);
  const bb = Buffer.from(b);
  return aa.length === bb.length && timingSafeEqual(aa, bb);
}
