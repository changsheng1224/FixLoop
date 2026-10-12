"""Trusted worker entrypoint; no runtime or project code runs before the go gate."""

import json
import sys


def main(request_path):
    if sys.stdin.readline() != "go\n":
        return
    # All potentially blocking work happens after OS containment and durable
    # registration. The controller alone owns run/candidate/history transitions.
    import threading
    from contextlib import closing, redirect_stdout
    from dataclasses import asdict
    from pathlib import Path

    from agent_runtime.cancellation import CancellationToken
    from agent_runtime.state_root import native_io_path
    from src.workbench.chat import ChatExecutor
    from src.workbench.context import SUMMARY_INSTRUCTION, ConversationContext
    from src.workbench.files import ProjectFiles
    from src.workbench.project import ProjectExecutor, ProjectResult
    from src.workbench.store import WorkbenchStore

    request = json.loads(native_io_path(request_path).read_text(encoding="utf-8"))
    token = CancellationToken()
    output = sys.stdout
    lock = threading.Lock()

    def emit(kind, data):
        with lock:
            output.write(
                json.dumps({"kind": kind, "data": data}, ensure_ascii=False, allow_nan=False) + "\n"
            )
            output.flush()

    def listen():
        for line in sys.stdin:
            command = json.loads(line)
            if command.get("cancel"):
                token.cancel(command["cancel"])
        token.cancel("controller disconnected")

    threading.Thread(target=listen, daemon=True).start()
    try:
        with redirect_stdout(sys.stderr):
            spec = request["spec"]
            config = spec["config"]
            context = request["context"]

            def summarize(data, cancel):
                import time

                from agent_runtime.bootstrap import create_model_client
                from agent_runtime.model_turn import FinishKind, ModelTurnRequest
                from agent_runtime.providers.chat import stream_chat_turn

                client = create_model_client(
                    provider=config["provider"],
                    model=config.get("model"),
                    base_url=config.get("base_url"),
                )
                result = stream_chat_turn(
                    client,
                    ModelTurnRequest(
                        system_prompt=SUMMARY_INSTRUCTION,
                        messages=[
                            {"role": "user", "content": json.dumps(data, ensure_ascii=False)}
                        ],
                        max_output_tokens=1600,
                        deadline=time.monotonic() + config["timeout_s"],
                    ),
                    cancel_token=cancel,
                    on_chunk=lambda _: None,
                )
                if result.finish.kind != FinishKind.TEXT_COMPLETE:
                    raise ValueError("summary did not complete")
                return result.text

            messages, context_report = ConversationContext(
                WorkbenchStore(context["database"]),
                summarize,
                model=config.get("model") or "",
                provider=config["provider"],
                max_bytes=config.get("max_context_bytes", 400_000),
            ).build(
                context["conversation_id"],
                context["node_id"],
                token,
                snapshot_key=context.get("snapshot_key"),
            )
            if spec["kind"] == "project":
                executor = ProjectExecutor(**spec["config"])
                with closing(ProjectFiles(request["workspace"])) as files:
                    result = executor(
                        mode=request["mode"],
                        files=files,
                        messages=messages,
                        token=token,
                        on_chunk=lambda text: emit("chunk", {"text": text}),
                        publish=lambda kind, data: emit(
                            "progress", {"kind": kind, "payload": data}
                        ),
                        run_id=request["run_id"],
                        state_root=Path(request["state_root"]),
                    )
            elif spec["kind"] == "chat":
                result = ChatExecutor(**spec["config"])(
                    messages, token, lambda text: emit("chunk", {"text": text})
                )
                result = ProjectResult(
                    result.text, artifacts={"usage": result.usage, "finish": asdict(result.finish)}
                )
            else:
                raise ValueError("unknown worker specification")
            result.artifacts["context"] = context_report
        token.check()
        emit("result", asdict(result))
    except BaseException as exc:
        from agent_runtime.providers.contracts import ProviderError

        emit(
            "error",
            {
                "error": exc.code.value if isinstance(exc, ProviderError) else type(exc).__name__,
                "cancelled": token.is_cancelled,
                "reason": token.reason,
            },
        )
