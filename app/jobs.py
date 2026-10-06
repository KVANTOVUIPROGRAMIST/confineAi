import asyncio
import contextlib
import logging
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import delete, exists, select, update

from .credits import settle
from .db import SessionLocal
from .documents import render_assignment, render_submission
from .models import Assignment, AssignmentLease, Course, Enrollment, Usage, uid, utcnow
from .pipeline import solve, solve_whole_assignment
from .submissions import complete_assignment
from .typesetting import render_completed

log = logging.getLogger(__name__)


class OwnershipLost(Exception):
    pass


def recover_jobs(db):
    """Recover expired owners, including old jobs created before lease support."""
    now = utcnow()
    active = exists(select(AssignmentLease.assignment_id).where(
        AssignmentLease.assignment_id == Assignment.id, AssignmentLease.expires_at > now))
    db.execute(delete(AssignmentLease).where(AssignmentLease.expires_at <= now))
    db.execute(update(Assignment).where(Assignment.status == 'running', ~active).values(status='queued'))
    db.commit()


def claim_job(db):
    pending = db.scalar(select(Assignment).where(Assignment.status == 'queued').order_by(Assignment.created_at))
    if not pending:
        return None
    active = exists(select(AssignmentLease.assignment_id).where(
        AssignmentLease.assignment_id == pending.id, AssignmentLease.expires_at > utcnow()))
    changed = db.execute(update(Assignment).where(Assignment.id == pending.id,
        Assignment.status == 'queued', ~active).values(status='running'))
    if not changed.rowcount:
        db.rollback()
        return None
    db.execute(delete(AssignmentLease).where(AssignmentLease.assignment_id == pending.id))
    owner = uid()
    db.add(AssignmentLease(assignment_id=pending.id, owner=owner, expires_at=utcnow() + timedelta(seconds=90)))
    db.commit()
    return pending.id, owner


def renew_lease(assignment_id, owner):
    with SessionLocal() as db:
        now = utcnow()
        changed = db.execute(update(AssignmentLease).where(AssignmentLease.assignment_id == assignment_id,
            AssignmentLease.owner == owner, AssignmentLease.expires_at > now).values(expires_at=now + timedelta(seconds=90)))
        db.commit()
        return bool(changed.rowcount)


async def heartbeat(assignment_id, owner):
    while True:
        await asyncio.sleep(15)
        try:
            if not await asyncio.to_thread(renew_lease, assignment_id, owner):
                return
        except Exception as exc:
            log.warning('Assignment heartbeat interrupted (%s)', type(exc).__name__)


def recover_reservations(db):
    """Complete settlement if shutdown happened between saving a job and its balance update."""
    pending = db.scalars(select(Usage).where(Usage.status == 'reserved', Usage.operation == 'assignment')).all()
    for usage in pending:
        assignment = db.scalar(select(Assignment).where(Assignment.usage_id == usage.id))
        if not assignment:
            settle(db, usage.id, 0)
        elif assignment.status not in ('queued', 'running'):
            consumed = sum(r['answer']['status'] == 'answered' for r in assignment.results)
            settle(db, usage.id, consumed, assignment.id, usage.input_tokens, usage.output_tokens)


def process_assignment(assignment_id, owner=None):
    with SessionLocal() as db:
        assignment = db.get(Assignment, assignment_id)
        if not assignment or assignment.status != 'running':
            return
        def guard():
            if owner:
                # Do not autoflush pending artifacts before confirming ownership.
                # The row lock serializes this commit with lease expiry/recovery.
                with db.no_autoflush:
                    valid_owner = db.scalar(select(AssignmentLease.assignment_id).where(
                        AssignmentLease.assignment_id == assignment_id, AssignmentLease.owner == owner,
                        AssignmentLease.expires_at > utcnow()).with_for_update())
                if not valid_owner:
                    raise OwnershipLost()
        try:
            guard()
            db.commit()
        except OwnershipLost:
            return
        enrollment = db.get(Enrollment, assignment.enrollment_id)
        course = db.get(Course, enrollment.course_id)
        usage = db.get(Usage, assignment.usage_id)
        input_tokens, output_tokens = usage.input_tokens, usage.output_tokens
        try:
            if assignment.file:
                def stage(value):
                    guard()
                    assignment.file.stage = value
                    db.commit()
                def record_tokens(tokens):
                    guard()
                    # Keep owner-cost usage even if coverage/source review later rejects the document.
                    usage.input_tokens += tokens[0]
                    usage.output_tokens += tokens[1]
                    db.commit()
                stage('reading')
                if assignment.output:
                    results, warnings, tokens = complete_assignment(db, enrollment, assignment.file,
                        assignment.source_snapshot, usage.monthly_reserved + usage.topup_reserved, on_stage=stage, on_tokens=record_tokens)
                    assignment.output.warnings = warnings
                else:
                    results, tokens = solve_whole_assignment(db, enrollment, assignment.file,
                        assignment.source_snapshot, usage.monthly_reserved + usage.topup_reserved, on_stage=stage, on_tokens=record_tokens)
                input_tokens += tokens[0]
                output_tokens += tokens[1]
                usage.input_tokens, usage.output_tokens = input_tokens, output_tokens
                # Export only after checking coverage against the original file and source support.
                assignment.results = results
                assignment.questions = [r['question'] for r in results]
                stage('rendering')
                if assignment.output:
                    rendered = render_completed(assignment.title, course, results, assignment.file.student_name,
                                                assignment.output.layout, assignment.file)
                    assignment.pdf, assignment.output.markdown, assignment.output.latex, assignment.output.docx, assignment.output.include_original = rendered
                else:
                    assignment.pdf = render_submission(assignment.title, course, results, assignment.file.student_name)
                assignment.file.stage = 'done'
            else:
                for index, question in enumerate(assignment.questions):
                    if index < len(assignment.results):
                        continue
                    answer, tokens = solve(db, enrollment, question, source_snapshot=assignment.source_snapshot,
                                           assignment_context=assignment.questions)
                    input_tokens += tokens[0]
                    output_tokens += tokens[1]
                    usage.input_tokens, usage.output_tokens = input_tokens, output_tokens
                    assignment.results = [*assignment.results, {'question': question, 'answer': answer}]
                    guard()
                    db.commit()
                assignment.pdf = render_assignment(assignment.title, course, assignment.results)
            assignment.status = 'completed' if all(r['answer']['status'] == 'answered' for r in assignment.results) else 'partial'
            guard()
            db.commit()
        except OwnershipLost:
            db.rollback()
            return
        except Exception as exc:
            db.rollback()
            try:
                guard()
            except OwnershipLost:
                return
            assignment = db.get(Assignment, assignment_id)
            assignment.status = 'failed'
            if assignment.file:
                assignment.file.stage = 'failed'
                assignment.pdf = None
                assignment.results = []
                if assignment.output:
                    assignment.output.markdown = assignment.output.latex = ''
                    assignment.output.docx = None
                    assignment.output.warnings = []
                db.refresh(usage)
                input_tokens, output_tokens = usage.input_tokens, usage.output_tokens
            assignment.error = exc.detail if isinstance(exc, HTTPException) else 'The assignment could not finish. Unused responses have been restored.'
            db.commit()
            # Keep provider errors out of logs; they can contain sensitive input.
            log.warning('Assignment %s failed (%s)', assignment_id, type(exc).__name__)
        consumed = sum(r['answer']['status'] == 'answered' for r in assignment.results)
        settle(db, assignment.usage_id, consumed, assignment.id, input_tokens, output_tokens)
        if owner:
            db.execute(delete(AssignmentLease).where(AssignmentLease.assignment_id == assignment_id, AssignmentLease.owner == owner))
            db.commit()


async def assignment_worker():
    while True:
        with SessionLocal() as db:
            recover_jobs(db)
            claimed = claim_job(db)
        if claimed:
            assignment_id, owner = claimed
            pulse = asyncio.create_task(heartbeat(assignment_id, owner))
            try:
                await asyncio.to_thread(process_assignment, assignment_id, owner)
            finally:
                pulse.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pulse
                # A cancelled owner's thread may still finish an HTTP call. Fence
                # its later writes before another process resumes the saved job.
                with SessionLocal() as db:
                    db.execute(update(AssignmentLease).where(AssignmentLease.assignment_id == assignment_id,
                        AssignmentLease.owner == owner).values(expires_at=utcnow()))
                    db.commit()
        else:
            await asyncio.sleep(1)
