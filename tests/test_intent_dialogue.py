"""Multi-turn dialogue / anaphora resolution tests (history-first)."""

from __future__ import annotations

from agent_runtime.intent.dialogue import (
    DialogueProjection,
    Referent,
    clear_projection,
    load_projection,
    resolve_utterance,
    save_projection,
    update_projection,
)
from agent_runtime.intent.models import RouteContext
from agent_runtime.intent.router import IntentRouter


def _hist(*users: str) -> list[dict]:
    return [{"role": "user", "content": u} for u in users]


class TestResolveHistoryFirst:
    def test_deixis_uses_history_when_no_projection(self):
        history = _hist(
            'Traceback (most recent call last):\n  File "foo.py", line 1\nTypeError: x'
        )
        r = resolve_utterance("刚才那个", history=history)
        assert r.outcome == "resolved"
        assert "TypeError" in r.text or "foo.py" in r.text
        assert r.used_history

    def test_fix_it_from_history_stack(self):
        history = _hist(
            'Traceback (most recent call last):\n  File "calc.py", line 2\nTypeError: bad'
        )
        r = resolve_utterance("修一下", history=history)
        assert r.outcome == "resolved"
        assert "帮我修" in r.text
        assert "calc.py" in r.text or "TypeError" in r.text

    def test_unresolved_without_context(self):
        r = resolve_utterance("刚才那个", history=[])
        assert r.outcome == "unresolved"

    def test_passthrough_normal(self):
        r = resolve_utterance("配置里 timeout 该怎么设？", history=_hist("hi"))
        assert r.outcome == "passthrough"
        assert r.text.startswith("配置里")


class TestClarifyResume:
    def test_choice_merges_original(self):
        proj = DialogueProjection(
            pending_clarify={
                "reason": "ambiguous",
                "original_text": "utils.py 里这段逻辑",
                "question": "?",
            }
        )
        r = resolve_utterance("修", history=[], projection=proj)
        assert r.outcome == "clarify_resume"
        assert "utils.py" in r.text
        assert "修" in r.text or "帮我修" in r.text

    def test_file_answer(self):
        proj = DialogueProjection(
            pending_clarify={
                "reason": "ambiguous",
                "original_text": "帮我修这个 TypeError",
            }
        )
        r = resolve_utterance("foo.py", history=[], projection=proj)
        assert r.outcome == "clarify_resume"
        assert "foo.py" in r.text


class TestRouterMultiTurn:
    def test_router_resolves_fix_it(self):
        history = _hist(
            'Traceback (most recent call last):\n  File "pricing.py", line 3\nTypeError: x'
        )
        proj = DialogueProjection()
        r = IntentRouter().route(
            "修一下",
            RouteContext(channel="repl", history=history, dialogue=proj),
        )
        assert r.primary in ("repair_request", "repair_issue")
        assert (r.raw_signals or {}).get("anaphora", {}).get("outcome") == "resolved"

    def test_router_unresolved_clarify(self):
        r = IntentRouter().route(
            "刚才那个",
            RouteContext(channel="repl", history=[], dialogue=DialogueProjection()),
        )
        assert r.action == "clarify"
        assert r.raw_signals.get("clarify_reason") == "unresolved_anaphora"

    def test_projection_roundtrip_session(self):
        session: dict = {}
        proj = DialogueProjection(
            last_text="解释 config.py",
            last_primary="explain",
            referents=[Referent(kind="file", value="config.py")],
        )
        save_projection(session, proj)
        loaded = load_projection(session)
        assert loaded.last_primary == "explain"
        assert loaded.referents[0].value == "config.py"
        clear_projection(session)
        assert "intent_dialogue" not in session

    def test_two_turn_route_with_projection(self):
        router = IntentRouter()
        t1 = (
            'Traceback (most recent call last):\n'
            '  File "billing.py", line 4\n'
            "AttributeError: x"
        )
        r1 = router.route(t1, RouteContext(channel="repl"))
        proj = update_projection(DialogueProjection(), r1, user_text=t1)
        history = _hist(t1)
        r2 = router.route(
            "修一下",
            RouteContext(channel="repl", history=history, dialogue=proj),
        )
        assert r2.primary in ("repair_request", "repair_issue")
        assert r2.raw_signals.get("anaphora", {}).get("outcome") == "resolved"


class TestUpdateProjection:
    def test_update_after_repair(self):
        result = IntentRouter().route(
            "帮我修这个 TypeError，文件 foo.py",
            RouteContext(channel="repl"),
        )
        proj = DialogueProjection()
        proj = update_projection(proj, result, user_text="帮我修这个 TypeError，文件 foo.py")
        assert proj.last_primary == "repair_request"
        assert proj.pending_clarify is None
        kinds = {r.kind for r in proj.referents}
        assert "file" in kinds or "issue_type" in kinds
