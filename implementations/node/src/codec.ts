import JSONBigFactory from 'json-bigint';
import { createHash } from 'node:crypto';

const JSONBig = JSONBigFactory({
  useNativeBigInt: true,
  // Small JSON numbers can stay native; large integral values become BigInt.
  // Domain money conversion below rejects unsafe Numbers rather than rounding.
  alwaysParseAsBig: false,
  protoAction: 'error',
  constructorAction: 'error',
  strict: true,
});

export type Json = any;
export function decode(value: string | Buffer): Json {
  try {
    const text = Buffer.isBuffer(value) ? new TextDecoder('utf-8', { fatal: true }).decode(value) : value;
    JSON.parse(text); // Grammar only: discard this result; exact-number parser remains authoritative.
    return JSONBig.parse(text);
  }
  catch { throw new SyntaxError('Invalid JSON'); }
}
export const encode = (value: Json): string => JSONBig.stringify(value);

export function canonical(value: Json): string {
  if (value === null || typeof value !== 'object') return encode(value);
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  return `{${Object.keys(value).sort().map(k => `${JSON.stringify(k)}:${canonical(value[k])}`).join(',')}}`;
}

export const hash = (value: string | Buffer): string => createHash('sha256').update(value).digest('hex');
export const fingerprint = (target: string, value: Json): string => hash(`${target}\n${canonical(value)}`);

export function exactInteger(value: unknown): bigint {
  if (typeof value === 'bigint') return value;
  if (typeof value === 'number' && Number.isSafeInteger(value)) return BigInt(value);
  throw new Error('Expected an exact integer');
}

export const MIN_INT64 = -(1n << 63n);
export const MAX_INT64 = (1n << 63n) - 1n;

export function checkedAmount(value: Json): bigint {
  const n = exactInteger(value);
  if (n <= 0n || n > MAX_INT64) throw new Error('Amount must be a positive int64 E5 integer');
  return n;
}
