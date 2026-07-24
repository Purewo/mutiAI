"""Small JSONL client for a locally managed Codex App Server process."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Self


class CodexAppServerError(RuntimeError):
    """Raised when the App Server process or JSON-RPC request fails."""


class CodexAppServerSession:
    """Own one App Server process and one initialized JSONL connection.

    The session deliberately exposes product-neutral Thread and Turn payloads.
    The Runtime adapter stores only selected IDs and summaries in the product
    database; callers may consume the full notification stream separately.
    """

    def __init__(
        self,
        *,
        cwd: str | Path,
        command: Sequence[str] = ("codex", "app-server", "--listen", "stdio://"),
        env: Mapping[str, str] | None = None,
        client_name: str = "mutiai",
        client_title: str = "mutiAI Runtime",
        client_version: str = "0.1.0",
        experimental_api: bool = False,
    ) -> None:
        self.cwd = Path(cwd).expanduser().resolve(strict=True)
        if not self.cwd.is_dir():
            raise CodexAppServerError("Codex App Server cwd must be a directory")
        self.command = tuple(command)
        self.env = dict(env) if env is not None else None
        self.client_name = client_name
        self.client_title = client_title
        self.client_version = client_version
        self.experimental_api = experimental_api
        self._process: subprocess.Popen[str] | None = None
        self._reader_thread: Thread | None = None
        self._responses: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._events: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._rpc_lock = Lock()
        self._write_lock = Lock()
        self._next_request_id = 1

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def connect(self) -> dict[str, Any]:
        """Start the owned process and complete the required handshake."""

        if self._process is not None:
            raise CodexAppServerError("Codex App Server session is already connected")
        process_env = os.environ.copy()
        if self.env is not None:
            process_env.update(self.env)
        executable = shutil.which(self.command[0], path=process_env.get("PATH"))
        resolved_command = [executable or self.command[0], *self.command[1:]]
        try:
            self._process = subprocess.Popen(
                resolved_command,
                cwd=self.cwd,
                env=process_env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except OSError as exc:
            raise CodexAppServerError("failed to start Codex App Server") from exc
        self._reader_thread = Thread(
            target=self._read_messages,
            name="mutiai-codex-app-server-reader",
            daemon=True,
        )
        self._reader_thread.start()
        try:
            result = self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": self.client_name,
                        "title": self.client_title,
                        "version": self.client_version,
                    },
                    "capabilities": {
                        "experimentalApi": self.experimental_api,
                    },
                },
            )
            self.notify("initialized", {})
            return result
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """Stop only the App Server process owned by this session."""

        process = self._process
        self._process = None
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.terminate()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Send one request and return its result while preserving notifications."""

        self._require_process()
        with self._rpc_lock:
            request_id = self._next_request_id
            self._next_request_id += 1
            self._write(
                {
                    "method": method,
                    "id": request_id,
                    "params": dict(params or {}),
                }
            )
            message = self._get_response(timeout)
            if message is None:
                raise CodexAppServerError(
                    f"Codex App Server exited while waiting for '{method}'"
                )
            if message.get("id") != request_id:
                raise CodexAppServerError("unexpected Codex App Server response ID")
            if "error" in message:
                error = message["error"]
                raise CodexAppServerError(
                    f"Codex App Server request '{method}' failed: "
                    f"{error.get('message', error)}"
                )
            result = message.get("result")
            if not isinstance(result, dict):
                raise CodexAppServerError(
                    f"Codex App Server request '{method}' returned no object"
                )
            return result

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification without waiting for a response."""

        self._require_process()
        self._write({"method": method, "params": dict(params or {})})

    def read_account(self, *, refresh_token: bool = False) -> dict[str, Any]:
        """Read the authentication state owned by this Codex home."""

        return self.request(
            "account/read",
            {"refreshToken": refresh_token},
        )

    def start_device_code_login(self) -> dict[str, Any]:
        """Start a managed ChatGPT device-code login ceremony."""

        return self.request(
            "account/login/start",
            {"type": "chatgptDeviceCode"},
        )

    def wait_for_login(
        self,
        *,
        login_id: str,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Wait for the terminal notification for one managed login."""

        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise CodexAppServerError("timed out waiting for Codex login")
            event = self.next_event(timeout=remaining)
            params = event.get("params")
            if (
                event.get("method") == "account/login/completed"
                and isinstance(params, dict)
                and params.get("loginId") == login_id
            ):
                return params

    def start_thread(
        self,
        *,
        model: str | None = None,
        approval_policy: str = "on-request",
        sandbox: str = "workspace-write",
    ) -> dict[str, Any]:
        """Start a new thread bound to the validated managed cwd."""

        params: dict[str, Any] = {
            "cwd": str(self.cwd),
            "approvalPolicy": approval_policy,
            "sandbox": sandbox,
        }
        if model is not None:
            params["model"] = model
        return self.request("thread/start", params)

    def resume_thread(
        self,
        thread_id: str,
        *,
        model: str | None = None,
        approval_policy: str = "on-request",
    ) -> dict[str, Any]:
        """Resume a previously recorded thread and check its cwd binding."""

        params: dict[str, Any] = {
            "threadId": thread_id,
            "cwd": str(self.cwd),
            "approvalPolicy": approval_policy,
        }
        if model is not None:
            params["model"] = model
        return self.request("thread/resume", params)

    def start_turn(
        self,
        *,
        thread_id: str,
        instructions: str,
        execution_id: str,
        output_schema: Mapping[str, Any] | None = None,
        approval_policy: str = "on-request",
    ) -> dict[str, Any]:
        """Start a turn and return its immediate Turn object.

        The request does not wait for model or tool execution. Use
        :meth:`wait_for_turn` or consume :meth:`next_event` from a Runtime
        worker that owns this session.
        """

        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": instructions}],
            "cwd": str(self.cwd),
            "approvalPolicy": approval_policy,
            "sandboxPolicy": {
                "type": "workspaceWrite",
                "writableRoots": [str(self.cwd)],
                "networkAccess": False,
            },
            "clientUserMessageId": execution_id,
        }
        if output_schema is not None:
            params["outputSchema"] = dict(output_schema)
        return self.request("turn/start", params)

    def wait_for_turn(
        self,
        *,
        thread_id: str,
        turn_id: str,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Wait for the terminal notification for one turn."""

        deadline = time.monotonic() + timeout if timeout is not None else None
        completed_items: list[dict[str, Any]] = []
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise CodexAppServerError("timed out waiting for Codex turn")
            event = self.next_event(timeout=remaining)
            params = event.get("params")
            if (
                event.get("method") == "item/completed"
                and isinstance(params, dict)
                and params.get("threadId") == thread_id
                and params.get("turnId") == turn_id
            ):
                item = params.get("item")
                if isinstance(item, dict):
                    completed_items.append(item)
                continue
            turn_payload = params.get("turn") if isinstance(params, dict) else None
            if (
                event.get("method") == "turn/completed"
                and isinstance(params, dict)
                and params.get("threadId") == thread_id
                and isinstance(turn_payload, dict)
                and turn_payload.get("id") == turn_id
            ):
                turn = turn_payload
                # The live App Server protocol emits completed response items
                # as notifications and may leave turn.items empty. Preserve
                # those items only inside the Runtime worker so the adapter
                # can extract a final summary without copying Codex history
                # into LangGraph state.
                existing_items = turn.get("items")
                if not isinstance(existing_items, list):
                    existing_items = []
                if completed_items:
                    seen_ids = {
                        item.get("id")
                        for item in existing_items
                        if isinstance(item, dict) and isinstance(item.get("id"), str)
                    }
                    turn = dict(turn)
                    turn["items"] = existing_items + [
                        item
                        for item in completed_items
                        if not (
                            isinstance(item.get("id"), str)
                            and item.get("id") in seen_ids
                        )
                    ]
                return turn

    def next_event(self, *, timeout: float | None = None) -> dict[str, Any]:
        """Return the next server notification."""

        try:
            event = self._events.get(timeout=timeout)
        except queue.Empty as exc:
            raise CodexAppServerError("timed out waiting for Codex App Server") from exc
        if event is None:
            raise CodexAppServerError("Codex App Server exited")
        if "_mutiai_error" in event:
            raise CodexAppServerError(str(event["_mutiai_error"]))
        return event

    def _read_messages(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    error = {"_mutiai_error": f"invalid App Server JSON: {exc}"}
                    self._responses.put(error)
                    self._events.put(error)
                    continue
                if isinstance(message, dict):
                    if "id" in message and "method" in message:
                        try:
                            self._reject_server_request(message)
                        except CodexAppServerError:
                            pass
                    elif "id" in message:
                        self._responses.put(message)
                    else:
                        self._events.put(message)
        finally:
            self._responses.put(None)
            self._events.put(None)

    def _get_response(self, timeout: float | None) -> dict[str, Any] | None:
        try:
            message = self._responses.get(timeout=timeout)
        except queue.Empty as exc:
            raise CodexAppServerError("timed out waiting for Codex App Server") from exc
        if message is not None and "_mutiai_error" in message:
            raise CodexAppServerError(str(message["_mutiai_error"]))
        return message

    def _reject_server_request(self, message: Mapping[str, Any]) -> None:
        self._write(
            {
                "id": message["id"],
                "error": {
                    "code": -32601,
                    "message": "mutiAI does not handle this App Server request",
                },
            }
        )

    def _write(self, message: Mapping[str, Any]) -> None:
        with self._write_lock:
            process = self._require_process()
            if process.stdin is None:
                raise CodexAppServerError("Codex App Server stdin is unavailable")
            try:
                process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise CodexAppServerError("Codex App Server stdin is closed") from exc

    def _require_process(self) -> subprocess.Popen[str]:
        if self._process is None:
            raise CodexAppServerError("Codex App Server session is not connected")
        return self._process
