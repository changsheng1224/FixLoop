"""issue_paths 单测。"""

from src.repair.issue_paths import extract_paths_from_issue


def test_extract_paths_dedupes_and_preserves_order():
    issue = 'File "src/a.py", line 1\n  at b.py:2\nFile "src/a.py", line 3'
    paths = extract_paths_from_issue(issue, extra=["c.py"])
    assert paths == ["c.py", "src/a.py", "b.py"]
