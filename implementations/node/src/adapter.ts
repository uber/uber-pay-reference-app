import { ReferenceFeatures } from './features.js';
import { ContractError } from './contract.js';
import type { Json } from './codec.js';
import { validatePaymentData } from './metadata.js';
import { checkedAmount } from './codec.js';

const defaults = new ReferenceFeatures();
export const FUNDING_METHODS = new Set([
  ...Object.keys(defaults.data.methods),
  ...Object.keys(defaults.aliases),
]);

export interface CaptureDecision {
  state: 'CONFIRMED' | 'REJECTED';
  detail?: string;
}

export interface ProviderDecision {
  state: 'CONFIRMED' | 'REJECTED' | 'PENDING';
  detail?: string;
  userAction?: 'web_view_user_input_action' | 'qr_code_input_action';
  finalState?: 'CONFIRMED' | 'REJECTED';
  finalDetail?: string;
}

/**
 * Synthetic, in-process adapter port. Methods execute under the demo database
 * transaction and MUST NOT make network calls or affect real funds. A real PSP
 * adapter requires a durable intent/reconciliation executor outside this lock,
 * not merely replacing this class or changing an environment variable.
 */
export interface PspAdapter {
  readonly mode: 'local';
  charge(command: Json, providerKey: string): ProviderDecision | Promise<ProviderDecision>;
  payout(command: Json, providerKey: string): ProviderDecision | Promise<ProviderDecision>;
  capture(command: Json, providerKey: string): CaptureDecision | Promise<CaptureDecision>;
  cancel(command: Json, providerKey: string): void | Promise<void>;
  refund(command: Json, providerKey: string): ProviderDecision | Promise<ProviderDecision>;
  getBalance(account: Json): Json;
  lookupOperation(providerKey: string): { state: 'UNKNOWN' };
}

export class DemoPspAdapter implements PspAdapter {
  readonly mode = 'local' as const;
  constructor(readonly features = new ReferenceFeatures()) {}
  private decide(command: Json, kind = 'charge'): ProviderDecision {
    const p = this.features.method(command.funding_method, command.country_iso2, command.amount?.currency);
    const allowed = kind === 'payout' ? this.features.data.payout_reasons : this.features.data.charge_reasons;
    if (p.detail && !allowed.includes(p.detail)) throw new ContractError('Reason not valid for operation');
    if (p.behavior === 'reject') return { state: 'REJECTED', detail: p.detail ?? 'INSUFFICIENT_BALANCE' };
    if (p.behavior === 'webview' || p.behavior === 'qr')
      return {
        state: 'PENDING',
        userAction: p.behavior === 'qr' ? 'qr_code_input_action' : 'web_view_user_input_action',
        finalState: 'CONFIRMED',
      };
    if (['pending', 'pending_reject'].includes(p.behavior))
      return {
        state: 'PENDING',
        finalState: p.behavior === 'pending_reject' ? 'REJECTED' : 'CONFIRMED',
        finalDetail: p.detail,
      };
    return { state: 'CONFIRMED' };
  }
  charge(command: Json, _providerKey: string): ProviderDecision {
    validatePaymentData(command, command.amount ? checkedAmount(command.amount.value) : undefined);
    return this.decide(command);
  }
  payout(command: Json, _providerKey: string): ProviderDecision {
    return this.decide(command, 'payout');
  }
  capture(command: Json, _providerKey: string): CaptureDecision {
    return ['capture_hard', 'capture_soft'].includes(this.features.method(command.funding_method).behavior)
      ? {
          state: 'REJECTED',
          detail:
            this.features.method(command.funding_method).behavior === 'capture_soft'
              ? 'BANK_SOFT_ERROR'
              : 'BANK_HARD_ERROR',
        }
      : { state: 'CONFIRMED' };
  }
  cancel(_command: Json, _providerKey: string): void {}
  refund(command: Json, _providerKey: string): ProviderDecision {
    const p = this.features.method(command.funding_method);
    if (p.behavior === 'refund_sync_reject') return { state: 'REJECTED' };
    return ['pending', 'pending_reject', 'refund_reversal', 'refund_reject'].includes(p.behavior)
      ? {
          state: 'PENDING',
          finalState: p.behavior === 'refund_reject' ? 'REJECTED' : 'CONFIRMED',
          finalDetail: 'MERCHANT_REJECTED',
        }
      : { state: 'CONFIRMED' };
  }
  getBalance(account: Json): Json {
    validatePaymentData(account);
    const result: Json = {
      available_balance: { value: account.available_e5, currency: account.currency },
      balance: { value: account.balance_e5, currency: account.currency },
    };
    if (account.itemized)
      result.itemized_balances = [
        {
          display_name: 'Meal voucher',
          available_balance: result.available_balance,
          balance: result.balance,
        },
      ];
    return result;
  }
  // Demo effects commit in the same DB transaction as gateway state, so an
  // ambiguous external effect cannot occur. Real adapters must supply lookup.
  lookupOperation(_providerKey: string): { state: 'UNKNOWN' } {
    return { state: 'UNKNOWN' };
  }
}
