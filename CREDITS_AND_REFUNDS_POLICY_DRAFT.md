# NENE AI Credits, Billing and Refund Policy (implementation draft)

## Credit issuance and refunds

- Credit purchases and subscription credit allocations must be clearly described before checkout, including amount, expiry (if any), recurring billing, and when credits are issued.
- Once purchased or subscription credits have been successfully issued to the subscriber's NENE AI wallet, they are non-refundable and cannot be exchanged for cash, except where a refund, reversal, or other remedy is required by applicable law or is needed to correct a duplicate/unauthorized charge or a NENE AI billing error.
- Cancelling a subscription stops future renewals; it does not by itself reverse credits already issued. The remaining subscription period and any unused credits must be described clearly at purchase.
- Credits consumed by a successfully accepted generation are not refundable merely because the subscriber changes their mind or dislikes the creative result. Provider failure, a charge without corresponding credit issuance, duplicate billing, or a generation that NENE AI confirms was not processed must be handled under a documented service-failure correction process; do not represent this policy as eliminating mandatory consumer rights.
- Do not advertise “no refunds under any circumstances.” Applicable consumer-protection law and payment-provider rules may override a contract term.

## Engineering invariants

1. Only verified payment-provider webhooks or trusted administrative workflows may issue purchased credits. A client request must never grant credits.
2. A wallet credit grant and its ledger record must be atomic and idempotent by provider event / transaction ID.
3. The API must not offer a user-facing refund or credit-grant endpoint.
4. Generation billing is server-side: reserve credits before provider submission, capture only according to the published billing rules, and release unused reservations on confirmed pre-processing/provider failure. Unknown provider outcomes are reconciled before any retry or settlement.
5. Keep generation disabled in production until generation ownership, durable recovery, and these wallet invariants are integrated and tested.
6. Legal review is required for the countries where NENE AI sells subscriptions, including Nigeria and any other target markets, before publishing this policy.

This file is an engineering/product draft, not legal advice. Checkout screens and subscription terms must present the policy in plain language before payment.
