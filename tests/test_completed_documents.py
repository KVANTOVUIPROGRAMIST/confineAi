import io
import json
from types import SimpleNamespace
import zipfile

import httpx
import pytest
from pypdf import PdfReader
from pypdf import PdfWriter
from sqlalchemy import select, update

from app import providers, submissions, typesetting
from app.config import settings
from app.db import SessionLocal
from app.jobs import process_assignment
from app.models import Assignment, Enrollment, Usage
from conftest import enroll


def draft(solution=r'There are $\binom{3}{2}=3$ ways.'):
    return submissions.CompletedDocument(sections=[submissions.CompletedSection(label='1(a)', question='Choose two of three objects.',
        solution=solution, source_ids=[], uses_diagram=False, assumptions=[])])


def check(correct=True, coverage=True):
    return submissions.DocumentCheck(coverage_complete=coverage, coverage_concerns=[] if coverage else ['Missing task'],
        sections=[submissions.CompletedCheck(index=0, correct=correct, course_supported=False,
                                            concerns=[] if correct else ['Recompute count'])])


def model(monkeypatch, reviews=None):
    calls = []
    reviews = list(reviews or [check()])
    def generate(schema, instructions, content, **kwargs):
        calls.append((schema, json.loads(content), kwargs))
        if schema is submissions.ReasoningCheck:
            return submissions.ReasoningCheck(correct=True, course_supported=False, replacement_solution='', assumptions=[], concerns=[]), (10, 5)
        return (draft() if schema is submissions.CompletedDocument else reviews.pop(0)), (10, 5)
    monkeypatch.setattr(providers, 'generate', generate)
    monkeypatch.setattr(settings, 'gemini_key', 'fake-key')
    return calls


def upload(client, course, key='completed-document-request-01', layout='rebuild'):
    return client.post('/api/assignments/upload', data={'enrollment_id': course['id'], 'request_key': key, 'output_layout': layout},
                       files={'file': ('homework.txt', b'1(a) Choose two of three objects.')})


def test_completion_without_course_materials_retains_answer_and_separate_note(client, student, monkeypatch):
    course = enroll(client, mode='strict')
    calls = model(monkeypatch)
    with SessionLocal() as db:
        results, notes, tokens = submissions.complete_assignment(db, db.get(Enrollment, course['id']),
            SimpleNamespace(name='hw.txt', mime='text/plain', data=b'Choose two of three objects.', pages=1), [], 30)
    assert results[0]['answer']['status'] == 'answered'
    assert results[0]['answer']['verification'] == 'general_knowledge'
    assert len(notes) == 1 and tokens == (30, 15)
    assert all(c[2]['model'] == settings.assignment_model and c[2]['thinking_level'] == 'high' for c in calls)


def test_correctness_repair_is_bounded_and_source_gap_does_not_trigger_repair(client, student, monkeypatch):
    course = enroll(client)
    calls = model(monkeypatch, [check(False), check()])
    with SessionLocal() as db:
        results, notes, tokens = submissions.complete_assignment(db, db.get(Enrollment, course['id']),
            SimpleNamespace(name='hw.txt', mime='text/plain', data=b'Choose two of three objects.', pages=1), [], 30)
    assert len(calls) == 5 and tokens == (50, 25)
    assert results[0]['answer']['status'] == 'answered'
    assert 'review_feedback' in calls[2][1]


def test_complete_job_artifacts_idempotency_ownership_and_billing(client, student, monkeypatch):
    course = enroll(client, mode='strict')
    model(monkeypatch)
    monkeypatch.setattr('app.jobs.render_completed', lambda *a: (b'%PDF-test', '# question\n3', '\\documentclass{article}', b'word', False))
    response = upload(client, course)
    assert response.status_code == 200, response.text
    item = response.json()
    assert item['output_layout'] == 'rebuild'
    assert upload(client, course).json()['id'] == item['id']
    assert upload(client, course, layout='new').status_code == 409
    assert upload(client, course, key='invalid-layout-request-01', layout='wrong').status_code == 422
    with SessionLocal() as db:
        db.execute(update(Assignment).where(Assignment.id == item['id']).values(status='running'))
        db.commit()
    process_assignment(item['id'])
    final = client.get('/api/assignments/' + item['id']).json()
    assert final['submission_ready'] and final['latex_ready'] and len(final['warnings']) == 1
    assert client.get('/api/auth/me').json()['credits'] == 299
    with SessionLocal() as db:
        usage = db.scalar(select(Usage).where(Usage.result_id == item['id']))
        assert usage.consumed == 1 and (usage.input_tokens, usage.output_tokens) == (30, 15)
    for kind in ('pdf', 'md', 'docx', 'tex', 'latex'):
        downloaded = client.get(f'/api/assignments/{item["id"]}/download?format={kind}')
        assert downloaded.status_code == 200
        assert b'not fully supported' not in downloaded.content
    assert client.get(f'/api/assignments/{item["id"]}/download?preview=true').headers['content-disposition'].startswith('inline')
    from fastapi.testclient import TestClient
    from app.main import app
    other = TestClient(app)
    assert other.get('/api/assignments/' + item['id']).status_code == 401
    assert client.delete('/api/assignments/' + item['id']).status_code == 200
    with SessionLocal() as db:
        from app.models import AssignmentOutput
        assert db.get(AssignmentOutput, item['id']) is None


@pytest.mark.parametrize('reason', ['unavailable', 'rate_limit'])
def test_capacity_fallback_is_visible_and_counts_failed_attempt_usage(client, student, monkeypatch, reason):
    course = enroll(client)
    monkeypatch.setattr(settings, 'gemini_key', 'fake-key')
    def generate(schema, instructions, content, **kwargs):
        if kwargs['model'] == settings.assignment_model:
            raise providers.ProviderFailure(reason, 'Temporary provider limit', (7, 2), True)
        return (draft() if schema is submissions.CompletedDocument else check()), (10, 5)
    monkeypatch.setattr(providers, 'generate', generate)
    recorded = []
    with SessionLocal() as db:
        results, notes, tokens = submissions.complete_assignment(db, db.get(Enrollment, course['id']),
            SimpleNamespace(name='hw.txt', mime='text/plain', data=b'Choose two objects.', pages=1), [], 30,
            on_tokens=recorded.append)
    assert results[0]['answer']['status'] == 'answered'
    expected = [(7, 2), (10, 5), (10, 5)] + ([(7, 2)] if reason == 'unavailable' else [])
    assert recorded == expected
    assert tokens == tuple(sum(value[i] for value in expected) for i in (0, 1))
    assert any(n['label'] == 'Document' and settings.ai_model in n['note'] for n in notes)


def test_access_failures_do_not_trigger_a_model_fallback(client, student, monkeypatch):
    course = enroll(client)
    calls = []
    def generate(*args, **kwargs):
        calls.append(kwargs['model'])
        raise providers.ProviderFailure('access', 'Invalid key or model access', (0, 0))
    monkeypatch.setattr(providers, 'generate', generate)
    with SessionLocal() as db, pytest.raises(providers.ProviderFailure):
        submissions.complete_assignment(db, db.get(Enrollment, course['id']),
            SimpleNamespace(name='hw.txt', mime='text/plain', data=b'Choose two objects.', pages=1), [], 30)
    assert calls == [settings.assignment_model]


@pytest.mark.parametrize('payload', [r'$\input{/etc/passwd}$', r'$\csname input\endcsname{x}$', r'$\begin{document}x\end{document}$', r'$^^5cinput{x}$', '![figure](file:///etc/passwd)'])
def test_generated_math_cannot_read_files_or_execute_tex(payload):
    with pytest.raises(typesetting.TypesetError):
        typesetting.validate_markdown(payload)


def test_latex_and_native_word_equations_preserve_math_without_private_notes(tmp_path):
    course = SimpleNamespace(code='MATH 183', title='Probability')
    results = [{'label': '1(a)', 'question': 'Choose two of three objects.', 'uses_diagram': False,
                'answer': {'status': 'answered', 'final_answer': r'$$\binom{3}{2}=\frac{3!}{2!1!}=3$$', 'summary': 'PRIVATE-NOTE'}}]
    markdown = typesetting.markdown_document('Homework', course, results, 'Student', 'rebuild')
    latex = typesetting.latex_document(markdown)
    assert r'\binom' in latex and r'\frac' in latex and 'PRIVATE-NOTE' not in latex
    assert markdown.index('Choose two') < markdown.index('**Solution**')
    assert 'Choose two' not in typesetting.markdown_document('Homework', course, results, '', 'new')
    path = tmp_path / 'math.docx'
    typesetting.pandoc(markdown, 'docx', outputfile=str(path))
    with zipfile.ZipFile(path) as archive:
        xml = archive.read('word/document.xml')
        assert b'<m:oMath' in xml and b'PRIVATE-NOTE' not in xml
    project = typesetting.latex_project(latex)
    with zipfile.ZipFile(io.BytesIO(project)) as archive:
        assert archive.read('submission.tex').decode() == latex
    original = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_blank_page(width=612, height=792)
    writer.write(original)
    typesetting.append_word_figures(path, original.getvalue())
    from docx import Document
    word = Document(path)
    assert len(word.inline_shapes) == 2
    with zipfile.ZipFile(path) as archive:
        assert b'<m:oMath' in archive.read('word/document.xml')


def test_windows_pandoc_newlines_do_not_break_aligned_math(monkeypatch):
    import pypandoc
    monkeypatch.setattr(pypandoc, 'convert_text', lambda *a, **k: '\\[\\begin{aligned}\r\nx&=1\\\\\r\ny&=2\r\n\\end{aligned}\\]\r\n')
    assert '\r' not in typesetting.pandoc('math', 'latex')


def test_malformed_display_terminators_are_repaired_without_changing_math_or_code():
    sample = r'Total: $$\frac{9!}{6}$|'
    assert typesetting.normalize_math(sample) == r'Total: $$\frac{9!}{6}$$'
    literal = '```sh\necho "$HOME"\n```\n\nPrice: \\$5; value $x$.'
    assert typesetting.normalize_math(literal) == literal
    with pytest.raises(typesetting.TypesetError):
        typesetting.normalize_math(r'An unclosed formula $$\frac{1}{2}')


def test_compilation_failure_restores_credits_and_keeps_reported_ai_usage(client, student, monkeypatch):
    course = enroll(client, mode='strict')
    model(monkeypatch)
    def cannot_compile(*args):
        raise typesetting.TypesetError()
    monkeypatch.setattr('app.jobs.render_completed', cannot_compile)
    item = upload(client, course).json()
    with SessionLocal() as db:
        db.execute(update(Assignment).where(Assignment.id == item['id']).values(status='running'))
        db.commit()
    process_assignment(item['id'])
    final = client.get('/api/assignments/' + item['id']).json()
    assert final['status'] == 'failed' and not final['download_ready'] and final['results'] == []
    assert client.get('/api/auth/me').json()['credits'] == 300
    with SessionLocal() as db:
        usage = db.scalar(select(Usage).where(Usage.result_id == item['id']))
        assert usage.consumed == 0 and (usage.input_tokens, usage.output_tokens) == (30, 15)
