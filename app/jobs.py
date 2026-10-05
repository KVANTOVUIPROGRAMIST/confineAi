import asyncio
import logging

from fastapi import HTTPException
from sqlalchemy import select, update

from .credits import settle
from .db import SessionLocal
from .documents import render_assignment
from .models import Assignment, Course, Enrollment, Usage
from .pipeline import solve

log = logging.getLogger(__name__)


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


def process_assignment(assignment_id):
    with SessionLocal() as db:
        assignment = db.get(Assignment, assignment_id)
        if not assignment or assignment.status != 'running':
            return
        enrollment = db.get(Enrollment, assignment.enrollment_id)
        course = db.get(Course, enrollment.course_id)
        usage = db.get(Usage, assignment.usage_id)
        input_tokens, output_tokens = usage.input_tokens, usage.output_tokens
        try:
            for index, question in enumerate(assignment.questions):
                if index < len(assignment.results):
                    continue
                answer, tokens = solve(db, enrollment, question, source_snapshot=assignment.source_snapshot)
                input_tokens += tokens[0]
                output_tokens += tokens[1]
                usage.input_tokens, usage.output_tokens = input_tokens, output_tokens
                assignment.results = [*assignment.results, {'question': question, 'answer': answer}]
                db.commit()
            assignment.pdf = render_assignment(assignment.title, course, assignment.results)
            assignment.status = 'completed' if all(r['answer']['status'] == 'answered' for r in assignment.results) else 'partial'
            db.commit()
        except Exception as exc:
            db.rollback()
            assignment = db.get(Assignment, assignment_id)
            assignment.status = 'failed'
            assignment.error = exc.detail if isinstance(exc, HTTPException) else 'The assignment could not finish. Unused responses have been restored.'
            db.commit()
            # Keep provider errors out of logs; they can contain sensitive input.
            log.warning('Assignment %s failed (%s)', assignment_id, type(exc).__name__)
        consumed = sum(r['answer']['status'] == 'answered' for r in assignment.results)
        settle(db, assignment.usage_id, consumed, assignment.id, input_tokens, output_tokens)


async def assignment_worker():
    while True:
        with SessionLocal() as db:
            pending = db.scalar(select(Assignment).where(Assignment.status == 'queued').order_by(Assignment.created_at))
            assignment_id = pending.id if pending else None
            if pending:
                db.execute(update(Assignment).where(Assignment.id == pending.id, Assignment.status == 'queued').values(status='running'))
                db.commit()
        if assignment_id:
            await asyncio.to_thread(process_assignment, assignment_id)
        else:
            await asyncio.sleep(1)
