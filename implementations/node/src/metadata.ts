import type { Json } from './codec.js';
import { ContractError } from './contract.js';
import { exactInteger } from './codec.js';

/** Synthetic validation only, not merchant authentication, FX or KYC. Never persist this input. */
export function validatePaymentData(input: Json, amount?: bigint): void {
  for (const entry of input.payment_data ?? []) {
    if (entry.type === 'meal_voucher_payment_data' &&
        (entry.meal_voucher_merchant_id !== 'demo-merchant' ||
         (entry.meal_voucher_merchant_secret !== undefined && entry.meal_voucher_merchant_secret !== 'demo-merchant-secret'))) {
      throw new ContractError('Unknown synthetic merchant');
    }
    if (entry.type === 'point_conversion_payment_data') {
      const points = exactInteger(entry.points_to_charge_e5);
      if (points < 0n || (amount !== undefined && points !== amount)) throw new ContractError('Synthetic 1:1 points mismatch');
    }
  }
}
