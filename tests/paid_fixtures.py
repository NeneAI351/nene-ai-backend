import uuid
from decimal import Decimal
import database
import payments
from account_agreement_store import initialize_account_agreement_schema,record_agreement
from wallet_store import initialize_wallet_schema

async def fund_paid(user_id, amount="500", mode="live"):
    await initialize_wallet_schema()
    await payments.initialize_payment_schema()
    await initialize_account_agreement_schema()
    await record_agreement(user_id)
    amount=Decimal(amount)
    if amount <= 0:
        return
    reference="nene_"+uuid.uuid4().hex
    await database.execute("""INSERT INTO payment_orders
      (id,user_id,mode,reference,package_id,amount_kobo,credits,status)
      VALUES (%s,%s,%s,%s,'test-fixture',100,%s,'pending')""",
      (str(uuid.uuid4()),user_id,mode,reference,amount))
    await payments._fulfill_verified_transaction({"reference":reference,"id":str(uuid.uuid4().int),
      "domain":mode,"status":"success","currency":"NGN","amount":100})
