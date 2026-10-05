import hashlib
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select, update

from .config import settings
from .models import Usage, User


def month_key():
    return datetime.now(timezone.utc).strftime('%Y-%m')


def refresh_allowance(db, user):
    period = month_key()
    if user.tier == 'beta' and user.period != period:
        db.execute(update(User).where(User.id == user.id, User.period != period).values(credits=settings.beta_credits, period=period))
        db.commit()
        db.refresh(user)


def reserve(db, user, count, key, operation, fingerprint):
    refresh_allowance(db, user)
    existing = db.scalar(select(Usage).where(Usage.user_id == user.id, Usage.request_key == key))
    digest = hashlib.sha256(fingerprint.encode()).hexdigest()
    if existing:
        if existing.fingerprint != digest:
            raise HTTPException(409, 'This request identifier was already used for a different request.')
        return existing, False
    # Compare-and-swap protects balances on SQLite and PostgreSQL without a process-local lock.
    for _ in range(5):
        db.refresh(user)
        if user.credits + user.topup_credits < count:
            raise HTTPException(402, 'You have reached your response allowance. Your usage page shows when it resets.')
        monthly = min(user.credits, count)
        topup = count - monthly
        changed = db.execute(update(User).where(User.id == user.id, User.credits == user.credits,
                                                User.topup_credits == user.topup_credits, User.period == user.period)
                             .values(credits=user.credits - monthly, topup_credits=user.topup_credits - topup))
        if changed.rowcount:
            usage = Usage(user_id=user.id, request_key=key, fingerprint=digest, operation=operation,
                          period=user.period, monthly_reserved=monthly, topup_reserved=topup)
            db.add(usage)
            try:
                db.commit()
            except Exception:
                db.rollback()
                existing = db.scalar(select(Usage).where(Usage.user_id == user.id, Usage.request_key == key))
                if existing and existing.fingerprint == digest:
                    return existing, False
                raise
            return usage, True
        db.rollback()
    raise HTTPException(409, 'Another request is updating your allowance. Please retry.')


def settle(db, usage_id, consumed, result_id=None, input_tokens=0, output_tokens=0):
    usage = db.get(Usage, usage_id)
    if not usage or usage.status != 'reserved':
        return
    total = usage.monthly_reserved + usage.topup_reserved
    consumed = max(0, min(total, consumed))
    claimed = db.execute(update(Usage).where(Usage.id == usage.id, Usage.status == 'reserved').values(
        status='completed' if consumed else 'refunded', consumed=consumed, result_id=result_id,
        input_tokens=input_tokens, output_tokens=output_tokens))
    if claimed.rowcount:
        monthly_consumed = min(consumed, usage.monthly_reserved)
        topup_consumed = max(0, consumed - usage.monthly_reserved)
        db.execute(update(User).where(User.id == usage.user_id, User.period == usage.period).values(
            credits=User.credits + usage.monthly_reserved - monthly_consumed))
        db.execute(update(User).where(User.id == usage.user_id).values(
            topup_credits=User.topup_credits + usage.topup_reserved - topup_consumed))
    db.commit()
