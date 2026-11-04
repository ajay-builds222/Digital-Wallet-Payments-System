from datetime import datetime, timedelta
from jose import jwt, JWTError
from passlib.context import CryptContext
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from app.database import settings, get_db
from app.models import User
pwd=CryptContext(schemes=['bcrypt'],deprecated='auto')
bearer=HTTPBearer(auto_error=False)
def hash_secret(v:str)->str: return pwd.hash(v)
def verify_secret(v:str,h:str)->bool: return pwd.verify(v,h)
def create_token(user_id:int,role:str):
    exp=datetime.utcnow()+timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode({'sub':str(user_id),'role':role,'exp':exp},settings.SECRET_KEY,algorithm='HS256')
def current_user(creds:HTTPAuthorizationCredentials=Depends(bearer),db:Session=Depends(get_db)):
    if not creds: raise HTTPException(401,'Authentication required')
    try: data=jwt.decode(creds.credentials,settings.SECRET_KEY,algorithms=['HS256']); uid=int(data['sub'])
    except Exception: raise HTTPException(401,'Invalid or expired token')
    user=db.get(User,uid)
    if not user or not user.is_active: raise HTTPException(401,'Inactive or unknown user')
    return user
def roles(*allowed):
    def check(user:User=Depends(current_user)):
        if user.role not in allowed: raise HTTPException(403,'Insufficient role')
        return user
    return check
