package gateway

import "errors"

// Only the local fixture policy. Do not log or persist merchant secrets or KYC input.
func validatePaymentData(input Object, amount int64) error {
	items, _ := input["payment_data"].([]any)
	for _, item := range items {
		d := object(item)
		switch str(d, "type") {
		case "meal_voucher_payment_data":
			if str(d, "meal_voucher_merchant_id") != "demo-merchant" {
				return errors.New("unknown synthetic merchant")
			}
			if secret, ok := d["meal_voucher_merchant_secret"]; ok && secret != "demo-merchant-secret" {
				return errors.New("invalid synthetic merchant secret")
			}
		case "point_conversion_payment_data":
			points, e := number(d["points_to_charge_e5"])
			if e != nil || points < 0 || amount > 0 && points != amount {
				return errors.New("synthetic 1:1 points mismatch")
			}
		}
	}
	return nil
}
