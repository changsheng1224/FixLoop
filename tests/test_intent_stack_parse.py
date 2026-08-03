"""Tests for traceback extraction from mixed large user inputs."""

from agent_runtime.intent.router import IntentRouter
from agent_runtime.intent.models import RouteContext
from agent_runtime.intent.stack_parse import extract_issue_slots, parse_stack
from agent_runtime.intent.segmenter import segment


_LARGE = '''
帮我看看：

```python
def load_config(path="settings.py"):
    return path

def walk_repo(root="src/legacy_app.py"):
    for name in ("a.py", "b.py", "noise_module.py"):
        print(name)
    return root
```

Traceback (most recent call last):
  File "calculator.py", line 42, in add
    return a + b
TypeError: unsupported operand type(s) for +: 'int' and 'str'
'''


class TestStackParse:
    def test_frames_from_traceback_not_fenced_noise(self):
        parsed = parse_stack(_LARGE)
        assert parsed.has_traceback
        assert parsed.exception_type == "TypeError"
        assert parsed.issue_type == "type_error"
        assert parsed.suspect_files == ["calculator.py"]
        assert "settings.py" not in parsed.suspect_files
        assert "noise_module.py" not in parsed.suspect_files
        assert parsed.top_frame is not None
        assert parsed.top_frame.line == 42

    def test_extract_slots_stack_first(self):
        slots = extract_issue_slots(_LARGE)
        assert slots["suspect_files"] == ["calculator.py"]
        assert slots["issue_type"] == "type_error"
        assert slots["frames"][0]["file"] == "calculator.py"

    def test_multi_frame_paths(self):
        text = '''
Traceback (most recent call last):
  File "gateway.py", line 1, in <module>
  File "backend/tasks.py", line 9, in run
AttributeError: x
'''
        parsed = parse_stack(text)
        assert parsed.suspect_files == ["gateway.py", "backend/tasks.py"]
        assert parsed.issue_type == "attribute_error"

    def test_at_file_fallback(self):
        slots = extract_issue_slots("TypeError: bad at calculator.py:42")
        assert "calculator.py" in slots.get("suspect_files", [])

    def test_filters_site_packages_keeps_app_frames(self):
        text = '''
Traceback (most recent call last):
  File "/usr/local/lib/python3.11/site-packages/django/core/handlers/base.py", line 197, in _get_response
    response = wrapped_callback(request)
  File "/app/backend/orders/services/pricing.py", line 67, in compute_total
    return subtotal + tax_amount
TypeError: unsupported operand type(s) for +: 'decimal.Decimal' and 'str'
'''
        slots = extract_issue_slots(text)
        assert slots["suspect_files"] == ["backend/orders/services/pricing.py"]
        assert slots["issue_type"] == "type_error"
        assert all("site-packages" not in f for f in slots["suspect_files"])

    def test_realistic_django_case_from_yaml(self):
        from agent_runtime.intent.eval_metrics import load_eval_cases

        cases = {c.id: c for c in load_eval_cases()}
        case = cases["realistic_django_orm_typeerror"]
        slots = extract_issue_slots(case.text)
        for f in case.expect["slots"]["suspect_files"]:
            assert f in slots["suspect_files"]
        assert all("site-packages" not in f for f in slots["suspect_files"])
        assert all("gunicorn" not in f for f in slots["suspect_files"])


class TestLargeInputRouting:
    def test_repair_channel_ignores_paste_noise(self):
        r = IntentRouter().route(_LARGE, RouteContext(channel="repair"))
        assert r.primary == "repair_issue"
        assert r.slots.get("suspect_files") == ["calculator.py"]
        assert r.slots.get("issue_type") == "type_error"

    def test_repl_large_with_fix_word(self):
        text = "帮我修这个错误\n\n" + _LARGE
        r = IntentRouter().route(text, RouteContext(channel="repl"))
        assert r.primary in ("repair_request", "repair_issue")
        assert "calculator.py" in (r.slots.get("suspect_files") or [])
        assert "noise_module.py" not in (r.slots.get("suspect_files") or [])

    def test_segmenter_merges_code_into_stack(self):
        segs = segment(_LARGE)
        assert len(segs) == 1
        assert "Traceback" in segs[0].text
        assert "load_config" in segs[0].text
