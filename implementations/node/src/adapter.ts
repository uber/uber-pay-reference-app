import { ContractError } from './contract.js';
import type { Json } from './codec.js';
import { validatePaymentData } from './metadata.js';
import { checkedAmount } from './codec.js';

export const FUNDING_METHODS = new Set([
  'DEMO_SYNC', 'DEMO_CAPTURE_HARD', 'DEMO_CAPTURE_SOFT', 'DEMO_REJECT', 'DEMO_PENDING', 'DEMO_PENDING_REJECT', 'DEMO_WEBVIEW', 'DEMO_QR',
]);

export interface CaptureDecision { state: 'CONFIRMED' | 'REJECTED'; detail?: string }

export interface ProviderDecision {
  state: 'CONFIRMED' | 'REJECTED' | 'PENDING';
  detail?: string;
  userAction?: 'web_view_user_input_action' | 'qr_code_input_action';
  finalState?: 'CONFIRMED' | 'REJECTED';
}

/**
 * Synthetic, in-process adapter port. Methods execute under the demo database
 * transaction and MUST NOT make network calls or affect real funds. A real PSP
 * adapter requires a durable intent/reconciliation executor outside this lock,
 * not merely replacing this class or changing an environment variable.
 */
export interface PspAdapter {
  readonly mode: 'local';
  charge(command: Json, providerKey: string): ProviderDecision;
  payout(command: Json, providerKey: string): ProviderDecision;
  capture(command: Json, providerKey: string): CaptureDecision;
  cancel(command: Json, providerKey: string): void;
  refund(command: Json, providerKey: string): ProviderDecision;
  getBalance(account: Json): Json;
  lookupOperation(providerKey: string): { state: 'UNKNOWN' };
}

export class DemoPspAdapter implements PspAdapter {
  readonly mode = 'local' as const;
  private decide(command: Json): ProviderDecision {
    const method = command.funding_method;
    if (!FUNDING_METHODS.has(method)) throw new ContractError('Unsupported demo funding method');
    if (method === 'DEMO_REJECT') return { state: 'REJECTED', detail: 'INSUFFICIENT_BALANCE' };
    if (method === 'DEMO_WEBVIEW') return { state: 'PENDING', userAction: 'web_view_user_input_action', finalState: 'CONFIRMED' };
    if (method === 'DEMO_QR') return { state: 'PENDING', userAction: 'qr_code_input_action', finalState: 'CONFIRMED' };
    if (method.startsWith('DEMO_PENDING')) {
      return { state: 'PENDING', finalState: method === 'DEMO_PENDING_REJECT' ? 'REJECTED' : 'CONFIRMED' };
    }
    return { state: 'CONFIRMED' };
  }
  charge(command: Json, _providerKey: string): ProviderDecision { validatePaymentData(command,command.amount ? checkedAmount(command.amount.value) : undefined); return this.decide(command); }
  payout(command: Json, _providerKey: string): ProviderDecision { return this.decide(command); }
  capture(command: Json, _providerKey: string): CaptureDecision {
    return command.funding_method?.startsWith('DEMO_CAPTURE_')
      ? { state: 'REJECTED', detail: command.funding_method === 'DEMO_CAPTURE_SOFT' ? 'BANK_SOFT_ERROR' : 'BANK_HARD_ERROR' }
      : { state: 'CONFIRMED' };
  }
  cancel(_command: Json, _providerKey: string): void {}
  refund(command: Json, _providerKey: string): ProviderDecision { return this.decide(command); }
  getBalance(account: Json): Json {
    validatePaymentData(account);
    return {
      available_balance: { value: account.available_e5, currency: account.currency },
      balance: { value: account.balance_e5, currency: account.currency },
    };
  }
  // Demo effects commit in the same DB transaction as gateway state, so an
  // ambiguous external effect cannot occur. Real adapters must supply lookup.
  lookupOperation(_providerKey: string): { state: 'UNKNOWN' } { return { state: 'UNKNOWN' }; }
}
