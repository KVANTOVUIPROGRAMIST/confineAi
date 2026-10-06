"""Regression checks for answering original uploads without a parsing pass."""
import io
import json
import base64
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from pypdf import PdfWriter
from pypdf import PdfReader
from sqlalchemy import select

from app import pipeline, providers
from app.config import settings
from app.assignment_files import validate_assignment_file
from app.credits import settle
from app.db import SessionLocal
from app.jobs import process_assignment
from app.models import Assignment, AssignmentFile, Enrollment, Usage, User
from app.schemas import Answer, AssignmentSection, SectionVerification, Step, WholeAssignment, WholeVerification
from conftest import enroll, register


ASSIGNMENT_TEXT = (
    'Homework 4\nShared experiment: flip a fair coin independently three times.\n'
    '4(a). Let X count the heads. Find P(X=2) and show your working.\n'
    '4(b). For the same experiment, find E[X] and show your working.\n'
)


def whole_draft():
    def answer(final):
        return Answer(status='answered', summary='Use the documented binomial model.',
                      steps=[Step(explanation=final, source_ids=['ref-binomial'])],
                      final_answer=final, concepts=['binomial'], source_ids=['ref-binomial'])
    return WholeAssignment(sections=[
        AssignmentSection(label='4(a)', question='Find P(X=2) for the shared experiment.',
                          answer=answer('P(X=2) = 3 * (0.5)^2 * 0.5 = 0.375.')),
        AssignmentSection(label='4(b)', question='For the same experiment, find E[X].',
                          answer=answer('E[X] = 3 * 0.5 = 1.5.')),
    ])


def whole_review(**kwargs):
    return WholeVerification(coverage_complete=kwargs.pop('coverage_complete', True), concerns=[],
                             sections=kwargs.pop('sections', [
                                 SectionVerification(index=0, supported=True, concerns=[]),
                                 SectionVerification(index=1, supported=True, concerns=[]),
                             ]), **kwargs)


def mock_whole_model(monkeypatch, draft=None, review=None):
    calls = []
    def generate(schema, instructions, content, **kwargs):
        calls.append({'schema': schema, 'context': json.loads(content), **kwargs})
        if schema is WholeAssignment:
            return draft or whole_draft(), (100, 50)
        if schema is WholeVerification:
            return review or whole_review(), (120, 20)
        raise AssertionError(f'Unexpected extraction or tutoring call: {schema}')
    monkeypatch.setattr(providers, 'generate', generate)
    return calls


def solve_file(course, file, budget=30, sources=None, on_stage=None, on_tokens=None):
    with SessionLocal() as db:
        enrollment = db.get(Enrollment, course['id'])
        if sources is None:
            sources = pipeline.available_sources(db, enrollment)
        return pipeline.solve_whole_assignment(db, enrollment, file, sources, budget, on_stage, on_tokens)


def text_file():
    return SimpleNamespace(name='homework-4.txt', mime='text/plain', pages=1, data=ASSIGNMENT_TEXT.encode())


def scanned_pdf():
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    writer.add_blank_page(width=600, height=800)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def test_whole_text_and_shared_preamble_reach_generation_and_review(client, student, monkeypatch):
    course = enroll(client)
    calls = mock_whole_model(monkeypatch)
    stages = []
    results, tokens = solve_file(course, text_file(), on_stage=stages.append)
    assert [call['schema'] for call in calls] == [WholeAssignment, WholeVerification]
    assert all(call['context']['assignment_file']['text'] == ASSIGNMENT_TEXT for call in calls)
    assert all(call['attachment'] is None for call in calls)
    assert calls[0]['context']['approved_evidence'] == calls[1]['context']['approved_evidence']
    assert [r['label'] for r in results] == ['4(a)', '4(b)']
    assert all(r['answer']['verification'] == 'checked' for r in results)
    assert tokens == (220, 70)
    assert stages == ['checking']


def test_scanned_pdf_reaches_both_model_calls_as_original_bytes(client, student, monkeypatch):
    course = enroll(client)
    calls = mock_whole_model(monkeypatch)
    data = scanned_pdf()
    assert validate_assignment_file('scan.pdf', data) == ('application/pdf', 2)
    file = SimpleNamespace(name='scan.pdf', mime='application/pdf', pages=2, data=data)
    solve_file(course, file)
    assert all(call['attachment'] == ('application/pdf', data) for call in calls)
    assert all('text' not in call['context']['assignment_file'] for call in calls)


@pytest.mark.parametrize('review', [
    whole_review(coverage_complete=False),
    whole_review(sections=[SectionVerification(index=0, supported=True, concerns=[])]),
    whole_review(sections=[SectionVerification(index=0, supported=True, concerns=[]),
                           SectionVerification(index=0, supported=True, concerns=[])]),
    whole_review(sections=[SectionVerification(index=0, supported=True, concerns=[]),
                           SectionVerification(index=2, supported=True, concerns=[])]),
])
def test_incomplete_or_ambiguous_coverage_fails_closed(client, student, monkeypatch, review):
    course = enroll(client)
    mock_whole_model(monkeypatch, review=review)
    with pytest.raises(HTTPException) as error:
        solve_file(course, text_file())
    assert error.value.status_code == 502


def test_unknown_citation_withholds_only_affected_section(client, student, monkeypatch):
    course = enroll(client)
    draft = whole_draft()
    draft.sections[0].answer.source_ids = ['invented-source']
    calls = mock_whole_model(monkeypatch, draft=draft)
    results, _ = solve_file(course, text_file())
    assert results[0]['answer']['status'] == 'needs_materials'
    assert results[0]['answer']['final_answer'] == ''
    assert results[0]['answer']['sources'] == []
    assert results[1]['answer']['status'] == 'answered'
    assert calls[1]['context']['proposed_sections'][0]['answer']['status'] == 'needs_materials'


def test_semantic_rejection_withholds_unsupported_full_answer(client, student, monkeypatch):
    course = enroll(client)
    review = whole_review(sections=[
        SectionVerification(index=0, supported=False, concerns=['The method is not in the course notes.']),
        SectionVerification(index=1, supported=True, concerns=[]),
    ])
    mock_whole_model(monkeypatch, review=review)
    results, _ = solve_file(course, text_file())
    assert results[0]['answer']['status'] == 'needs_materials'
    assert results[0]['answer']['final_answer'] == ''
    assert results[1]['answer']['status'] == 'answered'


def test_positive_coverage_flag_with_missing_task_concern_fails_and_tracks_spent_tokens(client, student, monkeypatch):
    course = enroll(client)
    review = whole_review()
    review.concerns = ['A required final subpart is missing from the proposed document.']
    mock_whole_model(monkeypatch, review=review)
    recorded_tokens = []
    with pytest.raises(HTTPException) as error:
        solve_file(course, text_file(), on_tokens=recorded_tokens.append)
    assert error.value.status_code == 502
    assert recorded_tokens == [(100, 50), (120, 20), (100, 50), (120, 20)]
    assert 'required final subpart' in error.value.detail
    assert 'clearer file' not in error.value.detail


def test_positive_support_flag_with_substantive_concern_still_withholds_section(client, student, monkeypatch):
    course = enroll(client)
    review = whole_review(sections=[
        SectionVerification(index=0, supported=True, concerns=['This answer uses a method absent from the class notes.']),
        SectionVerification(index=1, supported=True, concerns=[]),
    ])
    mock_whole_model(monkeypatch, review=review)
    results, _ = solve_file(course, text_file())
    assert results[0]['answer']['status'] == 'needs_materials'
    assert results[0]['answer']['final_answer'] == ''
    assert results[1]['answer']['verification'] == 'checked'


def test_complete_document_cannot_exceed_reserved_allowance(client, student, monkeypatch):
    course = enroll(client)
    mock_whole_model(monkeypatch)
    with pytest.raises(HTTPException) as error:
        solve_file(course, text_file(), budget=1)
    assert error.value.status_code == 402


@pytest.mark.parametrize(('sources', 'status'), [
    ([], 422),
    ([{'id': 'large-source', 'text': 'x' * 400001}], 413),
])
def test_missing_or_oversize_evidence_is_never_silently_truncated(client, student, monkeypatch, sources, status):
    course = enroll(client)
    calls = mock_whole_model(monkeypatch)
    with pytest.raises(HTTPException) as error:
        solve_file(course, text_file(), sources=sources)
    assert error.value.status_code == status
    assert calls == []


def upload(client, course, key='whole-assignment-request-01', data=None, name='homework-4.txt'):
    return client.post('/api/assignments/upload',
                       data={'enrollment_id': course['id'], 'request_key': key},
                       files={'file': (name, ASSIGNMENT_TEXT.encode() if data is None else data)})


def test_direct_upload_queues_original_file_without_extraction(client, student, monkeypatch):
    monkeypatch.setattr(settings, 'gemini_key', 'fake-test-key')
    def no_extraction(*args, **kwargs):
        raise AssertionError('Whole-file uploads must not perform extraction or an AI call while queuing.')
    monkeypatch.setattr('app.main.extract_file', no_extraction)
    monkeypatch.setattr('app.main.extract_questions', no_extraction)
    monkeypatch.setattr(providers, 'generate', no_extraction)
    course = enroll(client)
    response = upload(client, course)
    assert response.status_code == 200, response.text
    item = response.json()
    assert item['whole_file'] is True
    assert item['status'] == item['stage'] == 'queued'
    assert item['questions'] == []
    assert item['submission_ready'] is False
    assert item['file'] == {'name': 'homework-4.txt', 'pages': 1, 'size': len(ASSIGNMENT_TEXT.encode())}
    assert client.get('/api/auth/me').json()['credits'] == 270
    with SessionLocal() as db:
        original = db.get(AssignmentFile, item['id'])
        assert original.data == ASSIGNMENT_TEXT.encode()
        assert original.student_name == student['name']
        assert any(s['id'] == 'ref-binomial' for s in db.get(Assignment, item['id']).source_snapshot)


def test_idempotent_upload_replays_even_when_reservation_uses_last_credits(client, student, monkeypatch):
    monkeypatch.setattr(settings, 'gemini_key', 'fake-test-key')
    course = enroll(client)
    with SessionLocal() as db:
        db.get(User, student['id']).credits = 30
        db.commit()
    first = upload(client, course)
    assert first.status_code == 200, first.text
    assert client.get('/api/auth/me').json()['credits'] == 0
    second = upload(client, course)
    assert second.status_code == 200, second.text
    assert second.json()['id'] == first.json()['id']
    assert client.get('/api/auth/me').json()['credits'] == 0
    assert len(client.get('/api/assignments').json()) == 1


def test_request_key_cannot_replay_a_different_file_or_course(client, student, monkeypatch):
    monkeypatch.setattr(settings, 'gemini_key', 'fake-test-key')
    course = enroll(client)
    assert upload(client, course).status_code == 200
    assert upload(client, course, data=b'A different assignment.').status_code == 409
    second = enroll(client, 'ucsd-cse-21')
    assert upload(client, second).status_code == 409
    assert client.get('/api/auth/me').json()['credits'] == 270


def test_raw_upload_and_assignment_remain_private_to_owner(client, student, monkeypatch):
    monkeypatch.setattr(settings, 'gemini_key', 'fake-test-key')
    course = enroll(client)
    item = upload(client, course).json()
    register(client, 'whole-file-other@example.test')
    endpoint = '/api/assignments/' + item['id']
    assert client.get(endpoint).status_code == 404
    assert client.get(endpoint + '/download?format=docx').status_code == 404
    assert client.delete(endpoint).status_code == 404
    assert client.get('/api/assignments').json() == []
    assert upload(client, course, key='foreign-file-request-01').status_code == 404


def test_scanned_pdf_upload_does_not_request_transcription(client, student, monkeypatch):
    monkeypatch.setattr(settings, 'gemini_key', 'fake-test-key')
    def no_provider(*args, **kwargs):
        raise AssertionError('Scanned assignments go straight to the whole-file job.')
    monkeypatch.setattr(providers, 'generate', no_provider)
    course = enroll(client)
    data = scanned_pdf()
    response = upload(client, course, data=data, name='scanned.pdf')
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        original = db.get(AssignmentFile, response.json()['id'])
        assert (original.mime, original.pages, original.data) == ('application/pdf', 2, data)


@pytest.mark.parametrize('extension', ['.pdf', '.txt'])
def test_long_valid_filename_retains_extension_when_stored(client, student, monkeypatch, extension):
    monkeypatch.setattr(settings, 'gemini_key', 'fake-test-key')
    course = enroll(client)
    data = scanned_pdf() if extension == '.pdf' else ASSIGNMENT_TEXT.encode()
    response = upload(client, course, name='long-assignment-name-' + 'x' * 180 + extension, data=data)
    assert response.status_code == 200, response.text
    name = response.json()['file']['name']
    assert len(name) == 180 and name.endswith(extension)
    with SessionLocal() as db:
        original = db.get(AssignmentFile, response.json()['id'])
        assert original.data == data
        assert original.mime == ('application/pdf' if extension == '.pdf' else 'text/plain')


@pytest.mark.parametrize(('name', 'data', 'status'), [
    ('empty.txt', b'', 422),
    ('invalid.txt', b'\xff\xfe\xff', 422),
    ('broken.pdf', b'%PDF-not-a-document', 422),
    ('unsupported.zip', b'not-an-assignment', 415),
])
def test_invalid_uploads_do_not_create_jobs_or_reserve_credits(client, student, monkeypatch, name, data, status):
    monkeypatch.setattr(settings, 'gemini_key', 'fake-test-key')
    course = enroll(client)
    assert upload(client, course, name=name, data=data).status_code == status
    assert client.get('/api/auth/me').json()['credits'] == 300
    assert client.get('/api/assignments').json() == []


def test_raw_assignment_storage_counts_toward_both_upload_limits(client, student, monkeypatch):
    monkeypatch.setattr(settings, 'gemini_key', 'fake-test-key')
    course = enroll(client)
    assert upload(client, course).status_code == 200
    monkeypatch.setattr(settings, 'max_storage_bytes', len(ASSIGNMENT_TEXT.encode()) + 5)
    material = client.post('/api/materials', data={'enrollment_id': course['id']},
                           files={'file': ('more-notes.txt', b'This file exceeds the remaining storage allowance.')})
    assert material.status_code == 413
    assert upload(client, course, key='whole-assignment-request-02').status_code == 413
    assert client.get('/api/auth/me').json()['credits'] == 270


@pytest.mark.parametrize('delete_account', [False, True])
def test_private_original_file_cascades_on_assignment_or_account_deletion(client, student, monkeypatch, delete_account):
    monkeypatch.setattr(settings, 'gemini_key', 'fake-test-key')
    course = enroll(client)
    item = upload(client, course).json()
    with SessionLocal() as db:
        assignment = db.get(Assignment, item['id'])
        assignment.status = 'failed'
        db.commit()
        settle(db, assignment.usage_id, 0)
    endpoint = '/api/account' if delete_account else '/api/assignments/' + item['id']
    assert client.delete(endpoint).status_code == 200
    with SessionLocal() as db:
        assert db.get(AssignmentFile, item['id']) is None
        assert db.get(Assignment, item['id']) is None


def process_queued(item):
    with SessionLocal() as db:
        db.get(Assignment, item['id']).status = 'running'
        db.commit()
    process_assignment(item['id'])


def test_whole_file_job_finishes_clean_exports_and_settles_supported_sections(client, student, monkeypatch):
    monkeypatch.setattr(settings, 'gemini_key', 'fake-test-key')
    mock_whole_model(monkeypatch)
    course = enroll(client)
    response = upload(client, course)
    assert response.status_code == 200, response.text
    item = response.json()
    assert client.get('/api/auth/me').json()['credits'] == 270
    process_queued(item)
    endpoint = '/api/assignments/' + item['id']
    finished = client.get(endpoint).json()
    assert finished['status'] == 'completed'
    assert finished['stage'] == 'done'
    assert finished['submission_ready'] is True
    assert [r['label'] for r in finished['results']] == ['4(a)', '4(b)']
    assert client.get('/api/auth/me').json()['credits'] == 298
    with SessionLocal() as db:
        usage = db.scalar(select(Usage).where(Usage.result_id == item['id']))
        assert usage.status == 'completed' and usage.consumed == 2
        assert (usage.input_tokens, usage.output_tokens) == (220, 70)
    pdf = client.get(endpoint + '/download')
    assert pdf.status_code == 200 and pdf.content.startswith(b'%PDF')
    assert '-submission.pdf' in pdf.headers['content-disposition']
    pdf_text = '\n'.join(p.extract_text() for p in PdfReader(io.BytesIO(pdf.content)).pages)
    assert all(value in pdf_text for value in ['4(a)', '4(b)', '0.375', '1.5'])
    assert 'ref-binomial' not in pdf_text
    markdown = client.get(endpoint + '/download?format=md')
    assert markdown.status_code == 200 and 'text/markdown' in markdown.headers['content-type']
    assert '0.375' in markdown.text and 'ref-binomial' not in markdown.text
    docx = client.get(endpoint + '/download?format=docx')
    assert docx.status_code == 200 and docx.content.startswith(b'PK')
    assert 'wordprocessingml.document' in docx.headers['content-type']
    assert '-submission.docx' in docx.headers['content-disposition']
    assert client.get(endpoint + '/download?format=invalid').status_code == 422
    process_assignment(item['id'])
    assert client.get('/api/auth/me').json()['credits'] == 298


@pytest.mark.parametrize('all_missing', [False, True])
def test_partial_document_is_marked_incomplete_and_only_supported_sections_consume_credit(client, student, monkeypatch, all_missing):
    monkeypatch.setattr(settings, 'gemini_key', 'fake-test-key')
    review = whole_review(sections=[
        SectionVerification(index=0, supported=False, concerns=['Missing course method.']),
        SectionVerification(index=1, supported=not all_missing, concerns=[]),
    ])
    mock_whole_model(monkeypatch, review=review)
    course = enroll(client)
    item = upload(client, course).json()
    process_queued(item)
    endpoint = '/api/assignments/' + item['id']
    finished = client.get(endpoint).json()
    assert finished['status'] == 'partial'
    assert finished['submission_ready'] is False
    assert client.get('/api/auth/me').json()['credits'] == (300 if all_missing else 299)
    markdown = client.get(endpoint + '/download?format=md')
    assert markdown.status_code == 200
    assert '-incomplete-draft.md' in markdown.headers['content-disposition']
    assert 'incomplete' in markdown.text.lower()
    assert '0.375' not in markdown.text
    if all_missing:
        assert '1.5' not in markdown.text


@pytest.mark.parametrize('failure', ['provider', 'coverage', 'render', 'budget'])
def test_failed_whole_file_jobs_never_expose_a_completed_document_and_restore_reservation(client, student, monkeypatch, failure):
    monkeypatch.setattr(settings, 'gemini_key', 'fake-test-key')
    initial = 1 if failure == 'budget' else 300
    with SessionLocal() as db:
        db.get(User, student['id']).credits = initial
        db.commit()
    mock_whole_model(monkeypatch, review=whole_review(coverage_complete=failure != 'coverage'))
    if failure == 'provider':
        def unavailable(*args, **kwargs):
            raise HTTPException(502, 'Synthetic provider outage.')
        monkeypatch.setattr(providers, 'generate', unavailable)
    if failure == 'render':
        def broken_export(*args, **kwargs):
            raise RuntimeError('Synthetic export failure.')
        monkeypatch.setattr('app.jobs.render_submission', broken_export)
    course = enroll(client)
    item = upload(client, course).json()
    assert client.get('/api/auth/me').json()['credits'] == initial - min(30, initial)
    process_queued(item)
    endpoint = '/api/assignments/' + item['id']
    finished = client.get(endpoint).json()
    assert finished['status'] == finished['stage'] == 'failed'
    assert finished['results'] == []
    assert finished['submission_ready'] is False
    assert finished['download_ready'] is False
    assert client.get(endpoint + '/download').status_code == 404
    assert client.get('/api/auth/me').json()['credits'] == initial
    with SessionLocal() as db:
        usage = db.scalar(select(Usage).where(Usage.result_id == item['id']))
        assert usage.status == 'refunded' and usage.consumed == 0
        expected = (0, 0) if failure == 'provider' else (440, 140) if failure == 'coverage' else (220, 70)
        assert (usage.input_tokens, usage.output_tokens) == expected


def recovery_model(monkeypatch, first_draft, first_review, repaired_draft=None, final_review=None):
    calls = []
    drafts = iter([first_draft, repaired_draft or first_draft])
    reviews = iter([first_review, final_review or whole_review()])
    def generate(schema, instructions, content, **kwargs):
        calls.append({'schema': schema, 'instructions': instructions, 'context': json.loads(content), **kwargs})
        if schema is WholeAssignment:
            return next(drafts), (100, 50)
        if schema is WholeVerification:
            return next(reviews), (120, 20)
        raise AssertionError('No question extraction is permitted during recovery.')
    monkeypatch.setattr(providers, 'generate', generate)
    return calls


def test_missing_subpart_is_automatically_repaired_from_original_file(client, student, monkeypatch):
    course = enroll(client)
    incomplete = whole_draft()
    incomplete.sections = incomplete.sections[:1]
    missing_subpart = WholeVerification(coverage_complete=False, concerns=['4(b) is omitted.'],
        sections=[SectionVerification(index=0, supported=True, concerns=[])])
    calls = recovery_model(monkeypatch, incomplete, missing_subpart, repaired_draft=whole_draft())
    data = scanned_pdf()
    file = SimpleNamespace(name='original.pdf', mime='application/pdf', pages=2, data=data)
    stages = []
    results, tokens = solve_file(course, file, on_stage=stages.append)
    assert [r['label'] for r in results] == ['4(a)', '4(b)']
    assert all(r['answer']['verification'] == 'checked' for r in results)
    assert [c['schema'] for c in calls] == [WholeAssignment, WholeVerification, WholeAssignment, WholeVerification]
    assert all(c['attachment'] == ('application/pdf', data) for c in calls)
    assert all(c['context']['approved_evidence'] == calls[0]['context']['approved_evidence'] for c in calls)
    assert calls[2]['context']['coverage_feedback']['concerns'] == ['4(b) is omitted.']
    assert [s['index'] for s in calls[3]['context']['proposed_sections']] == [0, 1]
    assert tokens == (440, 140)
    assert stages == ['checking', 'repairing', 'checking']


def test_repaired_job_charges_only_final_supported_sections_once(client, student, monkeypatch):
    monkeypatch.setattr(settings, 'gemini_key', 'fake-test-key')
    incomplete = whole_draft()
    incomplete.sections = incomplete.sections[:1]
    review = WholeVerification(coverage_complete=False, concerns=['4(b) is omitted.'],
        sections=[SectionVerification(index=0, supported=True, concerns=[])])
    recovery_model(monkeypatch, incomplete, review, repaired_draft=whole_draft())
    course = enroll(client)
    item = upload(client, course).json()
    process_queued(item)
    result = client.get('/api/assignments/' + item['id']).json()
    assert result['status'] == 'completed' and result['submission_ready']
    assert client.get('/api/auth/me').json()['credits'] == 298
    with SessionLocal() as db:
        usage = db.scalar(select(Usage).where(Usage.result_id == item['id']))
        assert usage.consumed == 2 and (usage.input_tokens, usage.output_tokens) == (440, 140)


def test_wrong_review_indexes_retry_review_without_rewriting_answers(client, student, monkeypatch):
    course = enroll(client)
    bad = whole_review(sections=[SectionVerification(index=1, supported=True, concerns=[]),
                                 SectionVerification(index=2, supported=True, concerns=[])])
    calls = recovery_model(monkeypatch, whole_draft(), bad)
    results, tokens = solve_file(course, text_file())
    assert len(results) == 2 and all(r['answer']['verification'] == 'checked' for r in results)
    assert [c['schema'] for c in calls] == [WholeAssignment, WholeVerification, WholeVerification]
    assert calls[1]['context']['proposed_sections'] == calls[2]['context']['proposed_sections']
    assert tokens == (340, 90)


def test_repair_does_not_turn_missing_course_support_into_supported_answers(client, student, monkeypatch):
    course = enroll(client)
    incomplete = whole_draft()
    incomplete.sections = incomplete.sections[:1]
    repaired = whole_draft()
    repaired.sections[1].answer = Answer(status='needs_materials', summary='4(b) requires an absent course method.',
        steps=[], final_answer='', concepts=[], source_ids=[])
    first = WholeVerification(coverage_complete=False, concerns=['4(b) is missing.'],
        sections=[SectionVerification(index=0, supported=True, concerns=[])])
    final = whole_review(sections=[SectionVerification(index=0, supported=True, concerns=[]),
                                  SectionVerification(index=1, supported=False, concerns=['Missing course method.'])])
    recovery_model(monkeypatch, incomplete, first, repaired_draft=repaired, final_review=final)
    results, _ = solve_file(course, text_file())
    assert [r['answer']['status'] for r in results] == ['answered', 'needs_materials']
    assert results[1]['answer']['final_answer'] == ''


def test_repair_that_still_omits_task_fails_with_specific_feedback(client, student, monkeypatch):
    course = enroll(client)
    review = whole_review(coverage_complete=False)
    review.concerns = ['4(b) still omits the shared experiment data.']
    calls = recovery_model(monkeypatch, whole_draft(), review, final_review=review)
    with pytest.raises(HTTPException) as error:
        solve_file(course, text_file())
    assert '4(b)' in error.value.detail and 'shared experiment' in error.value.detail
    assert len(calls) == 4


def test_fifteen_subparts_remain_present_after_coverage_repair(client, student, monkeypatch):
    course = enroll(client)
    labels = ['1(a)', '1(b)'] + [f'{n}({letter})' for n in (2, 3) for letter in 'abcde'] + ['4(a)', '4(b)', '4(c)']
    complete = WholeAssignment(sections=[AssignmentSection(label=label,
        question=f'{label}: apply the documented binomial method to this case.', answer=whole_draft().sections[0].answer)
        for label in labels])
    incomplete = complete.model_copy(deep=True)
    incomplete.sections.pop()
    first = WholeVerification(coverage_complete=False, concerns=['4(c) was omitted on page 2.'],
        sections=[SectionVerification(index=i, supported=True, concerns=[]) for i in range(14)])
    final = WholeVerification(coverage_complete=True, concerns=[],
        sections=[SectionVerification(index=i, supported=True, concerns=[]) for i in range(15)])
    recovery_model(monkeypatch, incomplete, first, repaired_draft=complete, final_review=final)
    results, _ = solve_file(course, text_file())
    assert [r['label'] for r in results] == labels


@pytest.mark.parametrize('count', [0, 2, 31])
def test_gemini_transport_simplifies_schema_but_locally_enforces_whole_document_bounds(monkeypatch, count):
    """Reproduce the provider schema restriction without any real network or API key."""
    monkeypatch.setattr(settings, 'gemini_key', 'fake-test-key')
    monkeypatch.setattr(settings, 'ai_provider', 'gemini')
    section = whole_draft().sections[0].model_dump()
    provider_document = {'sections': [section for _ in range(count)]}
    requests = []
    original_client = httpx.Client
    data = scanned_pdf()

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, request=request, json={
            'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': json.dumps(provider_document)}]}}],
            'usageMetadata': {'promptTokenCount': 123, 'candidatesTokenCount': 40, 'thoughtsTokenCount': 7},
        })

    def offline_client(*args, **kwargs):
        return original_client(*args, transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(providers.httpx, 'Client', offline_client)
    if count == 2:
        answer, tokens = providers.generate(WholeAssignment, 'Read and answer the complete assignment.',
                                           'Original assignment file.', attachment=('application/pdf', data))
        assert len(answer.sections) == 2 and tokens == (123, 47)
    else:
        with pytest.raises(HTTPException) as error:
            providers.generate(WholeAssignment, 'Read and answer the complete assignment.',
                               'Original assignment file.', attachment=('application/pdf', data))
        assert error.value.status_code == 502
    assert len(requests) == 1
    wire_schema = requests[0]['generationConfig']['responseJsonSchema']
    assert 'minItems' not in json.dumps(wire_schema)
    assert 'maxItems' not in json.dumps(wire_schema)
    assert wire_schema['properties']['sections']['type'] == 'array'
    assert set(wire_schema['required']) == {'sections'}
    original_schema = WholeAssignment.model_json_schema()['properties']['sections']
    assert (original_schema['minItems'], original_schema['maxItems']) == (1, 30)
    attachment = requests[0]['contents'][0]['parts'][1]['inlineData']
    assert attachment['mimeType'] == 'application/pdf'
    assert base64.b64decode(attachment['data']) == data
