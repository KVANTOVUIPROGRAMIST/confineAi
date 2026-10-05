import json
from pathlib import Path

from sqlalchemy import select

from .models import Course

DATA = Path(__file__).parent / 'data'
SCHOOLS = [
    {'id': 'ucsd', 'name': 'UC San Diego', 'short': 'UCSD', 'available': True},
    {'id': 'ucb', 'name': 'UC Berkeley', 'short': 'UCB', 'available': False},
    {'id': 'ucla', 'name': 'UCLA', 'short': 'UCLA', 'available': False},
    {'id': 'uci', 'name': 'UC Irvine', 'short': 'UCI', 'available': False},
    {'id': 'ucd', 'name': 'UC Davis', 'short': 'UCD', 'available': False},
    {'id': 'ucsb', 'name': 'UC Santa Barbara', 'short': 'UCSB', 'available': False},
    {'id': 'ucsc', 'name': 'UC Santa Cruz', 'short': 'UCSC', 'available': False},
    {'id': 'ucr', 'name': 'UC Riverside', 'short': 'UCR', 'available': False},
    {'id': 'ucm', 'name': 'UC Merced', 'short': 'UCM', 'available': False},
]


def seed_catalog(db):
    path = DATA / 'catalog.json'
    if not path.exists():
        return
    for data in json.loads(path.read_text(encoding='utf-8'))['courses']:
        course = db.get(Course, data['id'])
        if course:
            for key, value in data.items():
                setattr(course, key, value)
        else:
            db.add(Course(**data))
    db.commit()


def course_dict(course):
    return {key: getattr(course, key) for key in ['id', 'school', 'code', 'title', 'description',
                                                'prerequisite_text', 'prerequisite_codes', 'source_url', 'catalog_year']}


def reference_pack(code):
    packs = json.loads((DATA / 'references.json').read_text(encoding='utf-8'))
    return [dict(item, origin='reference', name='Confine prerequisite reference', course_code=code,
                 page=1, url=None) for item in packs if code in item['courses']]
