"""Explicit in-process dependency adapter for deterministic service unit tests.

Production always uses supervised processes; local test closures cannot be
serialized as worker specifications and must never enable a runtime fallback.
"""

from src.workbench.service import WorkbenchService as ProductionService


class InlineWorker:
    def validate(self, executor, mode):
        pass

    def run(
        self,
        executor,
        *,
        mode,
        files,
        messages,
        token,
        on_chunk,
        publish,
        run_id,
        state_root,
        store,
        resource,
    ):
        try:
            if mode in {"project_chat", "repair"}:
                return executor(
                    mode=mode,
                    files=files,
                    messages=messages,
                    token=token,
                    on_chunk=on_chunk,
                    publish=publish,
                    run_id=run_id,
                    state_root=state_root,
                )
            return executor(messages, token, on_chunk)
        finally:
            store.save_resource(run_id, {**resource, "state": "stopped", "backend": "unit_test"})


class WorkbenchService(ProductionService):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, _worker_runner=InlineWorker(), **kwargs)
