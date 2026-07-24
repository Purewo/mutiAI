"""Bootstrap an isolated mutiAI Codex home from provider credentials only."""

from __future__ import annotations

import argparse
from pathlib import Path

from mutiai.config import get_settings
from mutiai.runtime import WorkspaceManager
from mutiai.services.workspaces import WorkspaceProvisioner


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Copy config.toml and auth.json into the isolated mutiAI Codex home. "
            "Sessions, history, state databases, and existing Threads are not copied."
        )
    )
    parser.add_argument(
        "--source-home",
        type=Path,
        default=Path.home() / ".codex",
        help="Codex configuration source (default: the current user's .codex home)",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace existing config.toml and auth.json in the isolated home",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()
    manager = WorkspaceManager(settings.runtime_workspace_root)
    result = WorkspaceProvisioner(manager).bootstrap_codex_home(
        args.source_home,
        replace=args.replace,
    )
    print(f"Managed Codex home: {result.codex_home}")
    print(f"Copied: {', '.join(result.copied) or 'none'}")
    print(f"Kept existing: {', '.join(result.skipped) or 'none'}")
    print("Interactive sessions and history were not copied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
