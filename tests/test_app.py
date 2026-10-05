import io
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pypdf import PdfReader
from sqlalchemy import select

from app import billing, pipeline, providers
from app.config import settings
from app.credits import reserve, settle
from app.db import SessionLocal
from app.documents import extract_file, extract_questions, render_assignment
from app.jobs import process_assignment, recover_reservations
from app.models import Assignment, Chat, Enrollment, Usage, User
from app.schemas import Answer, Step, Verification
from conftest import enroll, register


def draft():
    return Answer(status='answered', summary='Use the documented binomial model.',
                  steps=[Step(explanation='For three independent trials with p=0.5, P(X=2)=3*(0.5)^2*(0.5)=0.375.', source_ids=['ref-binomial'])],
                  final_answer='P(X=2)=0.375.', concepts=['binomial'], source_ids=['ref-binomial'])


def mock_ai(monkeypatch, supported=True):
    monkeypatch.setattr(settings, 'gemini_key', 'fake-test-key')
    def generate(schema, instructions, content, **kwargs):
        if schema is Answer:
            return draft(), (100, 50)
        if schema is Verification:
            return Verification(supported=supported, concerns=[] if supported else ['Outside source scope']), (100, 20)
        raise AssertionError(f'Unexpected model call {schema}')
    monkeypatch.setattr(providers, 'generate', generate)


def test_auth_csrf_and_password_hash(client, student):
    response = client.get('/api/auth/me')
    assert response.json()['credits'] == 300
    with SessionLocal() as db:
        assert 'test-password' not in db.get(User, student['id']).password_hash
    assert client.post('/api/auth/logout', headers={'X-CSRF-Token': 'wrong'}).status_code == 403
    assert client.post('/api/auth/logout').status_code == 200
    assert client.get('/api/auth/me').status_code == 401
    assert client.post('/api/auth/login', json={'email': student['email'], 'password': 'wrong-password'}).status_code == 401
    assert client.post('/api/auth/login', json={'email': student['email'], 'password': 'test-password-123'}).status_code == 200


def test_cross_origin_registration_is_rejected(client):
    response = client.post('/api/auth/register', headers={'Origin': 'https://attacker.test'},
                           json={'email': 'test@example.test', 'password': 'test-password-123'})
    assert response.status_code == 403


def test_catalog_normalization_and_prerequisite_restrictions(client):
    result = client.get('/api/catalog?q=CSE-021').json()
    assert any(c['code'] == 'CSE 21' for c in result)
    entry = client.get('/api/catalog/ucsd-cse-29').json()
    assert set(entry['course']['prerequisite_codes']) == {'CSE 11', 'CSE 8B', 'ECE 15'}
    assert entry['course']['source_url'] == 'https://catalog.ucsd.edu/courses/CSE.html'
    assert client.get('/api/catalog?school=ucla').json() == []


def test_cross_account_course_upload_and_assignment_isolation(client, student):
    course = enroll(client)
    document = client.post('/api/materials', data={'enrollment_id': course['id']}, files={'file': ('notes.txt', b'Private course formula: P(X=k)=choose(n,k)*p^k*(1-p)^(n-k).')}).json()
    assignment = client.post('/api/assignments/preview', data={'enrollment_id': course['id']}, files={'file': ('work.txt', b'1. Compute the binomial probability.\n2. Explain the model assumptions.')}).json()
    register(client, 'other@example.test')
    assert client.get('/api/sources/' + course['id']).status_code == 404
    assert client.get('/api/chat/' + course['id']).status_code == 404
    assert client.delete('/api/materials/' + document['id']).status_code == 404
    assert client.get('/api/assignments/' + assignment['id']).status_code == 404
    assert client.get('/api/assignments/' + assignment['id'] + '/download').status_code == 404


def test_strict_mode_excludes_current_course_reference_pack(client, student):
    course = enroll(client, mode='strict', prerequisites=['ucsd-math-20c'])
    sources = client.get('/api/sources/' + course['id']).json()
    assert sources
    assert all(s['id'] != 'ref-binomial' for s in sources)
    assert any(s['id'] == 'ref-derivatives' for s in sources)
    client.post('/api/materials', data={'enrollment_id': course['id']}, files={'file': ('class.txt', b'Our class uses a binomial distribution for independent Bernoulli trials.')})
    assert any(s['origin'] == 'upload' for s in client.get('/api/sources/' + course['id']).json())


def test_new_course_requires_explicit_opt_in_to_original_references(client, student):
    response = client.post('/api/courses', json={'course_id': 'ucsd-math-183'})
    assert response.status_code == 200
    course = response.json()
    assert course['mode'] == 'strict'
    assert client.get('/api/sources/' + course['id']).json() == []


def test_variable_topic_and_history_course_require_sources(client, student):
    for code in ('ucsd-cse-190', 'ucsd-mmw-122'):
        course = enroll(client, code)
        assert client.get('/api/sources/' + course['id']).json() == []


def test_missing_ai_does_not_consume_allowance(client, student):
    course = enroll(client)
    response = client.post('/api/chat', json={'enrollment_id': course['id'], 'question': 'Explain binomial probability.', 'request_key': 'x' * 32})
    assert response.status_code == 503
    assert client.get('/api/auth/me').json()['credits'] == 300


def test_supported_answer_and_request_idempotency(client, student, monkeypatch):
    mock_ai(monkeypatch)
    course = enroll(client)
    body = {'enrollment_id': course['id'], 'question': 'For n=3 and p=.5, find P(X=2) using the binomial formula.', 'request_key': 'a' * 32}
    first = client.post('/api/chat', json=body)
    assert first.status_code == 200, first.text
    assert first.json()['answer']['verification'] == 'checked'
    assert first.json()['answer']['sources'][0]['id'] == 'ref-binomial'
    second = client.post('/api/chat', json=body)
    assert second.json()['id'] == first.json()['id']
    assert client.get('/api/auth/me').json()['credits'] == 299
    body['question'] = 'A different question'
    assert client.post('/api/chat', json=body).status_code == 409


def test_semantic_rejection_refunds_response(client, student, monkeypatch):
    mock_ai(monkeypatch, supported=False)
    course = enroll(client)
    response = client.post('/api/chat', json={'enrollment_id': course['id'], 'question': 'Use an unsupported advanced method for a binomial question.', 'request_key': 'b' * 32})
    assert response.json()['answer']['status'] == 'needs_materials'
    assert response.json()['answer']['steps'] == []
    assert client.get('/api/auth/me').json()['credits'] == 300


def test_invalid_citation_is_withheld_before_review(client, student, monkeypatch):
    monkeypatch.setattr(settings, 'gemini_key', 'fake-test-key')
    calls = []
    def generate(schema, *_args, **_kwargs):
        calls.append(schema)
        answer = draft()
        answer.source_ids = ['invented-source']
        return answer, (10, 10)
    monkeypatch.setattr(providers, 'generate', generate)
    course = enroll(client)
    response = client.post('/api/chat', json={'enrollment_id': course['id'], 'question': 'Explain binomial distribution.', 'request_key': 'c' * 32})
    assert response.json()['answer']['status'] == 'needs_materials'
    assert calls == [Answer]
    assert client.get('/api/auth/me').json()['credits'] == 300


def test_provider_failure_restores_credit(client, student, monkeypatch):
    monkeypatch.setattr(settings, 'gemini_key', 'fake-test-key')
    def fail(*args, **kwargs):
        raise HTTPException(502, 'Provider unavailable')
    monkeypatch.setattr(providers, 'generate', fail)
    course = enroll(client)
    response = client.post('/api/chat', json={'enrollment_id': course['id'], 'question': 'Explain probability.', 'request_key': 'd' * 32})
    assert response.status_code == 502
    assert client.get('/api/auth/me').json()['credits'] == 300


def test_credit_overdraft_and_duplicate_settlement(client, student):
    with SessionLocal() as db:
        user = db.get(User, student['id'])
        user.credits, user.topup_credits = 1, 1
        db.commit()
        usage, fresh = reserve(db, user, 2, 'credit-test-key-0001', 'assignment', 'question')
        assert fresh
        db.refresh(user)
        assert (user.credits, user.topup_credits) == (0, 0)
        with pytest.raises(HTTPException) as error:
            reserve(db, user, 1, 'credit-test-key-0002', 'chat', 'question2')
        assert error.value.status_code == 402
        settle(db, usage.id, 1)
        settle(db, usage.id, 1)
        db.refresh(user)
        assert (user.credits, user.topup_credits) == (0, 1)


def test_assignment_preview_run_export_and_partial_refund(client, student, monkeypatch):
    course = enroll(client)
    preview = client.post('/api/assignments/preview', data={'enrollment_id': course['id']},
                          files={'file': ('problem-set.txt', b'1. For n=3 and p=.5, compute P(X=2).\n2. Solve with a method absent from the notes.')})
    assert preview.status_code == 200
    assignment = preview.json()
    assert len(assignment['questions']) == 2
    mock_ai(monkeypatch)
    original = pipeline.solve
    def partial(db, enrollment, question, **kwargs):
        if 'absent' in question:
            return pipeline.missing(), (0, 0)
        return original(db, enrollment, question, **kwargs)
    monkeypatch.setattr('app.jobs.solve', partial)
    response = client.post('/api/assignments/' + assignment['id'] + '/run', json={'questions': assignment['questions'], 'request_key': 'e' * 32})
    assert response.status_code == 200
    assert client.get('/api/auth/me').json()['credits'] == 298
    with SessionLocal() as db:
        item = db.get(Assignment, assignment['id'])
        assert any(s['id'] == 'ref-binomial' for s in item.source_snapshot)
        item.status = 'running'
        db.commit()
    process_assignment(assignment['id'])
    finished = client.get('/api/assignments/' + assignment['id']).json()
    assert finished['status'] == 'partial'
    assert client.get('/api/auth/me').json()['credits'] == 299
    pdf = client.get('/api/assignments/' + assignment['id'] + '/download')
    assert pdf.status_code == 200 and pdf.content.startswith(b'%PDF')
    text = '\n'.join(page.extract_text() for page in PdfReader(io.BytesIO(pdf.content)).pages)
    assert '0.375' in text and 'Needs supporting material' in text
    editable = client.get('/api/assignments/' + assignment['id'] + '/download?format=md')
    assert 'ref-binomial' in editable.text
    assert 'Needs supporting material' in editable.text


def test_scanned_pdf_without_key_is_explicitly_rejected():
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    stream = io.BytesIO()
    writer.write(stream)
    with pytest.raises(HTTPException) as error:
        extract_file('scan.pdf', stream.getvalue())
    assert 'scanned' in error.value.detail.lower()


def test_draft_edits_persist_and_require_ownership(client, student):
    course = enroll(client)
    item = client.post('/api/assignments/preview', data={'enrollment_id': course['id']},
                       files={'file': ('draft.txt', b'1. Original question.')}).json()
    endpoint = '/api/assignments/' + item['id']
    edited = ['1. Corrected question.', '2. A second question.']
    assert client.put(endpoint, json={'questions': edited}).status_code == 200
    assert client.get(endpoint).json()['questions'] == edited
    assert client.put(endpoint, json={'questions': ['   ']}).status_code == 422
    register(client, 'draft-other@example.test')
    assert client.put(endpoint, json={'questions': edited}).status_code == 404


def test_job_shutdown_recovers_unlinked_and_finished_reservations(client, student):
    course = enroll(client)
    with SessionLocal() as db:
        user = db.get(User, student['id'])
        orphan, _ = reserve(db, user, 2, 'orphan-job-request-01', 'assignment', 'orphan')
        finished, _ = reserve(db, user, 2, 'finished-job-request-01', 'assignment', 'finished')
        item = Assignment(user_id=user.id, enrollment_id=course['id'], title='Finished',
                          questions=['a', 'b'], usage_id=finished.id, status='partial',
                          results=[{'question': 'a', 'answer': draft().model_dump()},
                                   {'question': 'b', 'answer': pipeline.missing()}])
        db.add(item)
        db.commit()
        recover_reservations(db)
        recover_reservations(db)
        db.refresh(user)
        assert user.credits == 299
        assert db.get(Usage, orphan.id).status == 'refunded'
        assert db.get(Usage, finished.id).consumed == 1


def test_question_extraction_does_not_duplicate_chunk_overlap():
    text = '1. ' + 'alpha ' * 500 + '\n2. Second question.'
    chunks, _ = extract_file('test.txt', text.encode())
    questions = extract_questions(chunks)
    assert len(questions) == 2
    assert questions[0].count('alpha') == 500


def test_pdf_escapes_markup_and_wraps_long_content():
    answer = draft().model_dump()
    answer['sources'] = []
    result = render_assignment('Export <test>', SimpleNamespace(code='MATH 183', title='Statistical Methods'),
                               [{'question': '1. <script>alert(1)</script> ' + 'long question ' * 150, 'answer': answer}])
    reader = PdfReader(io.BytesIO(result))
    assert len(reader.pages) >= 1
    assert '<script>' in ''.join(p.extract_text() for p in reader.pages)


def test_later_questions_receive_shared_problem_data_in_draft_and_review(client, student, monkeypatch):
    course = enroll(client)
    mock_ai(monkeypatch)
    original = providers.generate
    contexts = []
    def capture(schema, instructions, content, **kwargs):
        contexts.append(json.loads(content)['assignment_context'])
        return original(schema, instructions, content, **kwargs)
    monkeypatch.setattr(providers, 'generate', capture)
    questions = ['1. Flip a fair coin independently three times.', '2. For the same experiment, find the expected number of heads.']
    with SessionLocal() as db:
        answer, _ = pipeline.solve(db, db.get(Enrollment, course['id']), questions[1], assignment_context=questions)
    assert answer['status'] == 'answered'
    assert contexts == [questions, questions]


def test_beta_disables_checkout_even_with_stripe_keys(client, student, monkeypatch):
    monkeypatch.setattr(settings, 'stripe_key', 'fake')
    monkeypatch.setattr(settings, 'stripe_price', 'price_student')
    monkeypatch.setattr(settings, 'stripe_webhook', 'fake')
    assert client.post('/api/billing/checkout').status_code == 503


def test_payment_fulfillment_is_idempotent_and_checks_price(client, student, monkeypatch):
    monkeypatch.setattr(settings, 'stripe_price', 'price_student')
    with SessionLocal() as db:
        user = db.get(User, student['id'])
        user.stripe_customer = 'cus_test'
        db.commit()
        event = {'id': 'evt_invoice', 'type': 'invoice.paid', 'data': {'object': {'customer': 'cus_test',
            'billing_reason': 'subscription_create', 'subscription': 'sub_test',
            'lines': {'data': [{'price': {'id': 'price_student'}, 'period': {'start': 100}}]}}}}
        billing.handle_event(db, event)
        db.refresh(user)
        assert user.tier == 'student' and user.credits == 300
        user.credits = 280
        db.commit()
        billing.handle_event(db, event)
        db.refresh(user)
        assert user.credits == 280
        wrong = {'id': 'evt_wrong', 'type': 'invoice.paid', 'data': {'object': {'customer': 'cus_test',
            'billing_reason': 'subscription_cycle', 'lines': {'data': [{'price': {'id': 'unrelated'}, 'period': {'start': 200}}]}}}}
        billing.handle_event(db, wrong)
        db.refresh(user)
        assert user.credits == 280
        purchase = {'customer': 'cus_test', 'id': 'cs_topup', 'mode': 'payment', 'payment_status': 'paid', 'metadata': {'purchase': 'topup'}}
        for event_id, kind in [('evt_topup', 'checkout.session.completed'), ('evt_topup_again', 'checkout.session.async_payment_succeeded')]:
            billing.handle_event(db, {'id': event_id, 'type': kind, 'data': {'object': purchase}})
        db.refresh(user)
        assert user.topup_credits == 100


def test_account_delete_removes_private_content(client, student):
    course = enroll(client)
    client.post('/api/materials', data={'enrollment_id': course['id']}, files={'file': ('notes.txt', b'A private mathematical explanation with a formula.')})
    assert client.delete('/api/account').status_code == 200
    assert client.get('/api/auth/me').status_code == 401
    with SessionLocal() as db:
        assert db.get(User, student['id']) is None
