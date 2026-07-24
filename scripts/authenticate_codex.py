"""Authenticate the isolated mutiAI Codex home with a device code."""

from __future__ import annotations

from typing import Any

from mutiai.config import get_settings
from mutiai.runtime import CodexAppServerError, CodexAppServerSession, WorkspaceManager
from mutiai.services.workspaces import WorkspaceProvisioner


def require_text(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise CodexAppServerError(f"Codex login response has no {field}")
    return value


def main() -> int:
    settings = get_settings()
    manager = WorkspaceManager(settings.runtime_workspace_root)
    codex_home = WorkspaceProvisioner(manager).ensure_codex_home()

    with CodexAppServerSession(
        cwd=codex_home,
        env={"CODEX_HOME": str(codex_home)},
        client_title="mutiAI Runtime Authentication",
    ) as session:
        current = session.read_account()
        account = current.get("account")
        if isinstance(account, dict):
            account_type = account.get("type", "unknown")
            plan_type = account.get("planType")
            suffix = f" ({plan_type})" if plan_type else ""
            print(
                f"Isolated Codex home is already authenticated: {account_type}{suffix}"
            )
            return 0

        login = session.start_device_code_login()
        login_id = require_text(login, "loginId")
        verification_url = require_text(login, "verificationUrl")
        user_code = require_text(login, "userCode")
        print(f"Open: {verification_url}", flush=True)
        print(f"Code: {user_code}", flush=True)
        print("Waiting for authorization...", flush=True)

        completed = session.wait_for_login(login_id=login_id, timeout=900)
        if completed.get("success") is not True:
            error = completed.get("error") or "unknown authentication error"
            raise CodexAppServerError(f"Codex login failed: {error}")

        authenticated = session.read_account(refresh_token=False).get("account")
        if not isinstance(authenticated, dict):
            raise CodexAppServerError("Codex login completed without an account")
        account_type = authenticated.get("type", "unknown")
        plan_type = authenticated.get("planType")
        suffix = f" ({plan_type})" if plan_type else ""
        print(f"Authenticated isolated Codex home: {account_type}{suffix}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
