"""Run the persistent local Codex App Server sidecar for mutiAI."""

from __future__ import annotations

import argparse
import os
import shutil
from collections.abc import Sequence

from mutiai.config import get_settings
from mutiai.runtime import (
    CodexAppServerSidecar,
    CodexSidecarRestartPolicy,
    WorkspaceManager,
    require_codex_app_server_ready,
    validate_codex_app_server_endpoint,
)
from mutiai.services.workspaces import WorkspaceProvisioner


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run and supervise the local mutiAI Codex App Server sidecar."
    )
    parser.add_argument(
        "--max-restarts",
        type=int,
        default=10,
        help="Maximum restarts after the initial process (default: 10).",
    )
    parser.add_argument(
        "--restart-backoff-seconds",
        type=float,
        default=0.5,
        help="Initial exponential restart delay (default: 0.5).",
    )
    parser.add_argument(
        "--restart-backoff-max-seconds",
        type=float,
        default=5.0,
        help="Maximum exponential restart delay (default: 5).",
    )
    parser.add_argument(
        "--startup-timeout-seconds",
        type=float,
        default=10.0,
        help="Readiness window for each process start (default: 10).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    endpoint = settings.codex_app_server_endpoint
    validate_codex_app_server_endpoint(endpoint)

    manager = WorkspaceManager(settings.runtime_workspace_root)
    codex_home = WorkspaceProvisioner(manager).ensure_codex_home()
    executable = shutil.which("codex")
    if executable is None:
        raise RuntimeError("codex executable was not found on PATH")

    process_env = os.environ.copy()
    process_env["CODEX_HOME"] = str(codex_home)
    policy = CodexSidecarRestartPolicy(
        max_restarts=args.max_restarts,
        initial_backoff_seconds=args.restart_backoff_seconds,
        max_backoff_seconds=args.restart_backoff_max_seconds,
        startup_timeout_seconds=args.startup_timeout_seconds,
    )
    sidecar = CodexAppServerSidecar(
        command=[executable, "app-server", "--listen", endpoint],
        cwd=codex_home,
        env=process_env,
        ready_probe=lambda: require_codex_app_server_ready(
            endpoint,
            timeout=min(1.0, policy.startup_timeout_seconds),
        ),
        policy=policy,
    )
    print(f"mutiAI Codex App Server: {endpoint}")
    print(f"Managed CODEX_HOME: {codex_home}")
    return sidecar.run()


if __name__ == "__main__":
    raise SystemExit(main())
