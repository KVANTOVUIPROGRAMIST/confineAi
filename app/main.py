import asyncio
import contextlib
import hashlib
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import stripe
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from . import billing, pipeline
from .auth import COOKIE, create_session, current_user, hash_password, limiter, owned_enrollment, verify_password
from .catalog import SCHOOLS, course_dict, seed_catalog
from .config import ROOT, settings
from .credits import month_key, refresh_allowance, reserve, settle
from .db import Base, SessionLocal, engine, get_db
from .documents import extract_file, extract_questions
from .assignment_files import validate_assignment_file
from .jobs import assignment_worker, recover_reservations
from .models import Assignment, AssignmentFile, AssignmentOutput, Chat, Course, Document, Enrollment, LoginSession, Usage, User, utcnow
from .schemas import AssignmentDraft, AssignmentRetry, AssignmentRun, AuthInput, ChatInput, EnrollmentInput


@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        seed_catalog(db)
        db.execute(delete(LoginSession).where(LoginSession.expires_at < utcnow()))
        # One app process owns the durable queue. Resume interrupted assignments without dropping results.
        db.execute(update(Assignment).where(Assignment.status == 'running').values(status='queued'))
        db.commit()
        orphaned = db.scalars(select(Usage).where(Usage.status == 'reserved', Usage.operation == 'chat')).all()
        for usage in orphaned:
            settle(db, usage.id, 0)
        recover_reservations(db)
    worker = asyncio.create_task(assignment_worker())
    warmup = None
    if settings.environment == 'production':
        from .typesetting import warm_typesetter
        warmup = asyncio.create_task(asyncio.to_thread(warm_typesetter))
    yield
    worker.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await worker
    if warmup:
        warmup.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await warmup


app = FastAPI(title='Confine', lifespan=lifespan, docs_url='/api/docs' if settings.environment != 'production' else None,
              redoc_url=None)


@app.middleware('http')
async def security_headers(request, call_next):
    origin = request.headers.get('origin')
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and origin:
        allowed = {settings.app_url}
        if settings.environment != 'production':
            allowed.update({'http://localhost:8000', 'http://127.0.0.1:8000'})
        if origin.rstrip('/') not in allowed:
            return Response('Origin not allowed', status_code=403)
    # Reject obviously oversized bodies before parsing multipart. Upload routes also count bytes.
    length = request.headers.get('content-length', '0')
    if length.isdigit() and int(length) > settings.max_upload_bytes + 100_000:
        return Response('Request too large', status_code=413)
    response = await call_next(request)
    response.headers.update({'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'same-origin',
                             'X-Frame-Options': 'DENY',
                             'Content-Security-Policy': "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"})
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


def user_dict(db, user, session=None):
    refresh_allowance(db, user)
    return {'id': user.id, 'name': user.name, 'email': user.email, 'tier': user.tier,
            'credits': user.credits, 'topup_credits': user.topup_credits, 'period': user.period,
            'csrf': session.csrf if session else None}


def enrollment_dict(db, item):
    course = db.get(Course, item.course_id)
    return {'id': item.id, 'course': course_dict(course), 'prerequisites': item.prerequisites, 'mode': item.mode,
            'term': item.term, 'instructor': item.instructor, 'topic': item.topic,
            'material_count': db.scalar(select(func.count()).select_from(Document).where(Document.enrollment_id == item.id)),
            'reference_count': len(pipeline.available_sources(db, item))}


def assignment_dict(item, include_results=True):
    original = item.file
    return {'id': item.id, 'enrollment_id': item.enrollment_id, 'title': item.title, 'questions': item.questions,
            'status': item.status, 'progress': len(item.results), 'results': item.results if include_results else [],
            'error': item.error, 'download_ready': item.pdf_ready, 'created_at': item.created_at.isoformat() + 'Z',
            'whole_file': bool(original), 'stage': original.stage if original else item.status,
            'output_layout': item.output.layout if item.output else 'legacy',
            'latex_ready': item.pdf_ready and bool(item.output),
            'warnings': item.output.warnings if item.output and include_results else [],
            'file': {'name': original.name, 'pages': original.pages, 'size': original.size} if original else None,
            'submission_ready': item.status == 'completed' and item.pdf_ready}


def ensure_ai():
    if not settings.ai_ready:
        raise HTTPException(503, 'Live AI is not connected yet. Course selection, uploads, and assignment previews work now. Add a server API key to generate answers.')


@app.get('/api/health')
def health(db=Depends(get_db)):
    db.execute(select(1))
    return {'status': 'ok'}


@app.get('/api/config')
def config():
    return {'ai_ready': settings.ai_ready, 'ai_provider': settings.ai_provider, 'beta': settings.beta_enabled,
            'billing_ready': settings.billing_ready, 'beta_credits': settings.beta_credits, 'price': 15,
            'paid_credits': 300, 'max_questions': settings.max_questions, 'schools': SCHOOLS,
            'featured_courses': ['ucsd-cse-21', 'ucsd-cse-29', 'ucsd-cse-190', 'ucsd-math-183', 'ucsd-mmw-122']}


@app.post('/api/auth/register')
def register(body: AuthInput, request: Request, response: Response, db=Depends(get_db)):
    limiter.check('register:' + request.client.host, 8, 3600)
    email = body.email.strip().lower()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
        raise HTTPException(422, 'Enter a valid email address.')
    name = body.name.strip() or email.split('@')[0]
    user = User(email=email, name=name, password_hash=hash_password(body.password),
                tier='beta' if settings.beta_enabled else 'free', credits=settings.beta_credits if settings.beta_enabled else 20,
                period=month_key() if settings.beta_enabled else 'trial')
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, 'An account with that email already exists. Sign in instead.') from None
    session = create_session(db, user, response)
    return user_dict(db, user, session)


@app.post('/api/auth/login')
def login(body: AuthInput, request: Request, response: Response, db=Depends(get_db)):
    limiter.check('login:' + request.client.host, 12)
    user = db.scalar(select(User).where(User.email == body.email.strip().lower()))
    stored = user.password_hash if user else hash_password('constant-dummy-password')
    if not verify_password(body.password, stored) or not user:
        raise HTTPException(401, 'Email or password is incorrect.')
    session = create_session(db, user, response)
    return user_dict(db, user, session)


@app.get('/api/auth/me')
def me(request: Request, user=Depends(current_user), db=Depends(get_db)):
    return user_dict(db, user, request.state.session)


@app.post('/api/auth/logout')
def logout(request: Request, response: Response, user=Depends(current_user), db=Depends(get_db)):
    db.delete(request.state.session)
    db.commit()
    response.delete_cookie(COOKIE, path='/')
    return {'ok': True}


@app.delete('/api/account')
def delete_account(user=Depends(current_user), db=Depends(get_db)):
    active = db.scalar(select(Assignment.id).where(Assignment.user_id == user.id, Assignment.status.in_(['queued', 'running'])))
    if active:
        raise HTTPException(409, 'Wait for your current assignment to finish before deleting your account.')
    if user.subscription_id:
        raise HTTPException(409, 'Cancel your subscription in the billing portal before deleting your account.')
    # Explicit order also works when a foreign-key target (usage) is referenced by assignments.
    for model in (Assignment, Chat, Document, Enrollment, LoginSession, Usage):
        db.execute(delete(model).where(model.user_id == user.id))
    db.delete(user)
    db.commit()
    return {'ok': True}


@app.get('/api/catalog')
def catalog(school: str = 'ucsd', q: str = '', db=Depends(get_db)):
    query = select(Course).where(Course.school == school)
    if q:
        q = re.sub(r'([A-Za-z]+)[- ]+0*(\d+)', r'\1 \2', q.strip())
        query = query.where(Course.code.ilike('%' + q[:100] + '%') | Course.title.ilike('%' + q[:100] + '%'))
    return [course_dict(c) for c in db.scalars(query.order_by(Course.code).limit(100)).all()]


@app.get('/api/catalog/{course_id}')
def catalog_course(course_id: str, db=Depends(get_db)):
    course = db.get(Course, course_id)
    if not course:
        raise HTTPException(404, 'This course is not in the imported catalog yet.')
    prerequisites = db.scalars(select(Course).where(Course.school == course.school, Course.code.in_(course.prerequisite_codes))).all()
    return {'course': course_dict(course), 'prerequisites': [course_dict(c) for c in prerequisites]}


@app.get('/api/courses')
def courses(user=Depends(current_user), db=Depends(get_db)):
    return [enrollment_dict(db, e) for e in db.scalars(select(Enrollment).where(Enrollment.user_id == user.id)).all()]


def validate_enrollment(db, body):
    course = db.get(Course, body.course_id)
    if not course:
        raise HTTPException(404, 'Course not found.')
    for course_id in body.prerequisites:
        prerequisite = db.get(Course, course_id)
        if not prerequisite or prerequisite.school != course.school or prerequisite.id == course.id:
            raise HTTPException(422, 'Select valid prerequisite courses from this school.')


@app.post('/api/courses')
def add_course(body: EnrollmentInput, user=Depends(current_user), db=Depends(get_db)):
    validate_enrollment(db, body)
    existing = db.scalar(select(Enrollment).where(Enrollment.user_id == user.id, Enrollment.course_id == body.course_id))
    if existing:
        return enrollment_dict(db, existing)
    if db.scalar(select(func.count()).select_from(Enrollment).where(Enrollment.user_id == user.id)) >= 12:
        raise HTTPException(422, 'The beta supports up to 12 active courses.')
    item = Enrollment(user_id=user.id, **body.model_dump())
    db.add(item)
    db.commit()
    return enrollment_dict(db, item)


@app.put('/api/courses/{enrollment_id}')
def edit_course(enrollment_id: str, body: EnrollmentInput, user=Depends(current_user), db=Depends(get_db)):
    item = owned_enrollment(db, user.id, enrollment_id)
    if body.course_id != item.course_id:
        raise HTTPException(422, 'The course cannot be changed. Add a new course instead.')
    validate_enrollment(db, body)
    if db.scalar(select(Assignment.id).where(Assignment.enrollment_id == item.id, Assignment.status.in_(['queued', 'running']))):
        raise HTTPException(409, 'Wait for the assignment to finish before changing its source settings.')
    for key, value in body.model_dump().items():
        setattr(item, key, value)
    db.commit()
    return enrollment_dict(db, item)


@app.get('/api/materials/{enrollment_id}')
def materials(enrollment_id: str, user=Depends(current_user), db=Depends(get_db)):
    owned_enrollment(db, user.id, enrollment_id)
    docs = db.scalars(select(Document).where(Document.user_id == user.id, Document.enrollment_id == enrollment_id)).all()
    return [{'id': d.id, 'name': d.name, 'size': d.size, 'pages': d.pages, 'created_at': d.created_at.isoformat() + 'Z'} for d in docs]


async def read_upload(file):
    data = await file.read(settings.max_upload_bytes + 1)
    await file.close()
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(413, 'Files must be 10 MB or smaller.')
    return data


def stored_upload_bytes(db, user_id):
    materials = db.scalar(select(func.coalesce(func.sum(Document.size), 0)).where(Document.user_id == user_id))
    assignments = db.scalar(select(func.coalesce(func.sum(AssignmentFile.size), 0)).join(
        Assignment, Assignment.id == AssignmentFile.assignment_id).where(Assignment.user_id == user_id))
    return materials + assignments


@app.post('/api/materials')
async def upload_material(enrollment_id: str = Form(...), file: UploadFile = File(...), user=Depends(current_user), db=Depends(get_db)):
    owned_enrollment(db, user.id, enrollment_id)
    limiter.check('upload:' + user.id, 10)
    data = await read_upload(file)
    size = stored_upload_bytes(db, user.id)
    if size + len(data) > settings.max_storage_bytes:
        raise HTTPException(413, 'Your beta upload allowance is 100 MB. Remove unused materials or assignments to make room.')
    chunks, pages = await asyncio.to_thread(extract_file, file.filename or 'upload', data)
    item = Document(user_id=user.id, enrollment_id=enrollment_id, name=Path(file.filename or 'upload').name[:180],
                    size=len(data), pages=pages, chunks=chunks)
    db.add(item)
    db.commit()
    return {'id': item.id, 'name': item.name, 'pages': pages}


@app.delete('/api/materials/{document_id}')
def delete_material(document_id: str, user=Depends(current_user), db=Depends(get_db)):
    item = db.scalar(select(Document).where(Document.id == document_id, Document.user_id == user.id))
    if not item:
        raise HTTPException(404, 'Material not found.')
    if db.scalar(select(Assignment.id).where(Assignment.enrollment_id == item.enrollment_id, Assignment.status.in_(['queued', 'running']))):
        raise HTTPException(409, 'Wait for your assignment to finish before removing its sources.')
    db.delete(item)
    db.commit()
    return {'ok': True}


@app.get('/api/sources/{enrollment_id}')
def sources(enrollment_id: str, q: str = '', user=Depends(current_user), db=Depends(get_db)):
    item = owned_enrollment(db, user.id, enrollment_id)
    available = pipeline.available_sources(db, item)
    return pipeline.retrieve(available, q) if q else available[:30]


@app.get('/api/chat/{enrollment_id}')
def chat_history(enrollment_id: str, user=Depends(current_user), db=Depends(get_db)):
    owned_enrollment(db, user.id, enrollment_id)
    items = db.scalars(select(Chat).where(Chat.user_id == user.id, Chat.enrollment_id == enrollment_id).order_by(Chat.created_at.desc()).limit(50)).all()
    return [{'id': c.id, 'question': c.question, 'answer': c.answer} for c in reversed(items)]


@app.post('/api/chat')
def chat(body: ChatInput, user=Depends(current_user), db=Depends(get_db)):
    enrollment = owned_enrollment(db, user.id, body.enrollment_id)
    ensure_ai()
    limiter.check('ai:' + user.id, 10)
    usage, fresh = reserve(db, user, 1, body.request_key, 'chat', body.enrollment_id + body.question)
    if not fresh:
        prior = db.get(Chat, usage.result_id) if usage.result_id else None
        if prior:
            return {'id': prior.id, 'question': prior.question, 'answer': prior.answer}
        raise HTTPException(409, 'This request is already processing or was refunded. Refresh the history or send a new request.')
    try:
        recent = db.scalars(select(Chat).where(Chat.enrollment_id == enrollment.id, Chat.user_id == user.id)
                            .order_by(Chat.created_at.desc()).limit(3)).all()
        history = [{'question': c.question, 'answer': c.answer['final_answer'][:1000]} for c in reversed(recent)]
        answer, tokens = pipeline.solve(db, enrollment, body.question, history)
        item = Chat(user_id=user.id, enrollment_id=enrollment.id, question=body.question, answer=answer)
        db.add(item)
        db.commit()
        settle(db, usage.id, 1 if answer['status'] == 'answered' else 0, item.id, *tokens)
        return {'id': item.id, 'question': item.question, 'answer': answer}
    except Exception:
        db.rollback()
        settle(db, usage.id, 0)
        raise


@app.post('/api/assignments/preview')
async def preview_assignment(enrollment_id: str = Form(...), file: UploadFile = File(...), user=Depends(current_user), db=Depends(get_db)):
    owned_enrollment(db, user.id, enrollment_id)
    limiter.check('upload:' + user.id, 10)
    if db.scalar(select(func.count()).select_from(Assignment).where(Assignment.user_id == user.id)) >= 100:
        raise HTTPException(422, 'Remove an old assignment before creating another. The beta supports 100 saved assignments.')
    data = await read_upload(file)
    chunks, _ = await asyncio.to_thread(extract_file, file.filename or 'assignment.txt', data)
    questions = await asyncio.to_thread(extract_questions, chunks)
    item = Assignment(user_id=user.id, enrollment_id=enrollment_id, title=Path(file.filename or 'Assignment').stem[:180], questions=questions)
    db.add(item)
    db.commit()
    return assignment_dict(item)


@app.post('/api/assignments/upload')
async def upload_assignment(enrollment_id: str = Form(...),
                            request_key: str = Form(..., min_length=16, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$'),
                            output_layout: Literal['rebuild', 'new', 'legacy'] = Form('rebuild'),
                            file: UploadFile = File(...), user=Depends(current_user), db=Depends(get_db)):
    enrollment = owned_enrollment(db, user.id, enrollment_id)
    data = await read_upload(file)
    original_name = Path(file.filename or 'assignment.txt').name
    suffix = Path(original_name).suffix
    name = original_name if len(original_name) <= 180 else Path(original_name).stem[:180 - len(suffix)] + suffix
    fingerprint = json.dumps(['whole-file-v1', enrollment_id, name, hashlib.sha256(data).hexdigest()] + ([] if output_layout == 'legacy' else [output_layout]))
    previous = db.scalar(select(Usage).where(Usage.user_id == user.id, Usage.request_key == request_key))
    if previous:
        usage, _ = reserve(db, user, 1, request_key, 'assignment', fingerprint)
        existing = db.scalar(select(Assignment).where(Assignment.usage_id == usage.id, Assignment.user_id == user.id))
        if existing:
            return assignment_dict(existing)
        raise HTTPException(409, 'This upload request already finished or was interrupted. Start a new upload.')
    ensure_ai()
    limiter.check('upload:' + user.id, 10)
    limiter.check('ai:' + user.id, 10)
    if db.scalar(select(func.count()).select_from(Assignment).where(Assignment.user_id == user.id)) >= 100:
        raise HTTPException(422, 'Remove an old assignment before creating another. The beta supports 100 saved assignments.')
    if db.scalar(select(func.count()).select_from(Assignment).where(Assignment.user_id == user.id, Assignment.status.in_(['queued', 'running']))) >= 2:
        raise HTTPException(429, 'You can queue up to two assignments at a time.')
    if stored_upload_bytes(db, user.id) + len(data) > settings.max_storage_bytes:
        raise HTTPException(413, 'Your beta upload allowance is 100 MB. Remove unused materials or assignments to make room.')
    mime, pages = await asyncio.to_thread(validate_assignment_file, original_name, data)
    snapshot = pipeline.available_sources(db, enrollment)
    if output_layout == 'legacy':
        pipeline.check_assignment_sources(snapshot)
    elif sum(len(s['text']) for s in snapshot) > 400_000:
        raise HTTPException(413, 'The course materials exceed the document context limit. Remove unused chapters and try again.')
    refresh_allowance(db, user)
    budget = min(settings.max_questions, user.credits + user.topup_credits)
    if not budget:
        raise HTTPException(402, 'You have reached your response allowance. Your usage page shows when it resets.')
    usage, fresh = reserve(db, user, budget, request_key, 'assignment', fingerprint)
    if not fresh:
        existing = db.scalar(select(Assignment).where(Assignment.usage_id == usage.id, Assignment.user_id == user.id))
        if existing:
            return assignment_dict(existing)
        raise HTTPException(409, 'This upload is already being submitted. Refresh the assignment list.')
    try:
        item = Assignment(user_id=user.id, enrollment_id=enrollment_id, title=Path(name).stem[:180],
                          questions=[], source_snapshot=snapshot, usage_id=usage.id, status='queued')
        item.file = AssignmentFile(name=name, mime=mime, data=data, size=len(data), pages=pages, student_name=user.name)
        if output_layout != 'legacy':
            item.output = AssignmentOutput(layout=output_layout)
        db.add(item)
        db.commit()
        return assignment_dict(item)
    except Exception:
        db.rollback()
        settle(db, usage.id, 0)
        raise


@app.post('/api/assignments/{assignment_id}/retry')
def retry_assignment(assignment_id: str, body: AssignmentRetry, user=Depends(current_user), db=Depends(get_db)):
    item = db.scalar(select(Assignment).where(Assignment.id == assignment_id, Assignment.user_id == user.id))
    if not item:
        raise HTTPException(404, 'Assignment not found.')
    if not item.file:
        raise HTTPException(422, 'The original file was not saved for this older assignment. Please upload it again.')
    fingerprint = json.dumps(['retry-whole-file-v1', item.id])
    previous = db.scalar(select(Usage).where(Usage.user_id == user.id, Usage.request_key == body.request_key))
    if previous:
        usage, _ = reserve(db, user, 1, body.request_key, 'assignment', fingerprint)
        if item.usage_id == usage.id or usage.result_id == item.id:
            return assignment_dict(item)
        raise HTTPException(409, 'This retry request was already refunded or interrupted. Please try again.')
    if item.status in ('queued', 'running'):
        return assignment_dict(item)
    if item.status != 'failed':
        raise HTTPException(409, 'Only stopped assignments can be retried.')
    ensure_ai()
    limiter.check('ai:' + user.id, 10)
    if db.scalar(select(func.count()).select_from(Assignment).where(Assignment.user_id == user.id, Assignment.status.in_(['queued', 'running']))) >= 2:
        raise HTTPException(429, 'You can queue up to two assignments at a time.')
    snapshot = pipeline.available_sources(db, db.get(Enrollment, item.enrollment_id))
    if not item.output:
        pipeline.check_assignment_sources(snapshot)
    elif sum(len(s['text']) for s in snapshot) > 400_000:
        raise HTTPException(413, 'The course materials exceed the document context limit. Remove unused chapters and try again.')
    old_usage = db.get(Usage, item.usage_id) if item.usage_id else None
    if old_usage and old_usage.status == 'reserved':
        settle(db, old_usage.id, 0, item.id, old_usage.input_tokens, old_usage.output_tokens)
    refresh_allowance(db, user)
    db.refresh(user)
    budget = min(settings.max_questions, user.credits + user.topup_credits)
    if not budget:
        raise HTTPException(402, 'You have reached your response allowance. Your usage page shows when it resets.')
    usage, fresh = reserve(db, user, budget, body.request_key, 'assignment', fingerprint)
    if not fresh:
        db.refresh(item)
        return assignment_dict(item)
    try:
        # Reuse the private original without duplicating uploads. Only one concurrent retry wins.
        changed = db.execute(update(Assignment).where(Assignment.id == item.id, Assignment.status == 'failed').values(
            usage_id=usage.id, status='queued', error='', questions=[], results=[], pdf=None, source_snapshot=snapshot))
        if changed.rowcount:
            db.execute(update(AssignmentFile).where(AssignmentFile.assignment_id == item.id).values(stage='queued'))
        db.commit()
        if not changed.rowcount:
            settle(db, usage.id, 0)
        db.refresh(item)
        return assignment_dict(item)
    except Exception:
        db.rollback()
        settle(db, usage.id, 0)
        raise


def checked_questions(questions):
    questions = [q.strip() for q in questions]
    if any(not q or len(q) > 12000 for q in questions) or sum(map(len, questions)) > 45000:
        raise HTTPException(422, 'Each question must contain 1–12,000 characters; total assignment text is limited to 45,000.')
    return questions


@app.put('/api/assignments/{assignment_id}')
def save_assignment(assignment_id: str, body: AssignmentDraft, user=Depends(current_user), db=Depends(get_db)):
    item = db.scalar(select(Assignment).where(Assignment.id == assignment_id, Assignment.user_id == user.id))
    if not item:
        raise HTTPException(404, 'Assignment not found.')
    changed = db.execute(update(Assignment).where(Assignment.id == item.id, Assignment.status == 'draft')
                         .values(questions=checked_questions(body.questions)))
    if not changed.rowcount:
        raise HTTPException(409, 'Only assignment drafts can be edited.')
    db.commit()
    db.refresh(item)
    return assignment_dict(item)


@app.post('/api/assignments/{assignment_id}/run')
def run_assignment(assignment_id: str, body: AssignmentRun, user=Depends(current_user), db=Depends(get_db)):
    item = db.scalar(select(Assignment).where(Assignment.id == assignment_id, Assignment.user_id == user.id))
    if not item:
        raise HTTPException(404, 'Assignment not found.')
    if item.status != 'draft':
        return assignment_dict(item)
    ensure_ai()
    limiter.check('ai:' + user.id, 10)
    if db.scalar(select(func.count()).select_from(Assignment).where(Assignment.user_id == user.id, Assignment.status.in_(['queued', 'running']))) >= 2:
        raise HTTPException(429, 'You can queue up to two assignments at a time.')
    questions = checked_questions(body.questions)
    usage, fresh = reserve(db, user, len(questions), body.request_key, 'assignment', assignment_id + json.dumps(questions))
    if not fresh:
        raise HTTPException(409, 'This request was already submitted. Refresh the assignment list.')
    # A conditional update prevents two concurrent runs of the same assignment.
    changed = db.execute(update(Assignment).where(Assignment.id == item.id, Assignment.status == 'draft').values(
        questions=questions, usage_id=usage.id, status='queued', source_snapshot=pipeline.available_sources(db, db.get(Enrollment, item.enrollment_id))))
    db.commit()
    if not changed.rowcount:
        settle(db, usage.id, 0)
    db.refresh(item)
    return assignment_dict(item)


@app.get('/api/assignments')
def assignments(user=Depends(current_user), db=Depends(get_db)):
    return [assignment_dict(a, False) for a in db.scalars(select(Assignment).where(Assignment.user_id == user.id).order_by(Assignment.created_at.desc())).all()]


@app.get('/api/assignments/{assignment_id}')
def assignment(assignment_id: str, user=Depends(current_user), db=Depends(get_db)):
    item = db.scalar(select(Assignment).where(Assignment.id == assignment_id, Assignment.user_id == user.id))
    if not item:
        raise HTTPException(404, 'Assignment not found.')
    return assignment_dict(item)


@app.get('/api/assignments/{assignment_id}/download')
def download_assignment(assignment_id: str, format: str = 'pdf', preview: bool = False, user=Depends(current_user), db=Depends(get_db)):
    item = db.scalar(select(Assignment).where(Assignment.id == assignment_id, Assignment.user_id == user.id))
    if not item or not item.pdf:
        raise HTTPException(404, 'The completed document is not ready yet.')
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '-', item.title)[:80] or 'assignment'
    if format not in ('pdf', 'md', 'docx', 'tex', 'latex'):
        raise HTTPException(422, 'Choose PDF, Markdown, Word, or LaTeX format.')
    if item.output:
        if format == 'latex':
            from .typesetting import latex_project
            body = latex_project(item.output.latex, item.file.data if item.output.include_original else None)
            return Response(body, media_type='application/zip', headers={'Content-Disposition': f'attachment; filename="{safe_name}-latex.zip"'})
        body, mime, extension = {
            'pdf': (item.pdf, 'application/pdf', 'pdf'),
            'md': (item.output.markdown, 'text/markdown', 'md'),
            'tex': (item.output.latex, 'application/x-tex', 'tex'),
            'docx': (item.output.docx, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'docx')
        }[format]
        disposition = 'inline' if format == 'pdf' and preview else 'attachment'
        return Response(body, media_type=mime, headers={'Content-Disposition': f'{disposition}; filename="{safe_name}-submission.{extension}"'})
    if format in ('tex', 'latex'):
        raise HTTPException(422, 'Upload this assignment again to generate LaTeX output.')
    if item.file:
        from .documents import submission_markdown, submission_docx
        course = db.get(Course, db.get(Enrollment, item.enrollment_id).course_id)
        suffix = 'submission' if item.status == 'completed' else 'incomplete-draft'
        if format == 'md':
            body = submission_markdown(item.title, course, item.results, item.file.student_name)
            return Response(body, media_type='text/markdown', headers={'Content-Disposition': f'attachment; filename="{safe_name}-{suffix}.md"'})
        if format == 'docx':
            body = submission_docx(item.title, course, item.results, item.file.student_name)
            return Response(body, media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                            headers={'Content-Disposition': f'attachment; filename="{safe_name}-{suffix}.docx"'})
        return Response(item.pdf, media_type='application/pdf', headers={'Content-Disposition': f'attachment; filename="{safe_name}-{suffix}.pdf"'})
    if format == 'docx':
        raise HTTPException(422, 'Word export is available for new whole-file uploads. Re-upload this assignment to use it.')
    if format == 'md':
        blocks = [f'# {item.title}\n\nGenerated with Confine.\n']
        for index, result in enumerate(item.results, 1):
            answer = result['answer']
            blocks.append(f'## Question {index}\n\n{result["question"]}\n\n{answer["summary"]}')
            blocks.extend(f'{n}. {s["explanation"]} [{", ".join(s["source_ids"])}]' for n, s in enumerate(answer['steps'], 1))
            blocks.append('\nAnswer: ' + answer['final_answer'] if answer['status'] == 'answered' else '\nNeeds supporting material.')
            blocks.extend(f'\nSource [{s["id"]}]: {s["name"]}, page {s["page"]}\n{s["text"]}' for s in answer.get('sources', []))
        return Response('\n\n'.join(blocks), media_type='text/markdown', headers={'Content-Disposition': f'attachment; filename="{safe_name}-solutions.md"'})
    return Response(item.pdf, media_type='application/pdf', headers={'Content-Disposition': f'attachment; filename="{safe_name}-solutions.pdf"'})


@app.delete('/api/assignments/{assignment_id}')
def delete_assignment(assignment_id: str, user=Depends(current_user), db=Depends(get_db)):
    item = db.scalar(select(Assignment).where(Assignment.id == assignment_id, Assignment.user_id == user.id))
    if not item:
        raise HTTPException(404, 'Assignment not found.')
    if item.status in ('queued', 'running'):
        raise HTTPException(409, 'Wait for this assignment to finish before deleting it.')
    db.delete(item)
    db.commit()
    return {'ok': True}


@app.get('/api/usage')
def usage(user=Depends(current_user), db=Depends(get_db)):
    refresh_allowance(db, user)
    entries = db.scalars(select(Usage).where(Usage.user_id == user.id).order_by(Usage.created_at.desc()).limit(50)).all()
    return {'account': user_dict(db, user), 'entries': [{'id': u.id, 'operation': u.operation, 'consumed': u.consumed,
            'status': u.status, 'input_tokens': u.input_tokens, 'output_tokens': u.output_tokens,
            'created_at': u.created_at.isoformat() + 'Z'} for u in entries]}


@app.post('/api/billing/checkout')
def checkout(topup: bool = False, user=Depends(current_user), db=Depends(get_db)):
    return billing.checkout(db, user, topup)


@app.post('/api/billing/portal')
def billing_portal(user=Depends(current_user)):
    return billing.portal(user)


@app.post('/api/billing/webhook')
async def stripe_webhook(request: Request, db=Depends(get_db)):
    if not settings.billing_ready:
        raise HTTPException(503, 'Billing is disabled.')
    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(payload, request.headers.get('stripe-signature', ''), settings.stripe_webhook)
    except (ValueError, stripe.SignatureVerificationError):
        raise HTTPException(400, 'Invalid payment event.') from None
    billing.handle_event(db, event)
    return {'received': True}


app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')


@app.get('/')
def index():
    return FileResponse(ROOT / 'static' / 'index.html')
