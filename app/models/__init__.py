from datetime import datetime
from decimal import Decimal
from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base

class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
class User(TimestampMixin, Base):
    __tablename__='users'
    id: Mapped[int]=mapped_column(primary_key=True)
    full_name: Mapped[str]=mapped_column(String(120))
    email: Mapped[str]=mapped_column(String(255),unique=True,index=True)
    phone: Mapped[str]=mapped_column(String(10),unique=True,index=True)
    password_hash: Mapped[str]=mapped_column(String(255))
    role: Mapped[str]=mapped_column(String(20),default='User')
    is_active: Mapped[bool]=mapped_column(Boolean,default=True)
    business_name: Mapped[str|None]=mapped_column(String(150),nullable=True)
    business_category: Mapped[str|None]=mapped_column(String(80),nullable=True)
    merchant_code: Mapped[str|None]=mapped_column(String(30),unique=True,nullable=True)
    wallet: Mapped['Wallet']=relationship(back_populates='owner',uselist=False)
class Wallet(TimestampMixin, Base):
    __tablename__='wallets'
    id: Mapped[int]=mapped_column(primary_key=True)
    owner_id: Mapped[int]=mapped_column(ForeignKey('users.id'),unique=True)
    wallet_number: Mapped[str]=mapped_column(String(30),unique=True)
    balance: Mapped[Decimal]=mapped_column(Numeric(12,2),default=Decimal('0.00'))
    kyc_level: Mapped[str]=mapped_column(String(20),default='Minimum')
    status: Mapped[str]=mapped_column(String(20),default='Active')
    pin_hash: Mapped[str|None]=mapped_column(String(255),nullable=True)
    pin_failures: Mapped[int]=mapped_column(Integer,default=0)
    pin_locked_until: Mapped[datetime|None]=mapped_column(DateTime,nullable=True)
    owner: Mapped[User]=relationship(back_populates='wallet')
class KYC(TimestampMixin, Base):
    __tablename__='kycs'
    id: Mapped[int]=mapped_column(primary_key=True)
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id'),index=True)
    pan_number: Mapped[str]=mapped_column(String(10),unique=True)
    aadhaar_last4: Mapped[str]=mapped_column(String(4))
    full_name: Mapped[str]=mapped_column(String(120))
    date_of_birth: Mapped[datetime]=mapped_column(Date)
    status: Mapped[str]=mapped_column(String(20),default='Pending')
    rejection_reason: Mapped[str|None]=mapped_column(Text,nullable=True)
class Account(TimestampMixin, Base):
    __tablename__='accounts'
    id: Mapped[int]=mapped_column(primary_key=True)
    name: Mapped[str]=mapped_column(String(100),unique=True)
    account_type: Mapped[str]=mapped_column(String(40))
    wallet_id: Mapped[int|None]=mapped_column(ForeignKey('wallets.id'),nullable=True)
class Transaction(TimestampMixin, Base):
    __tablename__='transactions'
    id: Mapped[int]=mapped_column(primary_key=True)
    transaction_ref: Mapped[str]=mapped_column(String(40),unique=True,index=True)
    type: Mapped[str]=mapped_column(String(40))
    amount: Mapped[Decimal]=mapped_column(Numeric(12,2))
    status: Mapped[str]=mapped_column(String(20),default='Pending')
    initiated_by: Mapped[int]=mapped_column(ForeignKey('users.id'))
    description: Mapped[str|None]=mapped_column(String(255),nullable=True)
    is_flagged: Mapped[bool]=mapped_column(Boolean,default=False)
    entries: Mapped[list['LedgerEntry']]=relationship(back_populates='transaction')
class LedgerEntry(TimestampMixin, Base):
    __tablename__='ledger_entries'
    id: Mapped[int]=mapped_column(primary_key=True)
    transaction_id: Mapped[int]=mapped_column(ForeignKey('transactions.id'),index=True)
    account_id: Mapped[int]=mapped_column(ForeignKey('accounts.id'),index=True)
    entry_type: Mapped[str]=mapped_column(String(10))
    amount: Mapped[Decimal]=mapped_column(Numeric(12,2))
    balance_after: Mapped[Decimal|None]=mapped_column(Numeric(12,2),nullable=True)
    transaction: Mapped[Transaction]=relationship(back_populates='entries')
class BankAccount(TimestampMixin, Base):
    __tablename__='bank_accounts'
    id: Mapped[int]=mapped_column(primary_key=True)
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id'))
    account_holder_name: Mapped[str]=mapped_column(String(120))
    account_number: Mapped[str]=mapped_column(String(18))
    ifsc_code: Mapped[str]=mapped_column(String(11))
    bank_name: Mapped[str]=mapped_column(String(120))
    is_primary: Mapped[bool]=mapped_column(Boolean,default=False)
class OTP(TimestampMixin, Base):
    __tablename__='otps'
    id: Mapped[int]=mapped_column(primary_key=True)
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id'))
    purpose: Mapped[str]=mapped_column(String(40))
    otp_hash: Mapped[str]=mapped_column(String(255))
    expires_at: Mapped[datetime]=mapped_column(DateTime)
    attempts: Mapped[int]=mapped_column(Integer,default=0)
    is_used: Mapped[bool]=mapped_column(Boolean,default=False)
class Idempotency(TimestampMixin, Base):
    __tablename__='idempotency_keys'
    id: Mapped[int]=mapped_column(primary_key=True)
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id'))
    key: Mapped[str]=mapped_column(String(100),unique=True)
    endpoint: Mapped[str]=mapped_column(String(120))
    body_hash: Mapped[str]=mapped_column(String(64))
    response_json: Mapped[str|None]=mapped_column(Text,nullable=True)
    status_code: Mapped[int|None]=mapped_column(Integer,nullable=True)
class MoneyRequest(TimestampMixin, Base):
    __tablename__='money_requests'
    id: Mapped[int]=mapped_column(primary_key=True)
    requester_id: Mapped[int]=mapped_column(ForeignKey('users.id'))
    payer_id: Mapped[int]=mapped_column(ForeignKey('users.id'))
    amount: Mapped[Decimal]=mapped_column(Numeric(12,2))
    note: Mapped[str|None]=mapped_column(String(255),nullable=True)
    status: Mapped[str]=mapped_column(String(20),default='Pending')
    expires_at: Mapped[datetime]=mapped_column(DateTime)
class MerchantRefund(TimestampMixin, Base):
    __tablename__='merchant_refunds'
    id: Mapped[int]=mapped_column(primary_key=True)
    transaction_id: Mapped[int]=mapped_column(ForeignKey('transactions.id'))
    refund_transaction_id: Mapped[int]=mapped_column(ForeignKey('transactions.id'))
    amount: Mapped[Decimal]=mapped_column(Numeric(12,2))
class Withdrawal(TimestampMixin, Base):
    __tablename__='withdrawals'
    id: Mapped[int]=mapped_column(primary_key=True)
    transaction_id: Mapped[int]=mapped_column(ForeignKey('transactions.id'))
    bank_account_id: Mapped[int]=mapped_column(ForeignKey('bank_accounts.id'))
    amount: Mapped[Decimal]=mapped_column(Numeric(12,2))
    status: Mapped[str]=mapped_column(String(20),default='Pending')
    processed_at: Mapped[datetime|None]=mapped_column(DateTime,nullable=True)
    failure_reason: Mapped[str|None]=mapped_column(Text,nullable=True)
class Cashback(TimestampMixin, Base):
    __tablename__='cashbacks'
    id: Mapped[int]=mapped_column(primary_key=True)
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id'))
    transaction_id: Mapped[int]=mapped_column(ForeignKey('transactions.id'))
    amount: Mapped[Decimal]=mapped_column(Numeric(12,2))
    status: Mapped[str]=mapped_column(String(20),default='Pending')
class FraudDecision(TimestampMixin, Base):
    __tablename__='fraud_decisions'
    id: Mapped[int]=mapped_column(primary_key=True)
    transaction_ref: Mapped[str]=mapped_column(String(40))
    admin_id: Mapped[int]=mapped_column(ForeignKey('users.id'))
    decision: Mapped[str]=mapped_column(String(20))
    reason: Mapped[str]=mapped_column(Text)
