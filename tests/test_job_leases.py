from datetime import timedelta

from sqlalchemy import select, update

from app import jobs
from app.db import SessionLocal
from app.models import Assignment, AssignmentLease, Usage, utcnow
from conftest import enroll
from test_completed_documents import model, upload


def test_live_owner_survives_recovery_and_expired_owner_can_be_replaced(client, student, monkeypatch):
    course = enroll(client)
    model(monkeypatch)
    item = upload(client, course).json()
    with SessionLocal() as db:
        assignment_id, owner = jobs.claim_job(db)
        assert assignment_id == item['id'] and jobs.claim_job(db) is None
        jobs.recover_jobs(db)
        db.expire_all()
        assert db.get(Assignment, assignment_id).status == 'running'
        assert db.get(AssignmentLease, assignment_id).owner == owner
        db.execute(update(AssignmentLease).values(expires_at=utcnow() - timedelta(seconds=1)))
        db.commit()
        jobs.recover_jobs(db)
        db.expire_all()
        assert db.get(Assignment, assignment_id).status == 'queued'
        _, replacement = jobs.claim_job(db)
        assert replacement != owner
    assert not jobs.renew_lease(assignment_id, owner)
    assert jobs.renew_lease(assignment_id, replacement)


def test_retired_owner_cannot_flush_artifacts_or_settle_resumed_job(client, student, monkeypatch):
    course = enroll(client)
    model(monkeypatch)
    item = upload(client, course).json()
    with SessionLocal() as db:
        assignment_id, owner = jobs.claim_job(db)
    def interrupted_render(*args):
        with SessionLocal() as db:
            db.execute(update(AssignmentLease).where(AssignmentLease.assignment_id == assignment_id).values(owner='replacement'))
            db.commit()
        return b'%PDF-retired', '# retired', 'retired', b'retired', False
    monkeypatch.setattr(jobs, 'render_completed', interrupted_render)
    jobs.process_assignment(assignment_id, owner)
    with SessionLocal() as db:
        current = db.get(Assignment, assignment_id)
        assert current.status == 'running' and current.pdf is None
        usage = db.get(Usage, current.usage_id)
        assert usage.status == 'reserved' and usage.consumed == 0
        assert usage.input_tokens == 30
    monkeypatch.setattr(jobs, 'render_completed', lambda *args: (b'%PDF-current', '# current', 'current', b'current', False))
    model(monkeypatch)
    jobs.process_assignment(assignment_id, 'replacement')
    assert client.get('/api/auth/me').json()['credits'] == 299
    with SessionLocal() as db:
        current = db.get(Assignment, assignment_id)
        assert current.status == 'completed' and current.pdf == b'%PDF-current'
        assert db.get(AssignmentLease, assignment_id) is None
        usage = db.get(Usage, current.usage_id)
        assert usage.consumed == 1 and usage.input_tokens == 60


def test_unleased_interrupted_job_is_recovered_without_losing_reservation(client, student, monkeypatch):
    course = enroll(client)
    model(monkeypatch)
    item = upload(client, course).json()
    with SessionLocal() as db:
        db.execute(update(Assignment).where(Assignment.id == item['id']).values(status='running'))
        db.commit()
        jobs.recover_jobs(db)
        db.expire_all()
        current = db.get(Assignment, item['id'])
        assert current.status == 'queued'
        assert db.get(Usage, current.usage_id).status == 'reserved'
        assert jobs.claim_job(db)[0] == item['id']


def test_startup_does_not_steal_a_live_owner(client, student, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    course = enroll(client)
    model(monkeypatch)
    item = upload(client, course).json()
    with SessionLocal() as db:
        assignment_id, owner = jobs.claim_job(db)
    with TestClient(app):
        with SessionLocal() as db:
            assert db.get(Assignment, assignment_id).status == 'running'
            assert db.get(AssignmentLease, assignment_id).owner == owner
