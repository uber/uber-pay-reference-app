import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { existsSync } from 'node:fs';
function rootDir(): string {
  let p = resolve('.');
  while (!existsSync(resolve(p, 'contracts/reference-features.json'))) {
    const up = dirname(p);
    if (up === p) throw new Error('Missing reference feature profile');
    p = up;
  }
  return p;
}
import { ContractError } from './contract.js';
import { decode, type Json } from './codec.js';

export class ReferenceFeatures {
  readonly data: Json;
  readonly aliases: Json;
  constructor(
    root = rootDir(),
    file = 'contracts/reference-features.json',
    aliases = process.env.FUNDING_METHODS_FILE,
  ) {
    this.data = decode(readFileSync(resolve(root, file)));
    this.aliases = aliases ? decode(readFileSync(resolve(root, aliases))) : {};
    for (const [code, alias] of Object.entries(this.aliases) as [string, Json][]) {
      if (
        Object.keys(alias).some((k) => !['method', 'countries', 'currencies', 'callback_mode'].includes(k)) ||
        (alias.callback_mode !== undefined && !['charge', 'deposit'].includes(alias.callback_mode))
      )
        throw new Error('Invalid funding callback policy');
      if (
        alias.callback_mode === 'deposit' &&
        !['webview', 'qr', 'pending', 'pending_reject'].includes(this.data.methods[alias.method]?.behavior)
      )
        throw new Error('Deposit callback policy requires an asynchronous funding profile');
      if (
        !code ||
        !Object.hasOwn(this.data.methods, alias.method) ||
        !alias.countries?.length ||
        !alias.currencies?.length ||
        !alias.countries.every((s: string) => /^[A-Z]{2}$/.test(s)) ||
        !alias.currencies.every((s: string) => /^[A-Z]{3}$/.test(s))
      ) {
        throw new Error('Invalid funding alias configuration');
      }
    }
  }
  method(code: string, country?: string, currency?: string): Json {
    const alias = Object.hasOwn(this.aliases, code) ? this.aliases[code] : undefined;
    const key = alias?.method ?? code;
    if (!Object.hasOwn(this.data.methods, key)) throw new ContractError('Unknown funding method');
    const result = { ...this.data.methods[key], ...alias, code, method: key };
    if (
      (country && !result.countries.includes(country)) ||
      (currency && !result.currencies.includes(currency))
    )
      throw new ContractError('Funding country/currency mismatch');
    return result;
  }
  locale(value = ''): string {
    return /^pt(?:-|\b)/i.test(value.trim()) ? 'pt-BR' : 'en-US';
  }
  text(locale: string, key: string): string {
    return this.data.translations[this.locale(locale)][key] ?? key;
  }
}
export const escapeHtml = (value: string): string =>
  value.replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]!,
  );
