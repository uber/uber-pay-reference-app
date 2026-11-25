import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { decode, exactInteger, MAX_INT64, MIN_INT64, type Json } from './codec.js';

export class ContractError extends Error {}

export class Contract {
  readonly spec: Json;
  constructor(root: string, file = 'contracts/runtime.json', strictAccountName = false) {
    this.spec = decode(readFileSync(resolve(root, file)));
    if (strictAccountName) this.spec.components.schemas.Account.required.push('name');
    // A recorded syntax-only erratum. Do not change upstream semantic enums.
    const required = this.spec.components.schemas.PayoutCallbackState.required;
    if (typeof required === 'string') this.spec.components.schemas.PayoutCallbackState.required = [required];
  }

  dereference(schema: Json): Json {
    if (!schema?.$ref) return schema;
    if (!schema.$ref.startsWith('#/')) throw new ContractError('External reference not permitted');
    return schema.$ref.slice(2).split('/').reduce(
      (node: Json, key: string) => node[key.replace(/~1/g, '/').replace(/~0/g, '~')], this.spec,
    );
  }

  validate(value: Json, schema: Json, path = '$'): void {
    if (!schema) return;
    if (schema.$ref) return this.validate(value, this.dereference(schema), path);
    if (schema.nullable && value === null) return;
    for (const item of schema.allOf ?? []) this.validate(value, item, path);
    for (const keyword of ['oneOf', 'anyOf']) {
      if (schema[keyword]) {
        const count = schema[keyword].filter((s: Json) => {
          try { this.validate(value, s, path); return true; } catch { return false; }
        }).length;
        if (keyword === 'oneOf' ? count !== 1 : count < 1) throw new ContractError(`${path}: ${keyword}`);
      }
    }
    if (schema.enum && !schema.enum.some((entry: Json) => entry === value)) throw new ContractError(`${path}: invalid enum`);
    const fail = (reason: string): never => { throw new ContractError(`${path}: ${reason}`); };
    switch (schema.type) {
      case 'object':
        if (typeof value !== 'object' || value === null || Array.isArray(value)) fail('expected object');
        for (const key of schema.required ?? []) if (!Object.hasOwn(value, key)) fail(`missing ${key}`);
        for (const [key, child] of Object.entries(value)) {
          if (['__proto__', 'constructor', 'prototype'].includes(key)) fail('unsafe property');
          if (schema.properties?.[key]) this.validate(child, schema.properties[key], `${path}.${key}`);
          else if (schema.additionalProperties === false) fail(`unknown ${key}`);
          else if (schema.additionalProperties && typeof schema.additionalProperties === 'object') {
            this.validate(child, schema.additionalProperties, `${path}.${key}`);
          }
        }
        break;
      case 'array':
        if (!Array.isArray(value)) fail('expected array');
        for (let i = 0; i < value.length; i++) this.validate(value[i], schema.items, `${path}[${i}]`);
        break;
      case 'string':
        if (typeof value !== 'string') fail('expected string');
        if (schema.minLength !== undefined && BigInt([...value].length) < exactInteger(schema.minLength)) fail('too short');
        if (schema.maxLength !== undefined && BigInt([...value].length) > exactInteger(schema.maxLength)) fail('too long');
        if (schema.pattern && !new RegExp(schema.pattern).test(value)) fail('pattern mismatch');
        if (schema.format === 'date-time' && (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value) || !Number.isFinite(Date.parse(value)))) fail('invalid timestamp');
        if (schema.format === 'uri') {
          try { new URL(value); } catch { fail('invalid URI'); }
        }
        break;
      case 'integer': {
        let n: bigint;
        try { n = exactInteger(value); } catch { return fail('expected exact integer'); }
        if (schema.format === 'int64' && (n < MIN_INT64 || n > MAX_INT64)) fail('int64 overflow');
        break;
      }
      case 'number':
        if (typeof value !== 'number' && typeof value !== 'bigint') fail('expected number');
        break;
      case 'boolean':
        if (typeof value !== 'boolean') fail('expected boolean');
        break;
    }
  }

  operation(method: string, path: string): Json {
    return this.spec.paths[path]?.[method.toLowerCase()];
  }

  request(operation: Json, value: Json): void {
    this.validate(value, operation.requestBody?.content?.['application/json']?.schema);
  }

  response(operation: Json, status: number, value?: Json, grant?: string): void {
    const response = operation.responses?.[String(status)];
    if (!response && status >= 500 && status <= 599 && value === undefined) return;
    if (!response) throw new ContractError('Response status not declared by operation');
    let schema = this.dereference(response).content?.['application/json']?.schema;
    if (grant && status === 200 && operation.operationId === 'createToken') {
      const name = grant === 'authorization_code' ? 'RefreshTokenResponse'
        : grant === 'refresh_token' ? 'AccessTokenResponse' : 'WalletGrantRefreshTokenResponse';
      schema = this.spec.components.schemas[name];
    }
    if (schema) this.validate(value, schema);
    else if (value !== undefined) throw new ContractError('Unexpected response body');
  }

  callback(name: string, id: string, payload: Json): { path: string; scope: string; idempotent: boolean } {
    const definition = this.spec.components.callbacks[name];
    if (!definition) throw new ContractError('Unknown callback');
    const url = Object.keys(definition)[0]!;
    const op = definition[url].post;
    this.validate(payload, op.requestBody.content['application/json'].schema);
    return {
      path: new URL(url.replace('{$request.body#/id}', encodeURIComponent(id))).pathname,
      scope: name === 'payoutCallback' ? 'payments.payouts' : 'payments.deposits',
      idempotent: name !== 'updateRefund',
    };
  }
}
