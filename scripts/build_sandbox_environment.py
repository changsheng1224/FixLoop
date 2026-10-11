"""Prepare/check the dedicated environment, optionally export an offline release image."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime.linux_sandbox.environment import (  # noqa: E402
    EnvironmentError,
    SandboxEnvironmentManager,
    file_digest,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--home", type=Path)
    parser.add_argument("--package-proxy", default="")
    parser.add_argument("--bundle", type=Path, help="Trusted offline image sidecar")
    parser.add_argument("--export", type=Path, help="New output directory for a prepared image")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        manager = SandboxEnvironmentManager(
            args.workspace,
            home=args.home,
            image_bundle=args.bundle,
            progress=lambda stage: print("environment: " + stage, file=sys.stderr, flush=True),
        )
        environment = manager.ensure(package_proxy=args.package_proxy)
        report = {
            "status": "ready",
            "release_id": environment.release_id,
            "distribution": environment.identity.distribution,
            "runtime_digest": environment.identity.runtime_digest,
            "health": environment.health,
        }
        if args.export:
            # Export only a freshly prepared/checked product-owned runtime, never user Ubuntu.
            args.export.mkdir(parents=True, exist_ok=False)
            image = args.export / "fixloop-sandbox.tar.gz"
            manager.transport.call(["--terminate", manager.distribution])
            manager.transport.call(
                ["--export", manager.distribution, str(image.resolve()), "--format", "tar.gz"],
                timeout=300,
            )
            sidecar = {
                "schema_version": 1,
                "release_id": environment.release_id,
                "runtime_digest": environment.identity.runtime_digest,
                "image": image.name,
                "sha256": file_digest(image),
            }
            (args.export / "bundle.json").write_text(
                json.dumps(sidecar, indent=2) + "\n", encoding="utf-8"
            )
            report["bundle"] = str(args.export / "bundle.json")
    except EnvironmentError as exc:
        report = {"status": "blocked", "error": exc.code, "guidance": list(exc.guidance)}
    except (OSError, ValueError):
        report = {
            "status": "blocked",
            "error": "environment_configuration_or_io_failed",
            "guidance": [],
        }
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    print(text)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    return 0 if report["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
