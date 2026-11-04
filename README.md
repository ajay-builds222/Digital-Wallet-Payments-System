# Digital Wallet & Payments System (FastAPI)

Swagger-ready educational backend implementing the supplied Digital Wallet assignment. It uses Decimal/Numeric money, SQLAlchemy, Alembic, JWT, role checks, PIN/OTP, a double-entry posting helper, idempotency headers, QR generation, statements and scheduled processing.

## 1. Requirements
- Python 3.9+ (recommended Python 3.11 or 3.12)
- MySQL 8.0+ and MySQL Workbench (optional)
- Windows PowerShell examples below

## 2. Create database
```sql
CREATE DATABASE digital_wallet_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

## 3. Setup
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```
Edit `.env`: set your MySQL password and a long random SECRET_KEY. Set SMTP values if email delivery is required. Never commit `.env`.

## 4. Create tables with Alembic
```powershell
alembic upgrade head
```
No `create_all()` is used by application startup; the initial Alembic revision creates the schema.

## 5. Run
```powershell
uvicorn app.main:app --reload
```
Open **http://127.0.0.1:8000/docs**. `GET /health` checks process health.

## 6. Swagger test sequence
1. `POST /auth/register` create a User. Use a different phone/email for each account.
2. `POST /auth/login`, copy `access_token`, click **Authorize**, enter `Bearer <token>`.
3. `POST /wallet/pin` set a non-simple 4-digit PIN (example `4826`).
4. `POST /wallet/add-money` with `Idempotency-Key: <unique UUID>` and amount >= 10. Use a fresh UUID for every new money request.
5. Register a second User and repeat login + PIN setup to test P2P transfer.
6. `POST /transfers` with receiver phone, amount, PIN, and `Idempotency-Key`. For amount > 5000, first call `/otp/send?purpose=High%20Value%20Transfer`; when SMTP is not configured the development response includes `debug_otp`.
7. To test merchant payment, register with role `Merchant` and business fields, login as a User, use merchant_code, and call `/payments/merchant`.
8. For KYC, submit `/kyc`, then log in as an Admin account created manually in the database (public Admin registration is blocked by the schema). Approve with `/admin/kyc/{id}/approve`.

## 7. Admin bootstrap (one-time)
Public registration intentionally does not allow Admin. Create the first admin with this one-time Python snippet from project root after migrations:
```powershell
python -c "from app.database import SessionLocal; from app.models import User; from app.auth.security import hash_secret; d=SessionLocal(); u=User(full_name='System Admin',email='admin@example.com',phone='9999999999',password_hash=hash_secret('Admin@12345'),role='Admin'); d.add(u); d.commit(); d.close(); print('Admin created')"
```
Change the sample credentials immediately. The bootstrap command only creates the Admin user; create a wallet separately only if required.

## 8. Important implementation notes
- All amounts use `Decimal` and SQL `Numeric(12,2)`.
- Ledger posting helper verifies total debits equal total credits and updates wallet quick balances in the same SQLAlchemy transaction.
- Add-money, transfer, merchant-payment, refund and withdrawal endpoints require `Idempotency-Key`.
- OTP is hashed and expires after five minutes. When SMTP is not configured, `/otp/send` returns `debug_otp` for local Swagger testing; never enable that in production.
- Generated QR PNGs and statement exports are local runtime artifacts; `statements/` is gitignored.
- SMTP failures are logged and do not reverse money movements.

## 9. Scope / production-readiness
This is an educational assignment implementation, not a real-money payment product. Integrate a regulated payment/banking provider, secrets manager, production migrations, durable outbox, audit logging, robust rate limiting, and independent security review before any real use. Some advanced flows (notably held-transfer approval routing, comprehensive reversal/refund reconciliation, and report aggregation) are intentionally marked incomplete in the API and require the recipient/payment metadata extension before production. Verify each requirement against your instructor's rubric before submission.
