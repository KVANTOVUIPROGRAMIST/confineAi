"""One-time removal of the agent's abandoned disposable deployment verification."""
import logging
from sqlalchemy import delete
from .models import Assignment, AssignmentLease, Chat, Document, Enrollment, LoginSession, Usage, User


def cleanup_abandoned_verification(db):
    item = db.get(Assignment, 'b871efdb862f4332ba91b7a4db522242')
    if not item or db.get(AssignmentLease, item.id):
        return
    user = db.get(User, item.user_id)
    if not user or user.name != 'HW1 verification' or not user.email.startswith('hw1-qa-') or not user.email.endswith('@example.test'):
        return
    for model in (Assignment, Chat, Document, Enrollment, LoginSession, Usage):
        db.execute(delete(model).where(model.user_id == user.id))
    db.delete(user)
    db.commit()
    logging.getLogger('uvicorn.error').info('Removed the abandoned disposable deployment verification account.')
