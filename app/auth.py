import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import select

from .config import settings
from .db import get_db
from .models import LoginSession, User, utcnow

COOKIE = 'confine_session'


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return f'{salt.hex()}:{digest.hex()}'


def verify_password(password, stored):
    salt, expected = stored.split(':')
    actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32)
    return hmac.compare_digest(actual.hex(), expected)


def create_session(db, user, response: Response):
    token = secrets.token_urlsafe(32)
    session = LoginSession(token_hash=hashlib.sha256(token.encode()).hexdigest(), user_id=user.id,
                           csrf=secrets.token_hex(32), expires_at=utcnow() + timedelta(days=14))
    db.add(session)
    db.commit()
    response.set_cookie(COOKIE, token, httponly=True, secure=settings.environment == 'production',
                        samesite='lax', max_age=14 * 86400, path='/')
    return session


def current_user(request: Request, db=Depends(get_db)):
    token = request.cookies.get(COOKIE, '')
    session = db.get(LoginSession, hashlib.sha256(token.encode()).hexdigest()) if token else None
    if not session or session.expires_at < utcnow():
        raise HTTPException(401, 'Please sign in to continue.')
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        if not hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), session.csrf):
            raise HTTPException(403, 'Session verification failed. Refresh the page and try again.')
    request.state.session = session
    return db.get(User, session.user_id)


class RateLimiter:
    """Bounded single-process limiter; deployment runs one application process."""
    def __init__(self):
        self.hits = defaultdict(deque)
        self.lock = threading.Lock()

    def check(self, key, limit=10, seconds=60):
        now = time.monotonic()
        with self.lock:
            if len(self.hits) > 5000:
                self.hits = defaultdict(deque, {k: v for k, v in self.hits.items() if v and v[-1] > now - 3600})
            queue = self.hits[key]
            while queue and queue[0] < now - seconds:
                queue.popleft()
            if len(queue) >= limit:
                raise HTTPException(429, 'Too many requests. Please wait a minute and try again.')
            queue.append(now)


limiter = RateLimiter()


def owned_enrollment(db, user_id, enrollment_id):
    from .models import Enrollment
    item = db.scalar(select(Enrollment).where(Enrollment.id == enrollment_id, Enrollment.user_id == user_id))
    if not item:
        raise HTTPException(404, 'Course not found.')
    return item
