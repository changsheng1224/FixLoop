"""Local deterministic server for the S5 browser acceptance script (no provider I/O)."""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.workbench.http import WorkbenchHTTPServer  # noqa: E402
from src.workbench.project import ProjectResult  # noqa: E402
from src.workbench.service import WorkbenchService  # noqa: E402
from tests.test_workbench_service import result  # noqa: E402

TOKEN = "s5-browser-fixture-token-local-only"


def chat(messages, token, chunk):
    text = messages[-1]["content"]
    if text == "等待停止":
        chunk("正在等待停止")
        while not token.is_cancelled:
            time.sleep(0.05)
        token.check()
    if text == "重连测试":
        parts = ["第一段", "第二段", "第三段"]
        for part in parts:
            token.check()
            chunk(part)
            time.sleep(0.7)
        return result("".join(parts))
    answer = "回答：" + text + "\n\n```python\nvalue = 1\n```"
    chunk(answer)
    return result(answer)


def project(**kw):
    path = kw["files"].root / "value.txt"
    text = kw["messages"][-1]["content"]
    kw["publish"]("project.progress", {"stage": "test", "tool": "read_file"})
    if kw["mode"] == "project_chat":
        return ProjectResult("当前文件：" + path.read_text(encoding="utf-8"))
    path.write_text(text, encoding="utf-8")
    if text == "部分失败":
        raise RuntimeError("fixture failure")
    report = {
        "verified": True,
        "verification": {
            "all_passed": True,
            "total_tests": 1,
            "passed": 1,
            "failed": 0,
            "error": 0,
        },
    }
    kw["publish"]("project.report", report)
    return ProjectResult("修复完成，验证通过。", artifacts=report)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    with WorkbenchService(
        root / "state", executor=chat, project_executor=project, project_lock_dir=root / "locks"
    ) as service:
        interrupted = root / "interrupted"
        interrupted.mkdir()
        (interrupted / "value.txt").write_text("base", encoding="utf-8")
        conversation = service.create_conversation("恢复中断验收", project_path=str(interrupted))
        (interrupted / "value.txt").write_text("manual", encoding="utf-8")
        plan = service.preview_restore(conversation.id, action="fork", node_id=None)

        def fail(where, index):
            if where == "after_step":
                raise OSError("test interrupted restore")

        service.restoration.fault = fail
        try:
            service.apply_restore(conversation.id, plan["id"])
        except OSError:
            pass
        service.restoration.fault = lambda *_: None
        service.create_conversation("普通对话验收")
        repo = root / "project"
        repo.mkdir()
        (repo / "value.txt").write_text("base", encoding="utf-8")
        with WorkbenchHTTPServer(("127.0.0.1", 0), service, token=TOKEN) as server:
            print(
                json.dumps(
                    {
                        "url": f"http://127.0.0.1:{server.server_port}",
                        "token": TOKEN,
                        "repo": str(repo),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            try:
                server.serve_forever(poll_interval=0.05)
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
