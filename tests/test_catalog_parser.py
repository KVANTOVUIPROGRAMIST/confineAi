from scripts.sync_catalog import normalize_code, parse_catalog


def test_parser_keeps_alternatives_but_excludes_credit_restrictions():
    html = '''<p>CSE 29. Systems Programming and Software Tools (4)</p>
    <p>Programming in C. Prerequisites: CSE 11 or CSE 8B or ECE 15; two units of credit if CSE 15L taken.</p>
    <p>CSE 190. Topics in Computer Science and Engineering (4)</p><p>Topics vary. Prerequisites: consent of instructor.</p>'''
    courses = parse_catalog(html, 'CSE', 'https://catalog.ucsd.edu/courses/CSE.html')
    assert len(courses) == 2
    assert courses[0]['prerequisite_codes'] == ['CSE 11', 'CSE 8B', 'ECE 15']
    assert 'or' in courses[0]['prerequisite_text']
    assert courses[1]['prerequisite_codes'] == []


def test_normalize_hyphenated_and_zero_padded_course_codes():
    assert normalize_code('CSE-021') == 'CSE 21'
    assert normalize_code('math-183') == 'MATH 183'
