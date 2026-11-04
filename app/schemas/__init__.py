from datetime import date
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, EmailStr, Field, field_validator
class Register(BaseModel):
    full_name:str=Field(min_length=2,max_length=120); email:EmailStr; phone:str=Field(pattern=r'^\d{10}$'); password:str=Field(min_length=8); role:Literal['User','Merchant']='User'; business_name:str|None=None; business_category:str|None=None
class Login(BaseModel): email:EmailStr; password:str
class Amount(BaseModel): amount:Decimal=Field(gt=0,max_digits=12,decimal_places=2)
class AddMoney(Amount): source:Literal['UPI','Card','Net Banking']; simulate_status:Literal['Success','Failed']='Success'
class Transfer(BaseModel): receiver_phone:str=Field(pattern=r'^\d{10}$'); amount:Decimal=Field(gt=0,max_digits=12,decimal_places=2); note:str|None=None; pin:str=Field(pattern=r'^\d{4}$'); otp:str|None=None
class PinIn(BaseModel): pin:str=Field(pattern=r'^\d{4}$')
class KYCIn(BaseModel): pan_number:str=Field(pattern=r'^[A-Z]{5}[0-9]{4}[A-Z]$'); aadhaar_last4:str=Field(pattern=r'^\d{4}$'); full_name:str; date_of_birth:date
class BankIn(BaseModel): account_holder_name:str; account_number:str=Field(pattern=r'^\d{9,18}$'); ifsc_code:str=Field(pattern=r'^[A-Z]{4}0[A-Z0-9]{6}$'); bank_name:str; otp:str
class MerchantPay(BaseModel): merchant_code:str; amount:Decimal=Field(gt=0,max_digits=12,decimal_places=2); pin:str=Field(pattern=r'^\d{4}$')
class RefundIn(BaseModel): amount:Decimal=Field(gt=0,max_digits=12,decimal_places=2); reason:str|None=None
class RequestIn(BaseModel): payer_phone:str=Field(pattern=r'^\d{10}$'); amount:Decimal=Field(gt=0,max_digits=12,decimal_places=2); note:str|None=None
class WithdrawIn(BaseModel): bank_account_id:int; amount:Decimal=Field(ge=100,max_digits=12,decimal_places=2); pin:str=Field(pattern=r'^\d{4}$')
