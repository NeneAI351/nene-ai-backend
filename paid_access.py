"""Live-payment entitlement, checked under the caller's wallet row lock.

Bonus balances, client edits and test checkouts cannot fund real provider work.
All consumption/reservations (including legacy) count conservatively against live
purchases. No environment flag permits free provider calls.
"""
from decimal import Decimal

async def paid_available(conn, user_id: str, wallet: dict) -> Decimal:
    row = await (await conn.execute("""SELECT COALESCE(SUM(l.amount),0) AS purchased
        FROM credit_ledger l JOIN payment_orders p
          ON l.operation_key='payment:' || p.reference AND l.user_id=p.user_id
        WHERE l.user_id=%s AND l.entry_type='purchase' AND l.amount=p.credits
          AND p.mode='live' AND p.status='success'
          AND p.provider_transaction_id IS NOT NULL AND p.paid_at IS NOT NULL""",
        (user_id,))).fetchone()
    remaining = Decimal(row['purchased']) - Decimal(wallet['lifetime_consumed']) - Decimal(wallet['reserved_credits'])
    return max(Decimal('0'), min(Decimal(wallet['available_credits']), remaining))
