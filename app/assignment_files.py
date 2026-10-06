"""Validate uploads without transcription, extraction, or question segmentation."""
import io
from pathlib import Path

from fastapi import HTTPException
from PIL import Image
from pypdf import PdfReader

from .config import settings


def validate_assignment_file(name, data):
    if not data:
        raise HTTPException(422, 'The assignment file is empty.')
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(413, 'Files must be 10 MB or smaller.')
    extension = Path(name).suffix.lower()
    try:
        if extension == '.pdf':
            if not data.startswith(b'%PDF-'):
                raise ValueError('Invalid PDF')
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                raise HTTPException(422, 'Please upload an unencrypted PDF.')
            pages = len(reader.pages)
            if not pages:
                raise HTTPException(422, 'The PDF has no pages.')
            if pages > settings.max_pages:
                raise HTTPException(413, f'Upload up to {settings.max_pages} pages at a time.')
            return 'application/pdf', pages
        if extension in ('.txt', '.md'):
            text = data.decode('utf-8-sig')
            if not text.strip():
                raise HTTPException(422, 'The assignment file is empty.')
            if len(text) > 45000:
                raise HTTPException(413, 'Text assignments can contain up to 45,000 characters. Upload a PDF for longer documents.')
            return 'text/plain', 1
        if extension in ('.png', '.jpg', '.jpeg'):
            with Image.open(io.BytesIO(data)) as image:
                expected = 'PNG' if extension == '.png' else 'JPEG'
                if image.format != expected:
                    raise ValueError('Image format does not match extension')
                if image.width * image.height > 25_000_000:
                    raise HTTPException(413, 'Use an image under 25 megapixels.')
                image.verify()
            return 'image/png' if extension == '.png' else 'image/jpeg', 1
        raise HTTPException(415, 'Upload PDF, TXT, Markdown, PNG, or JPG files.')
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(422, 'This file is invalid or unreadable. Try an unencrypted PDF, UTF-8 text file, or clear image.') from None


def model_file_context(file):
    if file.mime == 'text/plain':
        return {'name': file.name, 'text': file.data.decode('utf-8-sig')}, None
    return {'name': file.name, 'pages': file.pages, 'content': 'Read the original attached file, including all pages, diagrams, tables, and instructions.'}, (file.mime, file.data)
