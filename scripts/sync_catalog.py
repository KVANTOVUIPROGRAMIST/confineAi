"""Import official UCSD course metadata. Run manually; student answers never browse."""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DEFAULT_DEPARTMENTS = ['CSE', 'MATH', 'MMW', 'PHYS', 'ECE', 'DSC', 'COGS']


def normalize_code(code):
    match = re.fullmatch(r'\s*([A-Za-z]+)[\s-]*0*(\d+[A-Za-z]*)\s*', code)
    return f'{match[1].upper()} {match[2].upper()}' if match else code.strip().upper()


def parse_catalog(html, department, url):
    soup = BeautifulSoup(html, 'html.parser')
    courses = []
    pattern = re.compile(rf'^{department}\s+(\d+[A-Z]*)\.\s*(.+?)\s*\(([^)]+)\)', re.I)
    for paragraph in soup.find_all(['p', 'h3', 'h4']):
        text = paragraph.get_text(' ', strip=True)
        match = pattern.match(text)
        if not match:
            continue
        pieces = []
        for sibling in paragraph.find_next_siblings():
            value = sibling.get_text(' ', strip=True)
            if pattern.match(value) or sibling.name in ('h2', 'h3', 'h4'):
                break
            if sibling.name == 'p' and value:
                pieces.append(value)
        body = ' '.join(pieces)
        prereq_match = re.search(r'Prerequisites?:\s*(.+)', body, re.I)
        prereq = prereq_match[1] if prereq_match else ''
        # Keep the official language; flat suggestions do not claim to resolve AND/OR logic.
        requirement_clause = re.split(r';|\.(?:\s|$)', prereq)[0]
        codes = [normalize_code(f'{m[0]} {m[1]}') for m in re.findall(r'\b([A-Z]{2,6})\s*(\d+[A-Z]*)\b', requirement_clause)]
        description = re.split(r'Prerequisites?:', body, flags=re.I)[0].strip()
        code = normalize_code(f'{department} {match[1]}')
        courses.append({'id': 'ucsd-' + code.replace(' ', '-').lower(), 'school': 'ucsd', 'code': code,
                        'title': match[2].strip(), 'description': description[:3500],
                        'prerequisite_text': prereq[:2000], 'prerequisite_codes': list(dict.fromkeys(codes)),
                        'source_url': url, 'catalog_year': '2026-27'})
    return courses


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--departments', nargs='+', default=DEFAULT_DEPARTMENTS)
    args = parser.parse_args()
    items = {}
    target = ROOT / 'app' / 'data' / 'catalog.json'
    if target.exists():
        items = {c['id']: c for c in json.loads(target.read_text(encoding='utf-8'))['courses']}
    with httpx.Client(timeout=30, follow_redirects=False, headers={'User-Agent': 'ConfineCourseCatalog/0.1 (manual metadata import)'}) as client:
        for department in args.departments:
            if not re.fullmatch('[A-Z]{2,6}', department):
                raise ValueError('Use a UCSD department code, e.g. CSE or MATH.')
            url = f'https://catalog.ucsd.edu/courses/{department}.html'
            response = client.get(url)
            response.raise_for_status()
            courses = parse_catalog(response.text, department, url)
            if not courses:
                raise ValueError(f'No course entries parsed from {url}; catalog format may have changed.')
            items.update({c['id']: c for c in courses})
            print(f'{department}: {len(courses)} courses')
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps({'imported_at': datetime.now(timezone.utc).isoformat(),
                                  'courses': sorted(items.values(), key=lambda c: c['code'])}, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f'Saved {len(items)} course records to app/data/catalog.json')


if __name__ == '__main__':
    main()
