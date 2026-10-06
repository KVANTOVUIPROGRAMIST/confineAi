import asyncio
import logging

from fastapi import HTTPException
from sqlalchemy import select, update

from .credits import settle
from .db import SessionLocal
from .documents import render_assignment, render_submission
from .models import Assignment, Course, Enrollment, Usage
from .pipeline import solve, solve_whole_assignment

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
            if assignment.file:
                def stage(value):
                    assignment.file.stage = value
                    db.commit()
                def record_tokens(tokens):
                    # Keep owner-cost usage even if coverage/source review later rejects the document.
                    usage.input_tokens += tokens[0]
                    usage.output_tokens += tokens[1]
                    db.commit()
                stage('reading')
                results, tokens = solve_whole_assignment(db, enrollment, assignment.file,
                    assignment.source_snapshot, usage.monthly_reserved + usage.topup_reserved, on_stage=stage, on_tokens=record_tokens)
                input_tokens += tokens[0]
                output_tokens += tokens[1]
                usage.input_tokens, usage.output_tokens = input_tokens, output_tokens
                # Export only after checking coverage against the original file and source support.
                assignment.results = results
                assignment.questions = [r['question'] for r in results]
                stage('rendering')
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
                    db.commit()
                assignment.pdf = render_assignment(assignment.title, course, assignment.results)
            assignment.status = 'completed' if all(r['answer']['status'] == 'answered' for r in assignment.results) else 'partial'
            db.commit()
        except Exception as exc:
            db.rollback()
            assignment = db.get(Assignment, assignment_id)
            assignment.status = 'failed'
            if assignment.file:
                assignment.file.stage = 'failed'
                assignment.pdf = None
                assignment.results = []
                db.refresh(usage)
                input_tokens, output_tokens = usage.input_tokens, usage.output_tokens
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
                changed = db.execute(update(Assignment).where(Assignment.id == pending.id, Assignment.status == 'queued').values(status='running'))
                db.commit()
                if not changed.rowcount:
                    assignment_id = None
        if assignment_id:
            await asyncio.to_thread(process_assignment, assignment_id)
        else:
            await asyncio.sleep(1)
