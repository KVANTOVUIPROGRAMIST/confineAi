"""Assignment structure and bounded source crops; never accept model-authored TeX."""
import io
import math
import re
import unicodedata

from pydantic import Field, model_validator

from .schemas import StrictModel


class SourceRegion(StrictModel):
    page: int = Field(ge=1, le=60)
    # Top-left origin, normalized to 0..1000. No paths or rendering instructions.
    left: float = Field(ge=0, le=1000)
    top: float = Field(ge=0, le=1000)
    right: float = Field(ge=0, le=1000)
    bottom: float = Field(ge=0, le=1000)
    first_text: str = ''
    last_text: str = ''

    @model_validator(mode='after')
    def rectangle(self):
        if self.left >= self.right or self.top >= self.bottom:
            raise ValueError('Source region must have positive area')
        return self


class QuestionGroup(StrictModel):
    label: str
    context: str
    section_labels: list[str] = Field(min_length=1, max_length=30)
    figures: list[SourceRegion] = Field(default_factory=list, max_length=5)


class DocumentLayout(StrictModel):
    header: str
    preamble: str
    header_region: SourceRegion | None = None
    groups: list[QuestionGroup] = Field(min_length=1, max_length=30)


def source_anchor_text(file):
    """Text is only a locator aid. Gemini still receives the entire native file."""
    if file.mime != 'application/pdf':
        return []
    import pypdfium2 as pdfium
    try:
        with pdfium.PdfDocument(file.data) as pdf:
            result, remaining = [], 30000
            for index in range(len(pdf)):
                if remaining <= 0:
                    break
                page = pdf[index]
                text = page.get_textpage()
                try:
                    value = text.get_text_range()[:min(18000, remaining)]
                    result.append({'page': index + 1, 'text': value})
                    remaining -= len(value)
                finally:
                    text.close()
                    page.close()
            return result
    except (pdfium.PdfiumError, ValueError, RuntimeError):
        return []


def _normalized(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', value).casefold() if c.isalnum())


def source_crop(original, region, header=False, first_question=''):
    """Resolve a crop against the actual uploaded PDF, not approximate OCR geometry.

    Headers require uniquely matched text anchors. A failed match uses the full
    editable transcription instead of risking a clipped header. Figures use the
    bounded coordinates supplied from the native-file reading.
    """
    import pypdfium2 as pdfium
    try:
        region = SourceRegion.model_validate(region)
        with pdfium.PdfDocument(original) as pdf:
            if region.page > len(pdf):
                return None
            page = pdf[region.page - 1]
            try:
                width, height = page.get_size()
                x0, y0, x1, y1 = (region.left * width / 1000, (1000-region.bottom) * height / 1000,
                                  region.right * width / 1000, (1000-region.top) * height / 1000)
                if header:
                    text = page.get_textpage()
                    try:
                        if text.count_chars() > 50000:
                            return None
                        characters, indices = [], []
                        # PDFium text indices include generated CR/LF and UTF-16 units.
                        for index in range(text.count_chars()):
                            normalized = _normalized(text.get_text_range(index, 1))
                            characters.extend(normalized)
                            indices.extend([index] * len(normalized))
                        plain = ''.join(characters)
                        first, last = _normalized(region.first_text), _normalized(region.last_text)
                        if len(first) < 8 or plain.count(first) != 1:
                            return None
                        start = plain.index(first)
                        question = _normalized(first_question)
                        # The model's end anchor can omit a trailing note. Locate
                        # the first displayed prompt instead, solely for cropping.
                        boundary = next((plain.index(question[:size]) for size in (100, 80, 60, 40, 24)
                                         if len(question) >= size and plain.count(question[:size]) == 1
                                         and plain.index(question[:size]) > start), None)
                        if boundary is not None:
                            stop = boundary
                        elif len(last) >= 8 and plain.count(last) == 1:
                            stop = plain.index(last) + len(last)
                        else:
                            return None
                        if start >= stop:
                            return None
                        boxes = [text.get_charbox(i) for i in range(indices[start], indices[stop-1]+1)
                                 if text.get_text_range(i, 1).strip()]
                        x0, y0 = min(b[0] for b in boxes)-4, min(b[1] for b in boxes)-4
                        x1, y1 = max(b[2] for b in boxes)+4, max(b[3] for b in boxes)+4
                        # Include nearby rules around instruction panels.
                        x0, x1 = max(0, x0-3), min(width, x1+3)
                        y0, y1 = max(0, y0-3), min(height, y1+3)
                        if boundary is not None:
                            # Never let padding reveal a fragment of the question.
                            y0 = max(y0, text.get_charbox(indices[boundary])[3] + 2)
                    finally:
                        text.close()
                else:
                    # Gemini supplies an approximate location. Snap to native
                    # graphics when possible so nearby prompt text is not copied
                    # and edges outside that approximation are not clipped.
                    graphics = []
                    for obj in page.get_objects():
                        if obj.type not in (pdfium.raw.FPDF_PAGEOBJ_PATH, pdfium.raw.FPDF_PAGEOBJ_IMAGE):
                            continue
                        bx0, by0, bx1, by1 = obj.get_bounds()
                        area = max(0, bx1-bx0) * max(0, by1-by0)
                        overlap = max(0, min(x1, bx1)-max(x0, bx0)) * max(0, min(y1, by1)-max(y0, by0))
                        if area and area < width*height*0.8 and overlap / area >= 0.65:
                            graphics.append((bx0, by0, bx1, by1))
                    if graphics:
                        x0, y0 = min(b[0] for b in graphics), min(b[1] for b in graphics)
                        x1, y1 = max(b[2] for b in graphics), max(b[3] for b in graphics)
                        graphic_bounds = (x0, y0, x1, y1)
                        # Retain labels directly beside the illustration.
                        for obj in page.get_objects():
                            if obj.type != pdfium.raw.FPDF_PAGEOBJ_TEXT:
                                continue
                            bx0, by0, bx1, by1 = obj.get_bounds()
                            if (bx0 >= graphic_bounds[0]-15 and bx1 <= graphic_bounds[2]+15
                                    and by0 >= graphic_bounds[1]-12 and by1 <= graphic_bounds[3]+12):
                                x0, y0, x1, y1 = min(x0, bx0), min(y0, by0), max(x1, bx1), max(y1, by1)
                        x0, y0, x1, y1 = max(0, x0-4), max(0, y0-4), min(width, x1+4), min(height, y1+4)
                if not all(math.isfinite(v) for v in (x0, y0, x1, y1)) or x1-x0 < 10 or y1-y0 < 10:
                    return None
                crop = (max(0, x0), max(0, y0), max(0, width-x1), max(0, height-y1))
                bitmap = page.render(scale=min(2, 1600 / max(x1-x0, y1-y0)), crop=crop,
                                     draw_annots=False, may_draw_forms=False)
                try:
                    image = bitmap.to_pil()
                    stream = io.BytesIO()
                    image.save(stream, format='PNG')
                    image.close()
                finally:
                    bitmap.close()
                return {'page': region.page, 'trim': crop, 'width': x1-x0, 'height': y1-y0,
                        'image': stream.getvalue()}
            finally:
                page.close()
    except (pdfium.PdfiumError, ValueError, IndexError, RuntimeError):
        return None


def strip_question_label(text, label):
    """Only remove the exact leading label, never numbers inside the problem."""
    text = text.strip()
    text = re.sub(r'^\s*(?:#{1,6}\s+)?' + re.escape(label.strip()) + r'[.:]?\s+', '', text, count=1)
    part = re.search(r'(\([a-zA-Z0-9]+\))$', label)
    if part:
        parent = label[:part.start()].strip().rstrip('.')
        text = re.sub(r'^\s*' + re.escape(parent) + r'\.?\s*' + re.escape(part.group()) + r'[.:]?\s+', '', text, count=1)
        text = re.sub(r'^\s*' + re.escape(part.group()) + r'[.:]?\s+', '', text, count=1)
    return text
