import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship, column_property

from .db import Base


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def uid():
    return uuid.uuid4().hex


class User(Base):
    __tablename__ = 'users'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    password_hash: Mapped[str] = mapped_column(Text)
    tier: Mapped[str] = mapped_column(String(20), default='beta')
    credits: Mapped[int] = mapped_column(Integer, default=300)
    topup_credits: Mapped[int] = mapped_column(Integer, default=0)
    period: Mapped[str] = mapped_column(String(40))
    stripe_customer: Mapped[str | None] = mapped_column(String(100), nullable=True)
    subscription_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LoginSession(Base):
    __tablename__ = 'login_sessions'
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    csrf: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class Course(Base):
    __tablename__ = 'courses'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    school: Mapped[str] = mapped_column(String(32), index=True)
    code: Mapped[str] = mapped_column(String(30), index=True)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    prerequisite_text: Mapped[str] = mapped_column(Text, default='')
    prerequisite_codes: Mapped[list] = mapped_column(JSON, default=list)
    source_url: Mapped[str] = mapped_column(Text)
    catalog_year: Mapped[str] = mapped_column(String(20), default='2026-27')
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Enrollment(Base):
    __tablename__ = 'enrollments'
    __table_args__ = (UniqueConstraint('user_id', 'course_id'),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    course_id: Mapped[str] = mapped_column(ForeignKey('courses.id'))
    prerequisites: Mapped[list] = mapped_column(JSON, default=list)
    mode: Mapped[str] = mapped_column(String(20), default='strict')
    term: Mapped[str] = mapped_column(String(80), default='')
    instructor: Mapped[str] = mapped_column(String(100), default='')
    topic: Mapped[str] = mapped_column(String(200), default='')


class Document(Base):
    __tablename__ = 'documents'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    enrollment_id: Mapped[str] = mapped_column(ForeignKey('enrollments.id', ondelete='CASCADE'), index=True)
    name: Mapped[str] = mapped_column(String(180))
    kind: Mapped[str] = mapped_column(String(20), default='material')
    size: Mapped[int] = mapped_column(Integer)
    pages: Mapped[int] = mapped_column(Integer, default=1)
    chunks: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Chat(Base):
    __tablename__ = 'chats'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    enrollment_id: Mapped[str] = mapped_column(ForeignKey('enrollments.id', ondelete='CASCADE'), index=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Usage(Base):
    __tablename__ = 'usage'
    __table_args__ = (UniqueConstraint('user_id', 'request_key'),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    request_key: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    operation: Mapped[str] = mapped_column(String(30))
    period: Mapped[str] = mapped_column(String(40))
    monthly_reserved: Mapped[int] = mapped_column(Integer)
    topup_reserved: Mapped[int] = mapped_column(Integer, default=0)
    consumed: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default='reserved')
    result_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Assignment(Base):
    __tablename__ = 'assignments'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    enrollment_id: Mapped[str] = mapped_column(ForeignKey('enrollments.id', ondelete='CASCADE'))
    title: Mapped[str] = mapped_column(String(180))
    questions: Mapped[list] = mapped_column(JSON)
    source_snapshot: Mapped[list] = mapped_column(JSON, default=list)
    results: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default='draft')
    error: Mapped[str] = mapped_column(Text, default='')
    usage_id: Mapped[str | None] = mapped_column(ForeignKey('usage.id'), nullable=True)
    pdf: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True, deferred=True)
    pdf_ready: Mapped[bool] = column_property(pdf.is_not(None))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    file: Mapped['AssignmentFile | None'] = relationship(cascade='all, delete-orphan', uselist=False)
    output: Mapped['AssignmentOutput | None'] = relationship(cascade='all, delete-orphan', uselist=False)


class AssignmentOutput(Base):
    """Versioned output settings/artifacts; additive table leaves existing jobs intact."""
    __tablename__ = 'assignment_outputs'
    assignment_id: Mapped[str] = mapped_column(ForeignKey('assignments.id', ondelete='CASCADE'), primary_key=True)
    layout: Mapped[str] = mapped_column(String(20), default='rebuild')
    markdown: Mapped[str] = mapped_column(Text, default='', deferred=True)
    latex: Mapped[str] = mapped_column(Text, default='', deferred=True)
    docx: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True, deferred=True)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    include_original: Mapped[bool] = mapped_column(Boolean, default=False)


class AssignmentFile(Base):
    """Original private upload; a separate table keeps existing deployments migration-safe."""
    __tablename__ = 'assignment_files'
    assignment_id: Mapped[str] = mapped_column(ForeignKey('assignments.id', ondelete='CASCADE'), primary_key=True)
    name: Mapped[str] = mapped_column(String(180))
    mime: Mapped[str] = mapped_column(String(80))
    data: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    size: Mapped[int] = mapped_column(Integer)
    pages: Mapped[int] = mapped_column(Integer)
    student_name: Mapped[str] = mapped_column(String(80), default='')
    stage: Mapped[str] = mapped_column(String(20), default='queued')


class BillingEvent(Base):
    __tablename__ = 'billing_events'
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
