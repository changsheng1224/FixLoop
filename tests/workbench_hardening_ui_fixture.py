"""Seed durable UI states only; this fixture does not execute a model or repair."""

from agent_runtime.cancellation import CancellationToken


def seed(service, root):
    cases = {}
    for kind in ("candidate", "recovery", "blocked"):
        project = root / ("ui-" + kind)
        project.mkdir()
        for name in ("a.txt", "b.txt"):
            (project / name).write_bytes(b"old\n")
        chat = service.create_conversation("UI " + kind, project_path=str(project))
        turn = service._append(chat.id, "Review fixture candidate", "repair", None)
        snapshot = next(
            s
            for s in service.store.get_tree(chat.id).snapshots
            if s.id == turn.node.input_snapshot_id
        )
        workspace = service.workspaces.create(turn.run.id, snapshot.manifest, CancellationToken())
        for name in ("a.txt", "b.txt"):
            (workspace / name).write_bytes(b"new\n")
        output = service.workspaces.capture(turn.run.id, snapshot.manifest["root_identity"])
        service.store.start_run(chat.id, turn.run.id)
        service.store.finish_run(
            chat.id,
            turn.run.id,
            answer="UI fixture candidate",
            artifacts={"isolated": True},
            output_snapshot=output,
            candidate={
                "input_key": snapshot.storage_key,
                "project_revision": service.store.project_record(chat.id)["revision"],
            },
        )
        if kind == "recovery":
            plan = service.preview_candidate(chat.id, turn.run.id)

            def fault(where, index):
                if where in {"after_step", "recovery_before_step"} and index == 0:
                    raise RuntimeError("UI fixture partial application")

            service.restoration.fault = fault
            try:
                service.apply_restore(chat.id, plan["id"])
            except RuntimeError:
                try:
                    service.recover_restore(chat.id, plan["id"], decision="rollback")
                except RuntimeError:
                    pass
            finally:
                service.restoration.fault = lambda *_: None
        if kind == "blocked":
            service.store.save_resource(
                turn.run.id,
                {
                    "state": "unknown",
                    "backend": "unknown",
                    "run_id": turn.run.id,
                    "conversation_id": chat.id,
                },
            )
            observer = service.create_conversation("UI observer", project_path=str(project))
            cases["observer"] = {"id": observer.id, "project": str(project)}
        cases[kind] = {"id": chat.id, "run_id": turn.run.id, "project": str(project)}
    return cases
