from app import process
def test_process():
    assert process("  hello  ") == "hello"
