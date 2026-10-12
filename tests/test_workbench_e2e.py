"""S6 end-to-end acceptance with real HTTP providers, workers, tools and pytest."""

import hashlib
import http.client
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from tests.workbench_e2e_support import (
    ModelServer,
    WorkbenchProcess,
    eventually,
    repair_script,
    tool,
)


@pytest.fixture
def model(tmp_path):
    with ModelServer(tmp_path / "model") as server:
        yield server
        assert server.snapshot()["remaining"] == 0, "unconsumed model script"
        assert not any(r["unexpected"] for r in server.snapshot()["requests"])


def project(root):
    repo = root / "project"
    repo.mkdir()
    (repo / "v.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "test_value.py").write_text(
        "from v import value\ndef test_value():\n    assert value in (2, 3, 4)\n", encoding="utf-8"
    )
    return repo


def run_turn(app, cid, text, mode="chat"):
    turn = app.turn(cid, text, mode)
    return turn, app.wait(cid, turn["run"]["id"])


def apply_candidate(app, cid, run):
    plan = app.api(f"/conversations/{cid}/runs/{run['id']}/candidate-preview", "POST", {}, 201)
    return app.api(f"/conversations/{cid}/restores/apply", "POST", {"plan_id": plan["id"]})


def test_cli_chat_protocol_history_and_scope(tmp_path, model):
    with WorkbenchProcess(tmp_path / "cli", model.url, cli=True) as app:
        cid = app.create("写作与日常问答")["id"]
        model.script([{"chunks": [{"text": "春日"}, {"text": "散步记"}]}])
        first, run = run_turn(app, cid, "写一个短标题")
        assert run["status"] == "succeeded" and run["answer"] == "春日散步记"
        model.script([{"text": "第二轮回答"}])
        run_turn(app, cid, "FUTURE_ONLY")
        model.script([{"text": "改写后的标题"}])
        edited = app.api(
            f"/conversations/{cid}/nodes/{first['node']['id']}/edit",
            "POST",
            {"text": "换一个标题"},
            202,
        )
        assert app.wait(cid, edited["run"]["id"])["answer"] == "改写后的标题"
        requests = model.snapshot()["requests"]
        assert all(r["path"] == "/api/chat" for r in requests)
        latest = requests[-1]["body"]
        assert [m["role"] for m in latest["messages"]] == ["system", "user"]
        assert "FUTURE_ONLY" not in json.dumps(latest)
        assert "tools" not in latest
        tree = app.api(f"/conversations/{cid}/tree")
        app.api(f"/conversations/{cid}/turns", "POST", {"text": "repair", "mode": "repair"}, 422)
        assert app.api(f"/conversations/{cid}/tree") == tree
        assert len(tree["branches"]) == 2 and len(tree["runs"]) == 3


def test_real_l1_l2_history_restore_manual_and_isolation(tmp_path, model):
    repo = project(tmp_path)
    baseline = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(repo / "test_value.py"),
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert baseline.returncode == 1 and "1 failed" in baseline.stdout
    with WorkbenchProcess(tmp_path / "app", model.url) as app:
        chat = app.create("项目验收", repo)
        cid, original = chat["id"], chat["active_branch_id"]
        model.script(
            [
                {"text": tool("read_file", path="v.py")},
                {"text": "<final>Current initial value is 1.</final>"},
            ]
        )
        _, qa = run_turn(app, cid, "Read the initial value.", "project_chat")
        assert qa["status"] == "succeeded" and qa["artifacts"]["changed_files"] == []
        assert "1: value = 1" in model.snapshot()["requests"][-1]["body"]["prompt"]
        model.script(repair_script(1, 2))
        first, fixed = run_turn(app, cid, "Change the initial value from 1 to 2.", "repair")
        assert fixed["status"] == "succeeded", fixed
        assert fixed["artifacts"]["verified"] and fixed["artifacts"]["verification"]["passed"] >= 1
        assert fixed["artifacts"]["changed_files"] == ["v.py"]
        assert (repo / "v.py").read_text().strip() == "value = 1"
        apply_candidate(app, cid, fixed)
        model.script(repair_script(2, 3))
        _, future = run_turn(
            app, cid, "FUTURE_ONLY: change the initial value from 2 to 3.", "repair"
        )
        assert future["status"] == "succeeded", future
        apply_candidate(app, cid, future)
        (repo / "v.py").write_text("value = 99\n", encoding="utf-8")
        (repo / "manual.txt").write_text("editor note", encoding="utf-8")
        plan = app.api(
            f"/conversations/{cid}/restores/preview",
            "POST",
            {"action": "input", "node_id": first["node"]["id"]},
            201,
        )
        assert plan["manual"] is True
        start = len(model.snapshot()["requests"])
        model.script(repair_script(1, 4))
        edited = app.api(
            f"/conversations/{cid}/nodes/{first['node']['id']}/edit",
            "POST",
            {"text": "Change the initial value from 1 to 4.", "plan_id": plan["id"]},
            202,
        )
        result = app.wait(cid, edited["run"]["id"])
        assert result["status"] == "succeeded", result
        assert (repo / "v.py").read_text().strip() == "value = 1"
        apply_candidate(app, cid, result)
        assert (repo / "v.py").read_text(encoding="utf-8").strip() == "value = 4"
        assert not (repo / "manual.txt").exists()
        for request in model.snapshot()["requests"][start:]:
            assert "FUTURE_ONLY" not in request["body"]["prompt"]
            assert "value = 99" not in request["body"]["prompt"]
        tree = app.api(f"/conversations/{cid}/tree")
        assert len(tree["branches"]) == 2 and len(tree["runs"]) == 4
        assert any(n["kind"] == "local_checkpoint" for n in tree["nodes"])
        plan = app.api(
            f"/conversations/{cid}/restores/preview",
            "POST",
            {"action": "switch", "branch_id": original},
            201,
        )
        app.api(f"/conversations/{cid}/restores/apply", "POST", {"plan_id": plan["id"]})
        assert (repo / "v.py").read_text(encoding="utf-8").strip() == "value = 99"
        assert (repo / "manual.txt").read_text(encoding="utf-8") == "editor note"
        assert not app.api(f"/conversations/{cid}/workspace")["manual_changes"]
        plan = app.api(
            f"/conversations/{cid}/restores/preview",
            "POST",
            {"action": "input", "node_id": first["node"]["id"]},
            201,
        )
        (repo / "v.py").write_text("value = 100\n", encoding="utf-8")
        before = app.api(f"/conversations/{cid}/tree")
        app.api(f"/conversations/{cid}/restores/apply", "POST", {"plan_id": plan["id"]}, 409)
        assert app.api(f"/conversations/{cid}/tree") == before
        assert "100" in (repo / "v.py").read_text()
        assert not (repo / ".agent").exists()
        runtime = tmp_path / "app/state/runtime"
        assert len([p for p in runtime.iterdir() if p.is_dir()]) == 4


def test_readonly_tools_reject_write_and_protected_reads(tmp_path, model):
    repo = project(tmp_path)
    (repo / ".env").write_text("S6_SECRET_SENTINEL=hidden", encoding="utf-8")
    with WorkbenchProcess(tmp_path / "app", model.url) as app:
        cid = app.create(project=repo)["id"]
        model.script(
            [
                {"text": tool("patch_file", path="v.py", old_text="1", new_text="9")},
                {"text": tool("read_file", path=".env")},
                {"text": "<final>Protected paths and writes are unavailable.</final>"},
            ]
        )
        _, run = run_turn(app, cid, "Try writing and reading protected data.", "project_chat")
        assert run["status"] == "succeeded", run
        assert run["artifacts"]["changed_files"] == []
        assert (repo / "v.py").read_text().strip() == "value = 1"
        prompts = [r["body"]["prompt"] for r in model.snapshot()["requests"]]
        assert all("S6_SECRET_SENTINEL" not in prompt for prompt in prompts)
        assert "error" in prompts[-1].lower() or "denied" in prompts[-1].lower()


@pytest.mark.parametrize("action", ["stop", "timeout", "crash"])
def test_partial_repair_terminal_state_and_restart(tmp_path, model, action):
    repo = project(tmp_path)
    root = tmp_path / "app"
    app = WorkbenchProcess(root, model.url, timeout=10 if action == "timeout" else 40).start()
    try:
        cid = app.create(project=repo)["id"]
        model.script(
            repair_script(
                1,
                2,
                tail={"text": "<final>late</final>", "delay_s": 12 if action == "timeout" else 5},
            )
        )
        turn = app.turn(cid, "Change value to 2 then pause.", "repair")
        work = root / "state" / "workspaces" / turn["run"]["id"] / "tree" / "v.py"
        eventually(lambda: work.exists() and "value = 2" in work.read_text(), timeout=30)
        assert (repo / "v.py").read_text().strip() == "value = 1"
        eventually(lambda: len(model.snapshot()["requests"]) == 4)
        if action == "crash":
            app.kill()
        else:
            if action == "stop":
                app.api(f"/conversations/{cid}/runs/{turn['run']['id']}/stop", "POST", {}, 202)
            # A cancelling project must continue to block restoration while model I/O drains.
            app.api(
                f"/conversations/{cid}/restores/preview",
                "POST",
                {"action": "input", "node_id": turn["node"]["id"]},
                409,
            )
            run = app.wait(cid, turn["run"]["id"], timeout=25)
            assert run["status"] == "cancelled", run
            assert run["output_snapshot_id"]
            assert not app.api(f"/conversations/{cid}/workspace")["candidates"]
            assert (repo / "v.py").read_text().strip() == "value = 1"
            assert run["error"] == ("timeout" if action == "timeout" else "user")
            app.kill()
        count = len(model.snapshot()["requests"])
        with WorkbenchProcess(root, model.url) as restarted:
            run = restarted.api(f"/conversations/{cid}/runs/{turn['run']['id']}")["run"]
            assert run["status"] == ("interrupted" if action == "crash" else "cancelled")
            assert run["output_snapshot_id"]
            assert (repo / "v.py").read_text().strip() == "value = 1"
            tree = restarted.api(f"/conversations/{cid}/tree")
            output = next(s for s in tree["snapshots"] if s["id"] == run["output_snapshot_id"])
            assert (
                output["manifest"]["entries"]["v.py"]["digest"]
                == hashlib.sha256(work.read_bytes()).hexdigest()
            )
            # Read-only viewing after restart must neither re-execute nor reverse files.
            restarted.api(f"/conversations/{cid}/view", "POST", {"node_id": turn["node"]["id"]})
            time.sleep(0.15)
            assert len(model.snapshot()["requests"]) == count
            plan = restarted.api(
                f"/conversations/{cid}/restores/preview",
                "POST",
                {"action": "input", "node_id": turn["node"]["id"]},
                201,
            )
            restarted.api(f"/conversations/{cid}/restores/apply", "POST", {"plan_id": plan["id"]})
            assert (repo / "v.py").read_text().strip() == "value = 1"
    finally:
        app.kill()


@pytest.mark.parametrize("decision", ["complete", "rollback"])
def test_process_death_during_restore_recovery(tmp_path, model, decision):
    repo = project(tmp_path)
    root = tmp_path / "app"
    with WorkbenchProcess(root, model.url, crash_restore=True) as app:
        cid = app.create(project=repo)["id"]
        (repo / "v.py").write_text("value = 99\n", encoding="utf-8")
        (repo / "manual.txt").write_text("preserved manual", encoding="utf-8")
        plan = app.api(
            f"/conversations/{cid}/restores/preview",
            "POST",
            {"action": "fork", "node_id": None},
            201,
        )
        with pytest.raises(
            (http.client.RemoteDisconnected, ConnectionError, urllib.error.URLError)
        ):
            app.api(f"/conversations/{cid}/restores/apply", "POST", {"plan_id": plan["id"]})
        assert app.child.wait(10) == 77
    with WorkbenchProcess(root, model.url) as app:
        workspace = app.api(f"/conversations/{cid}/workspace")
        assert workspace["pending"]
        app.api(f"/conversations/{cid}/turns", "POST", {"text": "blocked", "mode": "repair"}, 409)
        app.api(
            f"/conversations/{cid}/restores/recover",
            "POST",
            {"operation_id": plan["id"], "decision": decision},
        )
        assert not app.api(f"/conversations/{cid}/workspace")["pending"]
        if decision == "complete":
            assert (repo / "v.py").read_text().strip() == "value = 1"
            assert not (repo / "manual.txt").exists()
            assert len(app.api(f"/conversations/{cid}/tree")["branches"]) == 2
        else:
            assert (repo / "v.py").read_text().strip() == "value = 99"
            assert (repo / "manual.txt").read_text() == "preserved manual"
            assert len(app.api(f"/conversations/{cid}/tree")["branches"]) == 1


def test_chat_sse_resume_failure_and_stop(tmp_path, model):
    with WorkbenchProcess(tmp_path / "app", model.url) as app:
        cid = app.create()["id"]
        model.script(
            [
                {
                    "chunks": [
                        {"text": "一"},
                        {"text": "二", "delay_s": 0.4},
                        {"text": "三", "delay_s": 0.4},
                    ]
                }
            ]
        )
        turn = app.turn(cid, "stream")
        base = f"/conversations/{cid}/runs/{turn['run']['id']}"
        url = app.startup["url"].removeprefix("http://").split(":")
        conn = http.client.HTTPConnection(url[0], int(url[1]), timeout=5)
        conn.request(
            "GET",
            "/api" + base + "/events",
            headers={"Authorization": "Bearer " + app.startup["token"]},
        )
        response = conn.getresponse()
        cursor = None
        while True:
            line = response.readline().decode()
            if line.startswith("id: "):
                cursor = int(line.rsplit(":", 1)[1])
            if line.startswith("event: answer.delta"):
                break
        conn.close()
        assert app.wait(cid, turn["run"]["id"])["answer"] == "一二三"
        req = urllib.request
        with req.urlopen(
            req.Request(
                app.startup["url"] + "/api" + base + f"/events?after={cursor}",
                headers={"Authorization": "Bearer " + app.startup["token"]},
            )
        ) as stream:
            replay = stream.read().decode()
        ids = [
            int(line.rsplit(":", 1)[1]) for line in replay.splitlines() if line.startswith("id:")
        ]
        assert ids and min(ids) > cursor and len(ids) == len(set(ids))
        assert "event: run.snapshot" in replay
        model.script([{"text": "partial only", "disconnect": True}])
        _, failed = run_turn(app, cid, "incomplete stream")
        assert failed["status"] == "failed" and failed["answer"] == "partial only"
        model.script([{"chunks": [{"text": "waiting"}, {"text": "late", "delay_s": 3}]}])
        turn = app.turn(cid, "stop")
        eventually(
            lambda: (
                app.api(f"/conversations/{cid}/runs/{turn['run']['id']}")["partial_answer"]
                == "waiting"
            )
        )
        app.api(f"/conversations/{cid}/runs/{turn['run']['id']}/stop", "POST", {}, 202)
        stopped = app.wait(cid, turn["run"]["id"])
        assert stopped["status"] == "cancelled" and "late" not in stopped["answer"]
        time.sleep(3.2)
        assert app.api(f"/conversations/{cid}/runs/{turn['run']['id']}")["run"] == stopped


def test_project_ownership_auth_and_conversation_scope(tmp_path, model):
    repo = project(tmp_path)
    with WorkbenchProcess(tmp_path / "one", model.url) as first:
        conversation = first.create(project=repo)
        cid = conversation["id"]
        http_url = first.startup["url"] + "/api/conversations"
        from tests.workbench_e2e_support import http

        http(http_url, expected=401)
        http(http_url, token="wrong-local-token", expected=401)
        with WorkbenchProcess(tmp_path / "two", model.url) as second:
            second.api("/conversations", "POST", {"project_path": str(repo)}, 409)
            nested = repo / "nested"
            nested.mkdir()
            second.api("/conversations", "POST", {"project_path": str(nested)}, 409)
            assert second.api("/conversations")["conversations"] == []
            other = first.create("unrelated writing")["id"]
            model.script([{"text": "Scope proof"}])
            turn, _ = run_turn(first, other, "ordinary writing")
            first.api(f"/conversations/{cid}/runs/{turn['run']['id']}", expected=404)
            before = first.api(f"/conversations/{cid}/tree")
            first.api(
                f"/conversations/{cid}/nodes/{turn['node']['id']}/edit",
                "POST",
                {"text": "cross-scope"},
                404,
            )
            assert first.api(f"/conversations/{cid}/tree") == before
            first.kill()
            # OS project ownership releases on real process death, even across state dirs.
            assert second.create(project=repo)["project_path"] == str(repo.resolve())
