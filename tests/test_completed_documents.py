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
from app.document_layout import DocumentLayout, QuestionGroup, SourceRegion, source_crop
from conftest import enroll


def draft(solution=r'There are $\binom{3}{2}=3$ ways.'):
    return submissions.CompletedDocument(sections=[submissions.CompletedSection(label='1(a)', question='Choose two of three objects.',
        display_question='Choose two of three objects.', solution=solution, source_ids=[], uses_diagram=False, assumptions=[])],
        layout=DocumentLayout(header='', preamble='', groups=[QuestionGroup(label='1.', context='', section_labels=['1(a)'])]))


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
    assert results[0]['document_layout']['groups'][0]['section_labels'] == ['1(a)']
    assert calls[1][1]['proposed_layout']['groups'][0]['label'] == '1.'


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


def test_double_escaped_paragraphs_are_fixed_but_program_escapes_and_tex_commands_remain():
    sample = r'Base case:\nLeft side $\nabla f$.\n\nInduction step:\n$$x=1$$'
    assert typesetting.normalize_math(sample) == 'Base case:\nLeft side $\\nabla f$.\n\nInduction step:\n$$x=1$$'
    program = '```python\nprint("\\nHello")\n```\n\nLiteral `\\nHello`.'
    assert typesetting.normalize_math(program) == program


def test_unformatted_question_latex_requires_repair_but_literal_code_is_allowed():
    with pytest.raises(typesetting.TypesetError, match='Wrap every LaTeX formula'):
        typesetting.validate_markdown(r'Prove: 2+5+8=\frac{n(3n+1)}{2}')
    typesetting.validate_markdown('Formula: $\\frac{1}{2}$.\n\n```python\nprint("\\frac{1}{2}")\n```')


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


def test_grouped_document_preserves_header_points_context_and_answers_only_mode():
    results = [
        {'label': '2(a)', 'question': 'Shared context. Question a.', 'display_question': '(a) (4 points) Question a.',
         'answer': {'final_answer': 'Answer a.'}},
        {'label': '2(b)', 'question': 'Shared context. Question b.', 'display_question': '(4 points) Question b.',
         'answer': {'final_answer': 'Answer b.'}},
    ]
    results[0]['document_layout'] = DocumentLayout(header='CSE 21 Fall 2026\n\nHomework 1\n\nDue Monday',
        preamble='**Instructions**\n\nExplain each factor.',
        groups=[QuestionGroup(label='2.', context='2. Shared context.', section_labels=['2(a)', '2(b)'])]).model_dump()
    course = SimpleNamespace(code='CSE 21', title='Catalog title')
    markdown = typesetting.markdown_document('WRONG FILE TITLE', course, results, 'Student')
    assert 'WRONG FILE TITLE' not in markdown and 'Catalog title' not in markdown
    assert markdown.count('Shared context.') == 1
    assert markdown.count('(4 points)') == 2 and markdown.count('### (a)') == 1
    assert markdown.index('Explain each factor.') < markdown.index('**2.**') < markdown.index('Question a.') < markdown.index('Answer a.')
    new = typesetting.markdown_document('Solutions', course, results, '', 'new')
    assert 'Shared context.' not in new and 'Due Monday' not in new and '2(a)' in new and 'Answer b.' in new


def test_source_header_anchors_resolve_exact_crop_and_bad_anchors_use_transcription():
    from reportlab.pdfgen import canvas
    original = io.BytesIO()
    writer = canvas.Canvas(original, pagesize=(612, 792))
    writer.drawRightString(530, 720, 'CSE 21 Fall 2026')
    writer.drawRightString(530, 700, 'Homework 1')
    writer.drawString(80, 665, 'Instructions: explain each factor.')
    writer.drawString(80, 600, '1. First question outside the header.')
    writer.save()
    # Intentionally wrong approximate coordinates: unique anchors own exact bounds.
    region = SourceRegion(page=1, left=0, top=0, right=1000, bottom=1000,
                          first_text='CSE 21 Fall 2026', last_text='explain each factor.')
    crop = source_crop(original.getvalue(), region.model_dump(), header=True)
    assert crop and crop['image'].startswith(b'\x89PNG')
    left, bottom, right, top = crop['trim']
    assert bottom > 640 and top > 50 and crop['height'] < 100
    region.last_text = 'a nonexistent anchor'
    assert source_crop(original.getvalue(), region.model_dump(), header=True) is None
    region.page = 2
    assert source_crop(original.getvalue(), region.model_dump()) is None
    with pytest.raises(ValueError):
        SourceRegion(page=1, left=500, top=10, right=400, bottom=20)


def test_solution_spacing_preserves_equations_and_code():
    solution = 'Base case: true. Induction hypothesis: assume $n=k$. Induction step: derive the result.\n\nThus $\\frac{n(3n+1)}{2}=\\frac{3n^2+n}{2}$.'
    spaced = typesetting.solution_spacing(solution)
    assert '\n\nInduction hypothesis:' in spaced and '\n\nInduction step:' in spaced
    assert r'$$\frac{n(3n+1)}{2}=\frac{3n^2+n}{2}$$' in spaced
    code = '```python\nprint("Base case: true. Induction step: literal")\n```'
    assert typesetting.solution_spacing(code) == code
    listed = '**Justification:**\n1. **Choose:** First step.\n2. **Arrange:** Second step.'
    assert '**Justification:**\n\n1. ' in typesetting.solution_spacing(listed)
    assert '\n\n2. **Arrange:**' in typesetting.solution_spacing(listed)


def test_native_regions_are_inlined_in_word_and_latex_without_original_appendix(tmp_path, monkeypatch):
    from PIL import Image
    image = io.BytesIO()
    Image.new('RGB', (300, 150), 'white').save(image, format='PNG')
    crop = {'page': 1, 'trim': (70, 500, 70, 80), 'width': 472, 'height': 212, 'image': image.getvalue()}
    monkeypatch.setattr(typesetting, 'source_crop', lambda *a, **k: crop)
    compiled = []
    monkeypatch.setattr(typesetting, 'compile_pdf', lambda tex, original: compiled.append((tex, original)) or b'%PDF')
    region = SourceRegion(page=1, left=0, top=0, right=1000, bottom=400)
    results = [{'label': '1(a)', 'question': 'A figure question.', 'display_question': '(4 points) A figure question.',
                'figures': [region.model_dump()], 'uses_diagram': True, 'answer': {'final_answer': '$$x=1$$'},
                'document_layout': DocumentLayout(header='Original title', preamble='Instructions', header_region=region,
                    groups=[QuestionGroup(label='1.', context='', section_labels=['1(a)'])]).model_dump()}]
    pdf, md, tex, docx, source_needed = typesetting.render_completed('Wrong title', SimpleNamespace(code='CSE 21', title='Course'),
        results, 'Student', 'rebuild', SimpleNamespace(mime='application/pdf', data=b'original-source'))
    assert source_needed and compiled[0][1] == b'original-source'
    assert tex.count('includegraphics[') == 2 and 'includepdf' not in tex
    assert 'Original title' in md and '(4 points)' in md and 'Wrong title' not in md
    assert r'\Needspace' in tex
    with zipfile.ZipFile(io.BytesIO(docx)) as archive:
        assert b'CONFINE_SOURCE_REGION_' not in archive.read('word/document.xml')
        assert b'CONFINE_BLOCK_ROLE_' not in archive.read('word/document.xml')
        assert archive.read('word/document.xml').count(b'<pic:pic>') == 2
        assert b'<m:oMath' in archive.read('word/document.xml')


def test_figure_crop_snaps_to_native_graphic_and_excludes_neighboring_question():
    from PIL import Image
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    picture = io.BytesIO()
    Image.new('RGB', (100, 100), 'navy').save(picture, format='PNG')
    original = io.BytesIO()
    writer = canvas.Canvas(original, pagesize=(612, 792))
    writer.drawString(110, 435, 'Neighboring question must not be in the crop.')
    writer.drawImage(ImageReader(picture), 118, 285, width=120, height=120)
    writer.save()
    region = SourceRegion(page=1, left=180, top=440, right=410, bottom=630)
    crop = source_crop(original.getvalue(), region.model_dump())
    assert crop and crop['height'] == pytest.approx(128, abs=1)
    assert crop['trim'][1] == pytest.approx(281, abs=1)
    assert crop['trim'][3] > 380


def test_layout_with_missing_or_reordered_subparts_cannot_be_accepted(client, student, monkeypatch):
    course = enroll(client)
    monkeypatch.setattr(settings, 'gemini_key', 'fake-key')
    malformed = draft()
    malformed.layout.groups[0].section_labels = ['1(b)']
    calls = []
    def generate(schema, instructions, content, **kwargs):
        calls.append(schema)
        return (malformed if schema is submissions.CompletedDocument else check()), (10, 5)
    monkeypatch.setattr(providers, 'generate', generate)
    from fastapi import HTTPException
    with SessionLocal() as db, pytest.raises(HTTPException, match='omitted or misread'):
        submissions.complete_assignment(db, db.get(Enrollment, course['id']),
            SimpleNamespace(name='hw.txt', mime='text/plain', data=b'Question a.', pages=1), [], 30)
    assert calls == [submissions.CompletedDocument, submissions.DocumentCheck]*2
