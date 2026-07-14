from report import generate_report
from values import get_score
def test_report_format():
    assert generate_report("alice") == "95 points"
def test_na_handling():
    assert get_score("bob") == 0
