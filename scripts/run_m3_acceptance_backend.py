"""Run an isolated loopback backend for one M3 browser acceptance scenario."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

from mutiai.config import Settings
from mutiai.main import create_app
from mutiai.runtime import (
    CodexRuntimeAdapter,
    FakeRuntimeAdapter,
    RuntimeWorkspaceBinding,
    WorkspaceManager,
)
from mutiai.services.workspaces import WorkspaceProvisioner

SCENARIOS = ("wait-cancel", "needs-revision", "approval")
FAKE_APP_SERVER = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "support"
    / "fake_codex_app_server.py"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a loopback-only backend with deterministic M3 acceptance state."
        )
    )
    parser.add_argument("--scenario", choices=SCENARIOS, required=True)
    parser.add_argument("--port", type=int, default=8000)
    return parser.parse_args()


def build_settings(scenario: str, port: int) -> Settings:
    state_root = Path("var") / "m3-acceptance" / scenario
    state_root.mkdir(parents=True, exist_ok=True)
    workspace_root = Settings().runtime_workspace_root / "m3-acceptance" / scenario
    fake_scenario = {
        "wait-cancel": "wait_first_specialist",
        "needs-revision": "needs_revision",
        "approval": "default",
    }[scenario]
    return Settings(
        app_env="development",
        app_host="127.0.0.1",
        app_port=port,
        database_url=(
            "sqlite+pysqlite:///"
            f"{(state_root / 'mutiai.db').resolve().as_posix()}"
        ),
        database_auto_migrate=True,
        langgraph_checkpoint_path=(state_root / "checkpoints.db").resolve(),
        runtime_provider="codex" if scenario == "approval" else "fake",
        fake_runtime_scenario=fake_scenario,
        runtime_security_mode=(
            "workspace_restricted" if scenario == "approval" else "demo_full_access"
        ),
        runtime_workspace_root=workspace_root,
        assistant_runtime_provider="fake",
    )


def build_app(scenario: str, settings: Settings):
    assistant_adapter = FakeRuntimeAdapter()
    if scenario != "approval":
        return create_app(
            settings,
            assistant_runtime_adapter=assistant_adapter,
        )

    if not FAKE_APP_SERVER.is_file():
        raise FileNotFoundError(f"Fake App Server is missing: {FAKE_APP_SERVER}")
    workspace_manager = WorkspaceManager(settings.runtime_workspace_root)
    codex_home = WorkspaceProvisioner(workspace_manager).ensure_codex_home()

    def unexpected_resolver(execution_id: str) -> RuntimeWorkspaceBinding:
        raise RuntimeError(
            "Acceptance Runtime expected an explicit product Workspace for "
            f"execution '{execution_id}'"
        )

    runtime_adapter = CodexRuntimeAdapter(
        workspace_manager=workspace_manager,
        resolve_workspace=unexpected_resolver,
        codex_home=codex_home,
        command=(sys.executable, str(FAKE_APP_SERVER)),
    )
    return create_app(
        settings,
        runtime_adapter=runtime_adapter,
        assistant_runtime_adapter=assistant_adapter,
    )


def main() -> None:
    args = parse_args()
    settings = build_settings(args.scenario, args.port)
    app = build_app(args.scenario, settings)
    print(
        f"M3 acceptance scenario '{args.scenario}' is isolated under "
        f"{Path('var/m3-acceptance') / args.scenario}."
    )
    print(
        "Seed it from another terminal with: uv run python "
        "scripts/seed_m3_acceptance_task.py "
        f"--scenario {args.scenario} --port {args.port}"
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
