"""Actual durable cut points, including process termination without cleanup."""

import os
import subprocess
import sys

import pytest

from agent_runtime.plan_runtime.recovery import recover
from tests.plan_support import session_for, simple_plan, through_analysis, write


class SimulatedCrash(BaseException):
    pass


@pytest.mark.parametrize(
    "point,expected",
    [
        ("prepared", "ready"),
        ("dispatched", "uncertain"),
        ("tool_prepared", "uncertain"),
        ("tool_dispatched", "uncertain"),
        ("tool_result_recorded", "succeeded"),
        ("result_recorded", "succeeded"),
        ("reducer_saved", "succeeded"),
        ("reconciled", "succeeded"),
        ("before_checkpoint", "succeeded"),
        ("checkpoint_saved", "succeeded"),
    ],
)
def test_write_crash_cut_points(tmp_path, point, expected):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        through_analysis(session)

        def fault(actual):
            if actual == point:
                raise SimulatedCrash()

        session.fault = fault
        with pytest.raises(SimulatedCrash):
            session.run_node("edit", lambda a: write(session, a))
    with session_for(tmp_path) as restored:
        recover(restored, stopped_probe=lambda a: True)
        assert restored.plan.node("edit").status == expected
        if expected == "uncertain":
            assert restored.plan.node("verify").status == "blocked"
            with pytest.raises(ValueError, match="not_ready"):
                restored.prepare("edit")


def test_partial_write_is_not_replayed(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        through_analysis(session)

        def partial(a):
            from tests.plan_support import tool

            def raw():
                (tmp_path / "value.py").write_text("partial")
                raise SimulatedCrash()

            tool(session, "write_file", {}, raw)

        with pytest.raises(SimulatedCrash):
            session.run_node("edit", partial)
    with session_for(tmp_path) as restored:
        report = recover(restored, stopped_probe=lambda a: True)
        assert restored.plan.node("edit").status == "uncertain"
        assert report["uncertain"][0]["changed_paths"] == ["value.py"]
        assert (tmp_path / "value.py").read_text() == "partial"


def test_read_and_verify_restart_only_when_stopped(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.prepare("read-0")
    with session_for(tmp_path) as restored:
        report = recover(restored, stopped_probe=lambda a: True)
        assert report["restarted_read"] == ["read-0"]
        through_analysis(restored)
        restored.run_node("edit", lambda a: write(restored, a))
        restored.prepare("verify")
    with session_for(tmp_path) as restored:
        report = recover(restored, stopped_probe=lambda a: True)
        assert report["rerun_verify"] == ["verify"]
        assert restored.plan.node("verify").status == "ready"


@pytest.mark.parametrize(
    "point,expected",
    [
        ("tool_dispatched", "uncertain"),
        ("tool_result_recorded", "succeeded"),
        ("result_recorded", "succeeded"),
        ("reducer_saved", "succeeded"),
    ],
)
def test_process_exit_recovers_durable_facts(tmp_path, point, expected):
    (tmp_path / "value.py").write_text("value = 1\n")
    code = """
import os, sys
from tests.plan_support import session_for, simple_plan, through_analysis, write
with session_for(sys.argv[1]) as session:
    session.create(simple_plan(session))
    through_analysis(session)
    session.fault = lambda actual: os._exit(73) if actual == sys.argv[2] else None
    session.run_node('edit', lambda a: write(session, a))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), point],
        timeout=30,
        env={**os.environ, "PYTHONPATH": os.getcwd()},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 73, result.stderr
    with session_for(tmp_path) as restored:
        report = recover(restored)
        assert restored.plan.node("edit").status == expected
        assert bool(report["uncertain"]) == (expected == "uncertain")
        operations = restored.store.latest("operation", "operation_id")
        assert len([o for o in operations.values() if o["tool"] == "write_file"]) == 1


@pytest.mark.parametrize("confirmed_stop", [False, True])
def test_interrupted_verifier_requires_real_stop_evidence(tmp_path, confirmed_stop):
    from agent_runtime.plan_runtime.processes import confirmed_exited

    (tmp_path / "value.py").write_text("value = 1\n")
    # This fixture verifier performs no subprocess launch: its process identity
    # is the complete known execution tree, so process exit proves it stopped.
    code = """
import os, sys
from tests.plan_support import session_for, simple_plan, through_analysis, write
with session_for(sys.argv[1]) as session:
    session.create(simple_plan(session))
    through_analysis(session)
    session.run_node('edit', lambda a: write(session, a))
    session.prepare('verify')
    os._exit(75)
"""
    child = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        timeout=30,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": os.getcwd()},
    )
    assert child.returncode == 75, child.stderr
    with session_for(tmp_path) as restored:
        probe = (lambda a: confirmed_exited(a["owner"])) if confirmed_stop else None
        report = recover(restored, stopped_probe=probe)
        if confirmed_stop:
            assert report["rerun_verify"] == ["verify"]
            assert restored.plan.node("verify").status == "ready"
        else:
            assert restored.plan.node("verify").status == "uncertain"


def test_interrupted_process_capable_read_requires_cleanup_proof(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    code = """
import os, sys
from dataclasses import replace
from tests.plan_support import session_for, simple_plan, tool
with session_for(sys.argv[1]) as session:
    plan = simple_plan(session)
    first = replace(plan.nodes[0], tool_name='grep', tool_allowlist=('grep',))
    session.create(replace(plan, nodes=(first, *plan.nodes[1:])).seal())
    session.fault = lambda point: os._exit(77) if point == 'tool_dispatched' else None
    session.run_node('read-0', lambda a: tool(session, 'grep', {}, lambda: 'matches'))
"""
    child = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        timeout=30,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": os.getcwd()},
    )
    assert child.returncode == 77, child.stderr
    with session_for(tmp_path) as restored:
        recover(restored)
        assert restored.plan.node("read-0").status == "uncertain"
