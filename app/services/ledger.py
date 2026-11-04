from decimal import Decimal
from datetime import datetime
from uuid import uuid4
from sqlalchemy.orm import Session
from sqlalchemy import select
from fastapi import HTTPException
from app.models import Account, Transaction, LedgerEntry, Wallet
SYSTEM={'Bank Settlement':'Bank Settlement','Cashback Pool':'Cashback Pool','Fees':'Fees','Withdrawal Clearing':'Withdrawal Clearing','Hold':'Hold'}
def ensure_accounts(db):
    for name,typ in SYSTEM.items():
        if not db.query(Account).filter_by(name=name).first(): db.add(Account(name=name,account_type=typ))
    db.flush()
def wallet_account(db,wallet_id):
    a=db.query(Account).filter_by(wallet_id=wallet_id).first()
    if not a:
        a=Account(name=f'Wallet-{wallet_id}',account_type='User Wallet',wallet_id=wallet_id); db.add(a); db.flush()
    return a
def system_account(db,name):
    a=db.query(Account).filter_by(name=name).first()
    if not a: a=Account(name=name,account_type=name); db.add(a); db.flush()
    return a
def post(db, user_id, typ, amount, postings, status='Success', description=None, flagged=False):
    amount=Decimal(amount).quantize(Decimal('0.01'))
    if sum((Decimal(x[1]) for x in postings if x[2]=='Debit'),Decimal('0')) != sum((Decimal(x[1]) for x in postings if x[2]=='Credit'),Decimal('0')): raise ValueError('Unbalanced ledger posting')
    tx=Transaction(transaction_ref='TXN-'+datetime.utcnow().strftime('%Y%m%d')+'-'+uuid4().hex[:6].upper(),type=typ,amount=amount,status=status,initiated_by=user_id,description=description,is_flagged=flagged); db.add(tx); db.flush()
    for account,amt,side in postings:
        bal=None
        if account.wallet_id:
            wallet=db.query(Wallet).filter_by(id=account.wallet_id).with_for_update().first()
            delta=Decimal(amt) if side=='Credit' else -Decimal(amt)
            if wallet.balance+delta<0: raise HTTPException(400,'Insufficient wallet balance')
            wallet.balance+=delta; bal=wallet.balance
        db.add(LedgerEntry(transaction_id=tx.id,account_id=account.id,entry_type=side,amount=amt,balance_after=bal))
    db.flush(); return tx
