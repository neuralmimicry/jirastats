from types import SimpleNamespace
import main as m


def make_issue(val):
    return SimpleNamespace(fields=SimpleNamespace(customfield_10104=val))


def test_sort_issues_by_priority_alphanumeric():
    issues = [make_issue('A002'), make_issue('A003'), make_issue('A001')]
    sorted_issues = m.sort_issues_by_priority(issues)
    assert [i.fields.customfield_10104 for i in sorted_issues] == ['A001', 'A002', 'A003']
