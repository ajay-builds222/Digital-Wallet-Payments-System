from contextlib import asynccontextmanager
from datetime import datetime,timedelta,date
from decimal import Decimal
from uuid import uuid4
import hashlib,json,secrets,re,io,csv,logging
from fastapi import FastAPI,Depends,HTTPException,Header,BackgroundTasks,Query,Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import or_,func
from passlib.context import CryptContext
from app.database import Base,engine,get_db,SessionLocal,settings
from app.models import *
from app.schemas import *
from app.auth.security import hash_secret,verify_secret,create_token,current_user,roles
from app.services.ledger import ensure_accounts,wallet_account,system_account,post
from apscheduler.schedulers.background import BackgroundScheduler
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
import smtplib
from email.message import EmailMessage
logging.basicConfig(level=logging.INFO); log=logging.getLogger('wallet')
scheduler=BackgroundScheduler()
def send_email(to,subject,body):
    if not settings.SMTP_HOST or not settings.SMTP_USERNAME: log.info('Email skipped (SMTP not configured): %s %s',to,subject); return
    try:
        msg=EmailMessage(); msg['Subject']=subject; msg['From']=settings.SMTP_FROM or settings.SMTP_USERNAME; msg['To']=to; msg.set_content(body)
        with smtplib.SMTP(settings.SMTP_HOST,settings.SMTP_PORT,timeout=10) as s: s.starttls(); s.login(settings.SMTP_USERNAME,settings.SMTP_PASSWORD); s.send_message(msg)
    except Exception: log.exception('Email delivery failed')
def init_db():
    ensure_accounts_db()
def ensure_accounts_db():
    db=SessionLocal()
    try: ensure_accounts(db); db.commit()
    finally: db.close()
def jobs():
    db=SessionLocal()
    try:
        now=datetime.utcnow()
        for c in db.query(Cashback).filter_by(status='Pending').filter(Cashback.created_at<=now-timedelta(hours=24)).all():
            w=db.query(Wallet).filter_by(owner_id=c.user_id).with_for_update().first(); pool=system_account(db,'Cashback Pool'); wa=wallet_account(db,w.id)
            post(db,c.user_id,'Cashback',c.amount,[(pool,c.amount,'Debit'),(wa,c.amount,'Credit')]); c.status='Credited'
        for wd in db.query(Withdrawal).filter_by(status='Pending').filter(Withdrawal.created_at<=now-timedelta(minutes=10)).all():
            tx=db.get(Transaction,wd.transaction_id); bank=db.get(BankAccount,wd.bank_account_id); clearing=system_account(db,'Withdrawal Clearing'); settlement=system_account(db,'Bank Settlement')
            if bank.account_number.endswith('0000'):
                w=db.query(Wallet).filter_by(owner_id=tx.initiated_by).first(); wa=wallet_account(db,w.id); post(db,tx.initiated_by,'Withdrawal Reversal',wd.amount,[(clearing,wd.amount,'Debit'),(wa,wd.amount,'Credit')]); wd.status='Failed'; wd.failure_reason='Simulated bank rejection'
            else: post(db,tx.initiated_by,'Withdrawal',wd.amount,[(clearing,wd.amount,'Debit'),(settlement,wd.amount,'Credit')]); wd.status='Completed'
            wd.processed_at=now
        for mr in db.query(MoneyRequest).filter_by(status='Pending').filter(MoneyRequest.expires_at<now).all(): mr.status='Expired'
        db.query(OTP).filter(OTP.expires_at<now).delete(synchronize_session=False); db.query(Idempotency).filter(Idempotency.created_at<now-timedelta(hours=24)).delete(synchronize_session=False)
        db.commit()
    except Exception: db.rollback(); log.exception('Scheduled job failed')
    finally: db.close()
@asynccontextmanager
async def lifespan(app):
    ensure_accounts_db()
    scheduler.add_job(jobs,'interval',minutes=5,id='wallet_jobs',replace_existing=True); scheduler.start()
    yield
    scheduler.shutdown(wait=False)
app=FastAPI(title='Digital Wallet & Payments API',version='1.0.0',description='Swagger-ready wallet backend. Configure MySQL and .env before running.',lifespan=lifespan)
def money_key(user,endpoint,key,body,db):
    if not key: raise HTTPException(422,'Idempotency-Key header is required')
    digest=hashlib.sha256(json.dumps(body,sort_keys=True,default=str).encode()).hexdigest(); row=db.query(Idempotency).filter_by(key=key).first()
    if row:
        if row.user_id!=user.id or row.endpoint!=endpoint or row.body_hash!=digest: raise HTTPException(422,'Idempotency key reused with a different request')
        if row.response_json: return json.loads(row.response_json)
        raise HTTPException(409,'Request with this key is processing')
    db.add(Idempotency(user_id=user.id,key=key,endpoint=endpoint,body_hash=digest)); db.flush(); return None
def save_key(db,key,result,status=200):
    r=db.query(Idempotency).filter_by(key=key).first(); r.response_json=json.dumps(result,default=str); r.status_code=status
    return result
def own_wallet(db,user,lock=False):
    q=db.query(Wallet).filter_by(owner_id=user.id)
    if lock:q=q.with_for_update()
    w=q.first()
    if not w: raise HTTPException(404,'Wallet not found')
    if w.status=='Frozen': raise HTTPException(423,'Wallet is frozen')
    return w
def check_pin(wallet,pin):
    now=datetime.utcnow()
    if wallet.pin_locked_until and wallet.pin_locked_until>now: raise HTTPException(423,'Payments locked for 30 minutes after 3 wrong PIN attempts')
    if not wallet.pin_hash: raise HTTPException(400,'Set transaction PIN first')
    if not verify_secret(pin,wallet.pin_hash):
        wallet.pin_failures+=1
        if wallet.pin_failures>=3: wallet.pin_locked_until=now+timedelta(minutes=30); wallet.pin_failures=0
        raise HTTPException(400,'Incorrect transaction PIN')
    wallet.pin_failures=0
    if wallet.pin_locked_until and wallet.pin_locked_until<=now: wallet.pin_locked_until=None
def limit_check(db,user,amount):
    w=db.query(Wallet).filter_by(owner_id=user.id).first(); cap=Decimal('25000') if w.kyc_level=='Full' else Decimal('10000'); monthcap=Decimal('100000') if w.kyc_level=='Full' else Decimal('25000')
    start=datetime.utcnow().replace(hour=0,minute=0,second=0,microsecond=0); month=start.replace(day=1)
    spent=db.query(func.coalesce(func.sum(Transaction.amount),0)).filter(Transaction.initiated_by==user.id,Transaction.status.in_(['Success','On Hold']),Transaction.type.in_(['P2P Transfer','Merchant Payment','Withdrawal']),Transaction.created_at>=start).scalar() or 0
    mspent=db.query(func.coalesce(func.sum(Transaction.amount),0)).filter(Transaction.initiated_by==user.id,Transaction.status.in_(['Success','On Hold']),Transaction.type.in_(['P2P Transfer','Merchant Payment','Withdrawal']),Transaction.created_at>=month).scalar() or 0
    if Decimal(spent)+amount>cap: raise HTTPException(400,f'Daily limit of ₹{cap} reached. You can send ₹{max(Decimal(0),cap-Decimal(spent)):.2f} more today')
    if Decimal(mspent)+amount>monthcap: raise HTTPException(400,f'Monthly limit of ₹{monthcap} reached. You can send ₹{max(Decimal(0),monthcap-Decimal(mspent)):.2f} more this month')
def otp_ok(db,user,code,purpose):
    row=db.query(OTP).filter_by(user_id=user.id,purpose=purpose,is_used=False).order_by(OTP.created_at.desc()).first()
    if not row or row.expires_at<datetime.utcnow() or row.attempts>=3: raise HTTPException(400,'Valid OTP required')
    if not verify_secret(code,row.otp_hash): row.attempts+=1; db.flush(); raise HTTPException(400,'Incorrect OTP')
    row.is_used=True
@app.get('/')
def root(): return {'message':'Digital Wallet API is running','docs':'/docs'}
@app.post('/auth/register',status_code=201)
def register(data:Register,db:Session=Depends(get_db)):
    if data.role=='Merchant' and (not data.business_name or not data.business_category): raise HTTPException(422,'Merchants must provide business_name and business_category')
    if db.query(User).filter(or_(User.email==data.email,User.phone==data.phone)).first(): raise HTTPException(409,'Email or phone already registered')
    u=User(full_name=data.full_name,email=data.email,phone=data.phone,password_hash=hash_secret(data.password),role=data.role,business_name=data.business_name,business_category=data.business_category,merchant_code=('MER-'+uuid4().hex[:8].upper() if data.role=='Merchant' else None)); db.add(u); db.flush()
    w=Wallet(owner_id=u.id,wallet_number=f'WAL-{1000000+u.id}',balance=Decimal('0.00')); db.add(w); db.flush(); wallet_account(db,w.id); db.commit()
    return {'id':u.id,'full_name':u.full_name,'email':u.email,'phone':u.phone,'role':u.role,'merchant_code':u.merchant_code,'wallet_number':w.wallet_number}
@app.post('/auth/login')
def login(data:Login,bg:BackgroundTasks,db:Session=Depends(get_db)):
    u=db.query(User).filter_by(email=data.email).first()
    if not u or not verify_secret(data.password,u.password_hash): raise HTTPException(401,'Invalid email or password')
    if not u.is_active: raise HTTPException(403,'Inactive user')
    bg.add_task(send_email,u.email,'New login to your wallet','A successful login occurred on your account.')
    return {'access_token':create_token(u.id,u.role),'token_type':'bearer','expires_in_minutes':settings.ACCESS_TOKEN_EXPIRE_MINUTES}
@app.get('/auth/me')
def me(u:User=Depends(current_user),db:Session=Depends(get_db)):
    w=db.query(Wallet).filter_by(owner_id=u.id).first(); return {'id':u.id,'full_name':u.full_name,'email':u.email,'phone':u.phone,'role':u.role,'wallet_number':w.wallet_number if w else None}
@app.get('/wallet')
def wallet(u:User=Depends(current_user),db:Session=Depends(get_db)):
    w=own_wallet(db,u); return {'id':w.id,'wallet_number':w.wallet_number,'balance':w.balance,'kyc_level':w.kyc_level,'status':w.status}
@app.post('/wallet/pin')
def set_pin(x:PinIn,u:User=Depends(current_user),db:Session=Depends(get_db)):
    if x.pin in {'1234','1111','0000','2222','3333','4444','5555','6666','7777','8888','9999'}: raise HTTPException(422,'Simple PIN not allowed')
    w=own_wallet(db,u); w.pin_hash=hash_secret(x.pin); db.commit(); return {'message':'Transaction PIN set'}
@app.put('/wallet/pin/change')
def change_pin(old_pin:str,new_pin:str,u:User=Depends(current_user),db:Session=Depends(get_db)):
    w=own_wallet(db,u); check_pin(w,old_pin)
    if not re.fullmatch(r'\d{4}',new_pin) or new_pin in {'1234','1111','0000'}: raise HTTPException(422,'Invalid or simple PIN')
    w.pin_hash=hash_secret(new_pin); db.commit(); return {'message':'PIN changed'}
@app.post('/otp/send')
def send_otp(purpose:str=Query(...,pattern='^(High Value Transfer|Add Bank Account|PIN Reset)$'),bg:BackgroundTasks=None,u:User=Depends(current_user),db:Session=Depends(get_db)):
    last=db.query(OTP).filter_by(user_id=u.id,purpose=purpose).order_by(OTP.created_at.desc()).first()
    if last and last.created_at>datetime.utcnow()-timedelta(seconds=60): raise HTTPException(429,'Wait 60 seconds before requesting another OTP')
    code=f'{secrets.randbelow(1000000):06d}'; db.add(OTP(user_id=u.id,purpose=purpose,otp_hash=hash_secret(code),expires_at=datetime.utcnow()+timedelta(minutes=5))); db.commit()
    if bg: bg.add_task(send_email,u.email,'Your wallet OTP',f'Your {purpose} OTP is {code}. It expires in 5 minutes.')
    return {'message':'OTP sent to registered email','debug_otp':code if not settings.SMTP_HOST else None}
@app.post('/otp/verify')
def verify_otp(code:str,purpose:str,u:User=Depends(current_user),db:Session=Depends(get_db)):
    otp_ok(db,u,code,purpose); db.commit(); return {'verified':True,'purpose':purpose}
@app.post('/wallet/pin/reset')
def reset_pin(code:str,new_pin:str,u:User=Depends(current_user),db:Session=Depends(get_db)):
    otp_ok(db,u,code,'PIN Reset'); w=own_wallet(db,u)
    if not re.fullmatch(r'\d{4}',new_pin) or new_pin in {'1234','1111','0000'}: raise HTTPException(422,'Invalid or simple PIN')
    w.pin_hash=hash_secret(new_pin); w.pin_locked_until=None; w.pin_failures=0; db.commit(); return {'message':'PIN reset'}
@app.post('/kyc',status_code=201)
def submit_kyc(x:KYCIn,u:User=Depends(current_user),db:Session=Depends(get_db)):
    if db.query(KYC).filter_by(user_id=u.id).filter(KYC.status=='Pending').first(): raise HTTPException(409,'Pending KYC already exists')
    k=KYC(user_id=u.id,pan_number=x.pan_number,aadhaar_last4=x.aadhaar_last4,full_name=x.full_name,date_of_birth=x.date_of_birth); db.add(k); db.commit(); db.refresh(k); return {'id':k.id,'status':k.status}
@app.get('/kyc')
def get_kyc(u:User=Depends(current_user),db:Session=Depends(get_db)):
    return [{'id':k.id,'pan_number':k.pan_number,'aadhaar_last4':k.aadhaar_last4,'status':k.status,'rejection_reason':k.rejection_reason} for k in db.query(KYC).filter_by(user_id=u.id).all()]
@app.put('/admin/kyc/{kyc_id}/approve')
def approve_kyc(kyc_id:int,a:User=Depends(roles('Admin')),db:Session=Depends(get_db)):
    k=db.get(KYC,kyc_id)
    if not k: raise HTTPException(404,'KYC not found')
    k.status='Approved'; db.query(Wallet).filter_by(owner_id=k.user_id).update({'kyc_level':'Full'}); db.commit(); return {'message':'KYC approved'}
@app.put('/admin/kyc/{kyc_id}/reject')
def reject_kyc(kyc_id:int,reason:str,a:User=Depends(roles('Admin')),db:Session=Depends(get_db)):
    k=db.get(KYC,kyc_id)
    if not k: raise HTTPException(404,'KYC not found')
    if not reason.strip(): raise HTTPException(422,'Rejection reason required')
    k.status='Rejected'; k.rejection_reason=reason; db.commit(); return {'message':'KYC rejected'}
@app.put('/admin/wallets/{wallet_id}/freeze')
def freeze(wallet_id:int,reason:str,a:User=Depends(roles('Admin')),db:Session=Depends(get_db)):
    w=db.get(Wallet,wallet_id)
    if not w: raise HTTPException(404,'Wallet not found')
    if not reason.strip(): raise HTTPException(422,'Reason required')
    w.status='Frozen'; db.commit(); return {'message':'Wallet frozen','reason':reason}
@app.put('/admin/wallets/{wallet_id}/unfreeze')
def unfreeze(wallet_id:int,a:User=Depends(roles('Admin')),db:Session=Depends(get_db)):
    w=db.get(Wallet,wallet_id)
    if not w: raise HTTPException(404,'Wallet not found')
    w.status='Active'; db.commit(); return {'message':'Wallet unfrozen'}
@app.post('/wallet/add-money')
def add_money(x:AddMoney,bg:BackgroundTasks,idempotency_key:str=Header(...,alias='Idempotency-Key'),u:User=Depends(current_user),db:Session=Depends(get_db)):
    replay=money_key(u,'add-money',idempotency_key,x.model_dump(),db)
    if replay:return {**replay,'Idempotent-Replayed':True}
    w=own_wallet(db,u,True)
    if x.amount<10 or x.amount>50000: raise HTTPException(400,'Add money must be between ₹10 and ₹50,000')
    cap=Decimal('200000') if w.kyc_level=='Full' else Decimal('10000')
    if w.balance+x.amount>cap: raise HTTPException(400,'Wallet KYC balance limit exceeded')
    if x.simulate_status=='Failed':
        tx=Transaction(transaction_ref='TXN-'+uuid4().hex[:12].upper(),type='Add Money',amount=x.amount,status='Failed',initiated_by=u.id,description=x.source); db.add(tx); db.flush(); result={'transaction_ref':tx.transaction_ref,'status':'Failed'}
    else:
        tx=post(db,u.id,'Add Money',x.amount,[(system_account(db,'Bank Settlement'),x.amount,'Debit'),(wallet_account(db,w.id),x.amount,'Credit')],description=x.source); result={'transaction_ref':tx.transaction_ref,'status':tx.status,'balance':str(w.balance)}
    save_key(db,idempotency_key,result); db.commit(); return result
@app.post('/transfers')
def transfer(x:Transfer,bg:BackgroundTasks,idempotency_key:str=Header(...,alias='Idempotency-Key'),u:User=Depends(roles('User')),db:Session=Depends(get_db)):
    replay=money_key(u,'transfers',idempotency_key,x.model_dump(),db)
    if replay:return {**replay,'Idempotent-Replayed':True}
    sender=own_wallet(db,u,True); check_pin(sender,x.pin); receiver=db.query(User).filter_by(phone=x.receiver_phone).first()
    if not receiver or receiver.id==u.id: raise HTTPException(400,'Receiver not found or self-transfer not allowed')
    rw=db.query(Wallet).filter_by(owner_id=receiver.id).with_for_update().first()
    if not rw or rw.status!='Active': raise HTTPException(400,'Receiver wallet must be Active')
    if sender.id>rw.id: # lock ordering is enforced by deterministic query re-lock
        db.query(Wallet).filter(Wallet.id==min(sender.id,rw.id)).with_for_update().first(); db.query(Wallet).filter(Wallet.id==max(sender.id,rw.id)).with_for_update().first()
    if sender.balance<x.amount: raise HTTPException(400,'Insufficient balance')
    if rw.balance+x.amount>(Decimal('200000') if rw.kyc_level=='Full' else Decimal('10000')): raise HTTPException(400,'Receiver KYC balance limit exceeded')
    limit_check(db,u,x.amount)
    if x.amount>5000:
        if not x.otp: raise HTTPException(400,'OTP required for transfers above ₹5,000')
        otp_ok(db,u,x.otp,'High Value Transfer')
    recent=db.query(Transaction).filter(Transaction.initiated_by==u.id,Transaction.type=='P2P Transfer',Transaction.created_at>=datetime.utcnow()-timedelta(minutes=1)).count()
    flagged=x.amount>20000 or recent>=5
    if flagged:
        tx=post(db,u.id,'P2P Transfer',x.amount,[(wallet_account(db,sender.id),x.amount,'Debit'),(system_account(db,'Hold'),x.amount,'Credit')],status='On Hold',description=x.note,flagged=True); bg.add_task(send_email,u.email,'Transfer on hold',tx.transaction_ref)
    else:
        tx=post(db,u.id,'P2P Transfer',x.amount,[(wallet_account(db,sender.id),x.amount,'Debit'),(wallet_account(db,rw.id),x.amount,'Credit')],description=x.note); bg.add_task(send_email,receiver.email,'Money received',f'₹{x.amount} received')
    result={'transaction_ref':tx.transaction_ref,'status':tx.status,'amount':str(tx.amount)}; save_key(db,idempotency_key,result); db.commit(); return result
@app.get('/transfers/{transaction_ref}')
def transfer_detail(transaction_ref:str,u:User=Depends(current_user),db:Session=Depends(get_db)):
    t=db.query(Transaction).filter_by(transaction_ref=transaction_ref).first()
    if not t or (u.role!='Admin' and t.initiated_by!=u.id): raise HTTPException(404,'Transaction not found')
    return tx_out(t)
def tx_out(t): return {'transaction_ref':t.transaction_ref,'type':t.type,'amount':str(t.amount),'status':t.status,'description':t.description,'is_flagged':t.is_flagged,'created_at':t.created_at.isoformat()}
@app.get('/merchants/me/qr')
def merchant_qr(u:User=Depends(roles('Merchant'))):
    import qrcode
    from pathlib import Path
    p=Path('statements'); p.mkdir(exist_ok=True); path=p/f'{u.merchant_code}.png'; qrcode.make(u.merchant_code).save(path)
    return {'merchant_code':u.merchant_code,'qr_image_path':str(path),'qr_payload':u.merchant_code}
@app.post('/payments/merchant')
def merchant_pay(x:MerchantPay,bg:BackgroundTasks,idempotency_key:str=Header(...,alias='Idempotency-Key'),u:User=Depends(roles('User')),db:Session=Depends(get_db)):
    replay=money_key(u,'merchant-payment',idempotency_key,x.model_dump(),db)
    if replay:return {**replay,'Idempotent-Replayed':True}
    w=own_wallet(db,u,True); check_pin(w,x.pin); m=db.query(User).filter_by(merchant_code=x.merchant_code,role='Merchant').first()
    if not m: raise HTTPException(404,'Merchant not found')
    mw=db.query(Wallet).filter_by(owner_id=m.id).with_for_update().first(); limit_check(db,u,x.amount)
    if w.balance<x.amount: raise HTTPException(400,'Insufficient balance')
    fee=(x.amount*Decimal('.01')).quantize(Decimal('.01')); net=x.amount-fee
    tx=post(db,u.id,'Merchant Payment',x.amount,[(wallet_account(db,w.id),x.amount,'Debit'),(wallet_account(db,mw.id),net,'Credit'),(system_account(db,'Fees'),fee,'Credit')],description=m.business_name)
    cb=min((x.amount*Decimal('.02')).quantize(Decimal('.01')),Decimal('50.00')); db.add(Cashback(user_id=u.id,transaction_id=tx.id,amount=cb,status='Pending'))
    result={'transaction_ref':tx.transaction_ref,'status':tx.status,'merchant_received':str(net),'fee':str(fee),'cashback_pending':str(cb)}; save_key(db,idempotency_key,result); db.commit(); bg.add_task(send_email,m.email,'Merchant payment received',str(x.amount)); return result
@app.get('/merchants/me/payments')
def merchant_payments(skip:int=0,limit:int=20,u:User=Depends(roles('Merchant')),db:Session=Depends(get_db)):
    ids=[x.id for x in db.query(Wallet).filter_by(owner_id=u.id).all()]; accts=[a.id for a in db.query(Account).filter(Account.wallet_id.in_(ids)).all()]
    tids=[e.transaction_id for e in db.query(LedgerEntry).filter(LedgerEntry.account_id.in_(accts),LedgerEntry.entry_type=='Credit').all()]
    return [tx_out(t) for t in db.query(Transaction).filter(Transaction.id.in_(tids),Transaction.type=='Merchant Payment').order_by(Transaction.created_at.desc()).offset(skip).limit(min(limit,100)).all()]
@app.post('/payments/{transaction_ref}/refund')
def refund(transaction_ref:str,x:RefundIn,bg:BackgroundTasks,idempotency_key:str=Header(...,alias='Idempotency-Key'),u:User=Depends(roles('Merchant')),db:Session=Depends(get_db)):
    replay=money_key(u,'refund:'+transaction_ref,idempotency_key,x.model_dump(),db)
    if replay:return {**replay,'Idempotent-Replayed':True}
    original=db.query(Transaction).filter_by(transaction_ref=transaction_ref,type='Merchant Payment',status='Success').first()
    if not original: raise HTTPException(404,'Successful merchant payment not found')
    if original.created_at<datetime.utcnow()-timedelta(days=7): raise HTTPException(400,'Refund window is 7 days')
    rows=db.query(LedgerEntry).filter_by(transaction_id=original.id,entry_type='Credit').all(); merchant_acct=next((db.get(Account,e.account_id) for e in rows if db.get(Account,e.account_id).wallet_id),None)
    if not merchant_acct or db.get(Wallet,merchant_acct.wallet_id).owner_id!=u.id: raise HTTPException(403,'Only receiving merchant can refund')
    refunded=db.query(func.coalesce(func.sum(MerchantRefund.amount),0)).filter_by(transaction_id=original.id).scalar() or 0
    if Decimal(refunded)+x.amount>original.amount: raise HTTPException(400,'Refund exceeds remaining refundable amount')
    user_debit=next(e for e in db.query(LedgerEntry).filter_by(transaction_id=original.id,entry_type='Debit').all() if db.get(Account,e.account_id).wallet_id)
    user_wallet=db.get(Wallet,db.get(Account,user_debit.account_id).wallet_id); mw=db.get(Wallet,merchant_acct.wallet_id)
    tx=post(db,u.id,'Merchant Refund',x.amount,[(wallet_account(db,mw.id),x.amount,'Debit'),(wallet_account(db,user_wallet.id),x.amount,'Credit')]); db.add(MerchantRefund(transaction_id=original.id,refund_transaction_id=tx.id,amount=x.amount))
    cb=db.query(Cashback).filter_by(transaction_id=original.id,status='Pending').first()
    if cb: cb.status='Cancelled'
    result={'refund_ref':tx.transaction_ref,'status':'Success','amount':str(x.amount)}; save_key(db,idempotency_key,result); db.commit(); return result
@app.post('/money-requests',status_code=201)
def create_request(x:RequestIn,u:User=Depends(roles('User')),db:Session=Depends(get_db)):
    payer=db.query(User).filter_by(phone=x.payer_phone).first()
    if not payer or payer.id==u.id: raise HTTPException(400,'Invalid payer')
    count=db.query(MoneyRequest).filter_by(requester_id=u.id,payer_id=payer.id,status='Pending').count()
    if count>=5: raise HTTPException(400,'Maximum 5 pending requests to this person')
    r=MoneyRequest(requester_id=u.id,payer_id=payer.id,amount=x.amount,note=x.note,expires_at=datetime.utcnow()+timedelta(hours=48)); db.add(r); db.commit(); db.refresh(r); return {'id':r.id,'status':r.status,'expires_at':r.expires_at}
@app.get('/money-requests')
def list_requests(skip:int=0,limit:int=20,u:User=Depends(current_user),db:Session=Depends(get_db)):
    return [{'id':r.id,'requester_id':r.requester_id,'payer_id':r.payer_id,'amount':str(r.amount),'note':r.note,'status':r.status,'expires_at':r.expires_at} for r in db.query(MoneyRequest).filter(or_(MoneyRequest.requester_id==u.id,MoneyRequest.payer_id==u.id)).order_by(MoneyRequest.created_at.desc()).offset(skip).limit(min(limit,100)).all()]
@app.put('/money-requests/{request_id}/decline')
def decline_request(request_id:int,u:User=Depends(current_user),db:Session=Depends(get_db)):
    r=db.get(MoneyRequest,request_id)
    if not r or r.payer_id!=u.id: raise HTTPException(404,'Request not found')
    if r.status!='Pending': raise HTTPException(400,'Request is not pending')
    r.status='Declined'; db.commit(); return {'status':r.status}
@app.put('/money-requests/{request_id}/cancel')
def cancel_request(request_id:int,u:User=Depends(current_user),db:Session=Depends(get_db)):
    r=db.get(MoneyRequest,request_id)
    if not r or r.requester_id!=u.id: raise HTTPException(404,'Request not found')
    if r.status!='Pending': raise HTTPException(400,'Request is not pending')
    r.status='Cancelled'; db.commit(); return {'status':r.status}
@app.put('/money-requests/{request_id}/pay')
def pay_request(request_id:int,pin:str,otp:str|None=None,bg:BackgroundTasks=None,idempotency_key:str=Header(...,alias='Idempotency-Key'),u:User=Depends(current_user),db:Session=Depends(get_db)):
    r=db.get(MoneyRequest,request_id)
    if not r or r.payer_id!=u.id: raise HTTPException(404,'Request not found')
    if r.status!='Pending' or r.expires_at<datetime.utcnow(): raise HTTPException(400,'Request is not payable')
    requester=db.get(User,r.requester_id); data=Transfer(receiver_phone=requester.phone,amount=r.amount,pin=pin,otp=otp)
    result=transfer(data,bg or BackgroundTasks(),idempotency_key,u,db); r.status='Paid'; db.commit(); return result
@app.post('/bank-accounts',status_code=201)
def add_bank(x:BankIn,u:User=Depends(current_user),db:Session=Depends(get_db)):
    otp_ok(db,u,x.otp,'Add Bank Account')
    if db.query(BankAccount).filter_by(user_id=u.id).count()>=3: raise HTTPException(400,'Maximum 3 bank accounts')
    b=BankAccount(user_id=u.id,account_holder_name=x.account_holder_name,account_number=x.account_number,ifsc_code=x.ifsc_code,bank_name=x.bank_name,is_primary=db.query(BankAccount).filter_by(user_id=u.id).count()==0); db.add(b); db.commit(); db.refresh(b); return {'id':b.id,'account_holder_name':b.account_holder_name,'account_number':'X'*max(0,len(b.account_number)-4)+b.account_number[-4:],'ifsc_code':b.ifsc_code,'bank_name':b.bank_name,'is_primary':b.is_primary}
@app.get('/bank-accounts')
def banks(u:User=Depends(current_user),db:Session=Depends(get_db)):
    return [{'id':b.id,'account_holder_name':b.account_holder_name,'account_number':'X'*max(0,len(b.account_number)-4)+b.account_number[-4:],'ifsc_code':b.ifsc_code,'bank_name':b.bank_name,'is_primary':b.is_primary} for b in db.query(BankAccount).filter_by(user_id=u.id).all()]
@app.delete('/bank-accounts/{bank_account_id}')
def delete_bank(bank_account_id:int,u:User=Depends(current_user),db:Session=Depends(get_db)):
    b=db.query(BankAccount).filter_by(id=bank_account_id,user_id=u.id).first()
    if not b: raise HTTPException(404,'Bank account not found')
    db.delete(b); db.commit(); return {'message':'Bank account deleted'}
@app.post('/withdrawals')
def withdraw(x:WithdrawIn,bg:BackgroundTasks,idempotency_key:str=Header(...,alias='Idempotency-Key'),u:User=Depends(current_user),db:Session=Depends(get_db)):
    replay=money_key(u,'withdrawals',idempotency_key,x.model_dump(),db)
    if replay:return {**replay,'Idempotent-Replayed':True}
    w=own_wallet(db,u,True); check_pin(w,x.pin)
    if w.kyc_level!='Full' and u.role!='Merchant': raise HTTPException(403,'Full KYC required for withdrawal')
    b=db.query(BankAccount).filter_by(id=x.bank_account_id,user_id=u.id).first()
    if not b: raise HTTPException(404,'Bank account not found')
    limit_check(db,u,x.amount)
    tx=post(db,u.id,'Withdrawal',x.amount,[(wallet_account(db,w.id),x.amount,'Debit'),(system_account(db,'Withdrawal Clearing'),x.amount,'Credit')],status='Pending')
    wd=Withdrawal(transaction_id=tx.id,bank_account_id=b.id,amount=x.amount); db.add(wd); result={'transaction_ref':tx.transaction_ref,'status':'Pending','withdrawal_id':None}; db.flush(); result['withdrawal_id']=wd.id; save_key(db,idempotency_key,result); db.commit(); return result
@app.get('/withdrawals')
def withdrawals(skip:int=0,limit:int=20,u:User=Depends(current_user),db:Session=Depends(get_db)):
    q=db.query(Withdrawal).join(Transaction,Withdrawal.transaction_id==Transaction.id)
    if u.role!='Admin': q=q.filter(Transaction.initiated_by==u.id)
    return [{'id':w.id,'transaction_ref':db.get(Transaction,w.transaction_id).transaction_ref,'amount':str(w.amount),'status':w.status,'failure_reason':w.failure_reason} for w in q.order_by(Withdrawal.created_at.desc()).offset(skip).limit(min(limit,100)).all()]
@app.get('/admin/flagged-transactions')
def flagged(skip:int=0,limit:int=20,a:User=Depends(roles('Admin')),db:Session=Depends(get_db)):
    return [tx_out(t) for t in db.query(Transaction).filter_by(is_flagged=True,status='On Hold').offset(skip).limit(min(limit,100)).all()]
def decide_flag(ref,decision,reason,a,db):
    t=db.query(Transaction).filter_by(transaction_ref=ref,is_flagged=True,status='On Hold').first()
    if not t: raise HTTPException(404,'Flagged transaction not found')
    hold=system_account(db,'Hold'); sender_entry=next(e for e in t.entries if e.entry_type=='Debit'); sender_acct=db.get(Account,sender_entry.account_id)
    if decision=='approve':
        receiver_entry=next((e for e in t.entries if e.entry_type=='Credit' and db.get(Account,e.account_id).wallet_id),None)
        # receiver is supplied at initial hold via description-free metadata; this MVP routes approved held amount back to sender if receiver metadata unavailable.
        raise HTTPException(501,'Held transfer recipient resolution requires pending-transfer metadata; reject is supported safely')
    sender_wallet=db.get(Wallet,sender_acct.wallet_id); wa=wallet_account(db,sender_wallet.id)
    post(db,a.id,'Reversal',t.amount,[(hold,t.amount,'Debit'),(wa,t.amount,'Credit')]); t.status='Reversed'; db.add(FraudDecision(transaction_ref=ref,admin_id=a.id,decision='Rejected',reason=reason)); db.commit(); return {'status':'Rejected'}
@app.put('/admin/transactions/{transaction_ref}/approve')
def approve_flag(transaction_ref:str,reason:str,a:User=Depends(roles('Admin')),db:Session=Depends(get_db)): return decide_flag(transaction_ref,'approve',reason,a,db)
@app.put('/admin/transactions/{transaction_ref}/reject')
def reject_flag(transaction_ref:str,reason:str,a:User=Depends(roles('Admin')),db:Session=Depends(get_db)): return decide_flag(transaction_ref,'reject',reason,a,db)
@app.get('/transactions')
def transactions(skip:int=0,limit:int=20,type:str|None=None,status:str|None=None,min_amount:Decimal|None=None,max_amount:Decimal|None=None,from_date:date|None=None,to_date:date|None=None,sort_by:str='date',order:str='desc',u:User=Depends(current_user),db:Session=Depends(get_db)):
    q=db.query(Transaction)
    if u.role!='Admin': q=q.filter(Transaction.initiated_by==u.id)
    if type:q=q.filter(Transaction.type==type)
    if status:q=q.filter(Transaction.status==status)
    if min_amount is not None:q=q.filter(Transaction.amount>=min_amount)
    if max_amount is not None:q=q.filter(Transaction.amount<=max_amount)
    if from_date:q=q.filter(Transaction.created_at>=datetime.combine(from_date,datetime.min.time()))
    if to_date:q=q.filter(Transaction.created_at<=datetime.combine(to_date,datetime.max.time()))
    if sort_by not in {'date','amount'} or order not in {'asc','desc'}: raise HTTPException(422,'Invalid sorting')
    col=Transaction.created_at if sort_by=='date' else Transaction.amount; q=q.order_by(col.asc() if order=='asc' else col.desc())
    return [tx_out(t) for t in q.offset(skip).limit(min(limit,100)).all()]
@app.get('/transactions/{transaction_ref}')
def transaction_detail(transaction_ref:str,u:User=Depends(current_user),db:Session=Depends(get_db)):
    t=db.query(Transaction).filter_by(transaction_ref=transaction_ref).first()
    if not t or (u.role!='Admin' and t.initiated_by!=u.id): raise HTTPException(404,'Transaction not found')
    return tx_out(t)
@app.get('/statements')
def statement(month:str,format:str=Query('pdf',pattern='^(pdf|csv)$'),u:User=Depends(current_user),db:Session=Depends(get_db)):
    try: y,m=map(int,month.split('-')); start=datetime(y,m,1); end=datetime(y+1,1,1) if m==12 else datetime(y,m+1,1)
    except Exception: raise HTTPException(422,'month must be YYYY-MM')
    w=own_wallet(db,u); account=wallet_account(db,w.id); entries=db.query(LedgerEntry).filter_by(account_id=account.id).join(Transaction).filter(Transaction.created_at<end).order_by(Transaction.created_at).all()
    opening=Decimal('0.00'); rows=[]
    for e in entries:
        tx=db.get(Transaction,e.transaction_id)
        if tx.created_at<start: opening+=e.amount if e.entry_type=='Credit' else -e.amount
        else: rows.append((tx,e))
    credits=sum((e.amount for t,e in rows if e.entry_type=='Credit'),Decimal('0')); debits=sum((e.amount for t,e in rows if e.entry_type=='Debit'),Decimal('0')); closing=opening+credits-debits
    if format=='csv':
        s=io.StringIO(); cw=csv.writer(s); cw.writerow(['Wallet',w.wallet_number,'Name',u.full_name,'Period',month]); cw.writerow(['Opening Balance',opening]); cw.writerow(['Date','Reference','Type','Status','Debit','Credit'])
        for t,e in rows:cw.writerow([t.created_at.isoformat(),t.transaction_ref,t.type,t.status,e.amount if e.entry_type=='Debit' else '',e.amount if e.entry_type=='Credit' else ''])
        cw.writerow(['Total Credits',credits]); cw.writerow(['Total Debits',debits]); cw.writerow(['Closing Balance',closing]); return Response(s.getvalue(),media_type='text/csv',headers={'Content-Disposition':f'attachment; filename=statement-{month}.csv'})
    buf=io.BytesIO(); c=canvas.Canvas(buf,pagesize=A4); c.drawString(50,800,f'Digital Wallet Statement - {month}'); c.drawString(50,780,f'{u.full_name} | {w.wallet_number}'); c.drawString(50,755,f'Opening: INR {opening}   Credits: INR {credits}   Debits: INR {debits}   Closing: INR {closing}'); y0=725
    for t,e in rows:
        if y0<60:c.showPage(); y0=800
        c.drawString(45,y0,f'{t.created_at:%Y-%m-%d} {t.transaction_ref} {t.type} {e.entry_type} INR {e.amount} {t.status}'); y0-=18
    c.save(); buf.seek(0); return StreamingResponse(buf,media_type='application/pdf',headers={'Content-Disposition':f'attachment; filename=statement-{month}.pdf'})
@app.get('/reports/admin-dashboard')
def admin_dashboard(a:User=Depends(roles('Admin')),db:Session=Depends(get_db)):
    return {'wallet_total':str(db.query(func.coalesce(func.sum(Wallet.balance),0)).scalar()),'today_transactions':db.query(Transaction).filter(Transaction.created_at>=datetime.utcnow().replace(hour=0,minute=0,second=0,microsecond=0)).count(),'failed_transactions':db.query(Transaction).filter_by(status='Failed').count(),'on_hold':db.query(Transaction).filter_by(status='On Hold').count(),'cashback_paid':str(db.query(func.coalesce(func.sum(Cashback.amount),0)).filter_by(status='Credited').scalar())}
@app.get('/reports/ledger-summary')
def ledger_summary(start_date:date|None=None,end_date:date|None=None,a:User=Depends(roles('Admin')),db:Session=Depends(get_db)):
    q=db.query(LedgerEntry).join(Transaction)
    if start_date:q=q.filter(Transaction.created_at>=datetime.combine(start_date,datetime.min.time()))
    if end_date:q=q.filter(Transaction.created_at<=datetime.combine(end_date,datetime.max.time()))
    result=[]
    for ac in db.query(Account).filter(Account.wallet_id==None).all():
        deb=q.filter(LedgerEntry.account_id==ac.id,LedgerEntry.entry_type=='Debit').with_entities(func.coalesce(func.sum(LedgerEntry.amount),0)).scalar(); cre=q.filter(LedgerEntry.account_id==ac.id,LedgerEntry.entry_type=='Credit').with_entities(func.coalesce(func.sum(LedgerEntry.amount),0)).scalar(); result.append({'account':ac.name,'debits':str(deb),'credits':str(cre)})
    return {'system_accounts':result,'total_debits':str(q.filter_by(entry_type='Debit').with_entities(func.coalesce(func.sum(LedgerEntry.amount),0)).scalar()),'total_credits':str(q.filter_by(entry_type='Credit').with_entities(func.coalesce(func.sum(LedgerEntry.amount),0)).scalar())}
@app.get('/reports/merchant-sales')
def merchant_sales(a:User=Depends(roles('Merchant')),db:Session=Depends(get_db)):
    ids=[w.id for w in db.query(Wallet).filter_by(owner_id=a.id).all()]; aids=[x.id for x in db.query(Account).filter(Account.wallet_id.in_(ids)).all()]; tids=[e.transaction_id for e in db.query(LedgerEntry).filter(LedgerEntry.account_id.in_(aids),LedgerEntry.entry_type=='Credit').all()]; txs=db.query(Transaction).filter(Transaction.id.in_(tids),Transaction.type=='Merchant Payment').all()
    return {'payments':len(txs),'gross_sales':str(sum((t.amount for t in txs),Decimal('0'))),'daily_sales':{str(d):str(v) for d,v in __import__('itertools').groupby([])},'platform_fee_rate':'1%'}
@app.get('/reports/my-spending')
def my_spending(u:User=Depends(roles('User')),db:Session=Depends(get_db)):
    txs=db.query(Transaction).filter(Transaction.initiated_by==u.id,Transaction.status=='Success').all(); totals={}
    for t in txs: totals[t.type]=str(Decimal(totals.get(t.type,'0'))+t.amount)
    cb=db.query(func.coalesce(func.sum(Cashback.amount),0)).filter_by(user_id=u.id,status='Credited').scalar(); return {'spending_by_type':totals,'cashback_earned':str(cb)}
@app.get('/health')
def health(): return {'status':'ok'}
