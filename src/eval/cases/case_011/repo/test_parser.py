from parser import parse_line

def test_parse_line():
    assert parse_line("a,b,c") == ["a", "b", "c"]
