from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Step(StrictModel):
    explanation: str
    source_ids: list[str]


class Answer(StrictModel):
    status: Literal['answered', 'needs_materials']
    summary: str
    steps: list[Step]
    final_answer: str
    concepts: list[str]
    source_ids: list[str]


class Verification(StrictModel):
    supported: bool
    concerns: list[str]


class AssignmentSection(StrictModel):
    label: str
    question: str
    answer: Answer


class WholeAssignment(StrictModel):
    sections: list[AssignmentSection] = Field(min_length=1, max_length=30)


class SectionVerification(StrictModel):
    index: int
    supported: bool
    concerns: list[str]


class WholeVerification(StrictModel):
    coverage_complete: bool
    concerns: list[str]
    sections: list[SectionVerification]


class Questions(StrictModel):
    questions: list[str]


class Transcription(StrictModel):
    pages: list[str]


class AuthInput(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=10, max_length=128)
    name: str = Field(default='', max_length=80)


class EnrollmentInput(BaseModel):
    course_id: str
    prerequisites: list[str] = Field(default_factory=list, max_length=20)
    mode: Literal['catalog', 'strict'] = 'strict'
    term: str = Field(default='', max_length=80)
    instructor: str = Field(default='', max_length=100)
    topic: str = Field(default='', max_length=200)


class ChatInput(BaseModel):
    enrollment_id: str
    question: str = Field(min_length=2, max_length=5000)
    request_key: str = Field(min_length=16, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$')


class AssignmentDraft(BaseModel):
    questions: list[str] = Field(min_length=1, max_length=30)


class AssignmentRun(AssignmentDraft):
    request_key: str = Field(min_length=16, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$')


class AssignmentRetry(StrictModel):
    request_key: str = Field(min_length=16, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$')
