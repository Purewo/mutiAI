"""Run the persistent local Codex App Server sidecar for mutiAI."""

from __future__ import annotations

import os
import shutil
import subprocess

from mutiai.config import get_settings
from mutiai.runtime import (
    WorkspaceManager,
    validate_codex_app_server_endpoint,
)
from mutiai.services.workspaces import WorkspaceProvisioner


def main() -> int:
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
    process = subprocess.Popen(
        [executable, "app-server", "--listen", endpoint],
        cwd=codex_home,
        env=process_env,
    )
    print(f"mutiAI Codex App Server: {endpoint}")
    print(f"Managed CODEX_HOME: {codex_home}")
    try:
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        try:
            return process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            return process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
