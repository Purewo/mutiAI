"""Small JSONL client for a locally managed Codex App Server process."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import queue
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Self
from urllib.error import URLError
from urllib.parse import SplitResult, urlsplit
from urllib.request import Request, urlopen

from websockets.sync.client import connect as websocket_connect
from websockets.sync.client import unix_connect

from mutiai.runtime.base import RuntimeTokenUsage

logger = logging.getLogger(__name__)


class CodexAppServerError(RuntimeError):
    """Raised when the App Server process or JSON-RPC request fails."""


class _RuntimeUsageAccumulator:
    """Convert cumulative Thread updates into usage observed for one Turn."""

    def __init__(self) -> None:
        self._baseline: RuntimeTokenUsage | None = None
        self._latest_total: RuntimeTokenUsage | None = None
        self._fallback = RuntimeTokenUsage()
        self._observed = False

    def add(self, payload: Mapping[str, Any]) -> None:
        last = self._parse(payload.get("last"))
        total = self._parse(payload.get("total"))
        if last is None and total is None:
            return
        self._observed = True
        if total is not None and last is not None:
            if (
                self._latest_total is not None
                and total.total_tokens < self._latest_total.total_tokens
            ) or self._baseline is None:
                self._baseline = self._subtract(total, last)
            self._latest_total = total
            return
        if last is not None:
            self._fallback = self._add(self._fallback, last)
        elif total is not None:
            self._baseline = RuntimeTokenUsage()
            self._latest_total = total

    def result(self) -> RuntimeTokenUsage | None:
        if not self._observed:
            return None
        if self._latest_total is not None and self._baseline is not None:
            return self._subtract(self._latest_total, self._baseline)
        return self._fallback

    @staticmethod
    def _parse(value: Any) -> RuntimeTokenUsage | None:
        if not isinstance(value, Mapping):
            return None

        def count(name: str) -> int:
            item = value.get(name, 0)
            return item if isinstance(item, int) and not isinstance(item, bool) else 0

        return RuntimeTokenUsage(
            input_tokens=count("inputTokens"),
            cached_input_tokens=count("cachedInputTokens"),
            output_tokens=count("outputTokens"),
            reasoning_output_tokens=count("reasoningOutputTokens"),
            total_tokens=count("totalTokens"),
        )

    @staticmethod
    def _add(left: RuntimeTokenUsage, right: RuntimeTokenUsage) -> RuntimeTokenUsage:
        return RuntimeTokenUsage(
            input_tokens=left.input_tokens + right.input_tokens,
            cached_input_tokens=(
                left.cached_input_tokens + right.cached_input_tokens
            ),
            output_tokens=left.output_tokens + right.output_tokens,
            reasoning_output_tokens=(
                left.reasoning_output_tokens + right.reasoning_output_tokens
            ),
            total_tokens=left.total_tokens + right.total_tokens,
        )

    @staticmethod
    def _subtract(
        left: RuntimeTokenUsage,
        right: RuntimeTokenUsage,
    ) -> RuntimeTokenUsage:
        return RuntimeTokenUsage(
            input_tokens=max(0, left.input_tokens - right.input_tokens),
            cached_input_tokens=max(
                0,
                left.cached_input_tokens - right.cached_input_tokens,
            ),
            output_tokens=max(0, left.output_tokens - right.output_tokens),
            reasoning_output_tokens=max(
                0,
                left.reasoning_output_tokens - right.reasoning_output_tokens,
            ),
            total_tokens=max(0, left.total_tokens - right.total_tokens),
        )


def validate_codex_app_server_endpoint(endpoint: str) -> SplitResult:
    """Validate a local App Server endpoint and return its parsed URL."""

    parsed = urlsplit(endpoint)
    if parsed.scheme in {"ws", "wss"}:
        host = parsed.hostname
        if (
            host is None
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise CodexAppServerError(
                "Codex App Server WebSocket endpoint is invalid"
            )
        if host.lower() != "localhost":
            try:
                address = ipaddress.ip_address(host)
            except ValueError as exc:
                raise CodexAppServerError(
                    "Codex App Server WebSocket endpoint must use a loopback host"
                ) from exc
            if not address.is_loopback:
                raise CodexAppServerError(
                    "Codex App Server WebSocket endpoint must use a loopback host"
                )
        return parsed
    if parsed.scheme == "unix":
        path = endpoint.removeprefix("unix://")
        if not path or not Path(path).is_absolute():
            raise CodexAppServerError(
                "unix:// endpoint must include an absolute socket path"
            )
        return parsed
    raise CodexAppServerError(
        "Codex App Server endpoint must use ws://, wss://, or unix://"
    )


def require_codex_app_server_ready(
    endpoint: str,
    *,
    timeout: float = 5.0,
) -> None:
    """Require the configured App Server transport to accept connections."""

    parsed = validate_codex_app_server_endpoint(endpoint)
    if timeout <= 0:
        raise ValueError("App Server readiness timeout must be positive")
    try:
        if parsed.scheme in {"ws", "wss"}:
            health_scheme = "https" if parsed.scheme == "wss" else "http"
            health_url = f"{health_scheme}://{parsed.netloc}/readyz"
            request = Request(health_url, headers={"User-Agent": "mutiai"})
            with urlopen(request, timeout=timeout) as response:
                if response.status != 200:
                    raise CodexAppServerError(
                        f"Codex App Server readiness returned HTTP {response.status}"
                    )
            return
        websocket = unix_connect(
            endpoint.removeprefix("unix://"),
            open_timeout=timeout,
            close_timeout=2,
        )
        websocket.close()
    except CodexAppServerError:
        raise
    except (OSError, URLError, TimeoutError) as exc:
        raise CodexAppServerError(
            f"Codex App Server endpoint is not ready: {endpoint}"
        ) from exc


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
        endpoint: str | None = None,
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
        self.endpoint = endpoint
        self.env = dict(env) if env is not None else None
        self.client_name = client_name
        self.client_title = client_title
        self.client_version = client_version
        self.experimental_api = experimental_api
        self._process: subprocess.Popen[str] | None = None
        self._websocket: Any | None = None
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
        """Connect a process or external endpoint and complete the handshake."""

        if self._process is not None or self._websocket is not None:
            raise CodexAppServerError("Codex App Server session is already connected")
        if self.endpoint is None:
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
            reader = self._read_messages
        else:
            try:
                self._websocket = self._connect_endpoint(self.endpoint)
            except Exception as exc:
                raise CodexAppServerError(
                    "failed to connect to Codex App Server endpoint"
                ) from exc
            reader = self._read_websocket_messages
        self._reader_thread = Thread(
            target=reader,
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
        """Close this connection and stop a process owned by this session."""

        process = self._process
        websocket = self._websocket
        self._process = None
        self._websocket = None
        if websocket is not None:
            try:
                websocket.close()
            except Exception as exc:
                logger.debug("failed to close App Server WebSocket", exc_info=exc)
            return
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

        self._require_connected()
        with self._rpc_lock:
            request_id = self._next_request_id
            self._next_request_id += 1
            self._write(
                {
                    "method": method,
                    "id": request_id,
                    "params": None if params is None else dict(params),
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

        self._require_connected()
        self._write({"method": method, "params": dict(params or {})})

    def read_account(self, *, refresh_token: bool = False) -> dict[str, Any]:
        """Read the authentication state owned by this Codex home."""

        return self.request(
            "account/read",
            {"refreshToken": refresh_token},
        )

    def read_account_rate_limits(self) -> dict[str, Any]:
        """Read ChatGPT account rate limits when this auth mode provides them."""

        return self.request("account/rateLimits/read", None)

    def read_account_usage(self) -> dict[str, Any]:
        """Read account token-activity summaries, not product-attributed usage."""

        return self.request("account/usage/read", None)

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
        sandbox: str = "workspace-write",
    ) -> dict[str, Any]:
        """Resume a previously recorded thread and check its cwd binding."""

        params: dict[str, Any] = {
            "threadId": thread_id,
            "cwd": str(self.cwd),
            "approvalPolicy": approval_policy,
            "sandbox": sandbox,
        }
        if model is not None:
            params["model"] = model
        return self.request("thread/resume", params)

    def read_thread(
        self,
        thread_id: str,
        *,
        include_turns: bool = True,
    ) -> dict[str, Any]:
        """Read persisted Thread and Turn state without starting a Turn."""

        return self.request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": include_turns},
        )

    def start_turn(
        self,
        *,
        thread_id: str,
        instructions: str,
        execution_id: str,
        output_schema: Mapping[str, Any] | None = None,
        approval_policy: str = "on-request",
        model: str | None = None,
        reasoning_effort: str | None = None,
        sandbox_mode: str = "workspace-write",
        network_access: bool = False,
    ) -> dict[str, Any]:
        """Start a turn and return its immediate Turn object.

        The request does not wait for model or tool execution. Use
        :meth:`wait_for_turn` or consume :meth:`next_event` from a Runtime
        worker that owns this session.
        """

        if sandbox_mode == "danger-full-access":
            sandbox_policy: dict[str, Any] = {"type": "dangerFullAccess"}
        elif sandbox_mode == "workspace-write":
            sandbox_policy = {
                "type": "workspaceWrite",
                "writableRoots": [str(self.cwd)],
                "networkAccess": network_access,
            }
        else:
            raise ValueError(f"unsupported Codex sandbox mode '{sandbox_mode}'")

        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": instructions}],
            "cwd": str(self.cwd),
            "approvalPolicy": approval_policy,
            "sandboxPolicy": sandbox_policy,
            "clientUserMessageId": execution_id,
        }
        if model is not None:
            params["model"] = model
        if reasoning_effort is not None:
            params["effort"] = reasoning_effort
        if output_schema is not None:
            params["outputSchema"] = dict(output_schema)
        return self.request("turn/start", params)

    def interrupt_turn(
        self,
        *,
        thread_id: str,
        turn_id: str,
    ) -> dict[str, Any]:
        """Request interruption of one active Turn."""

        return self.request(
            "turn/interrupt",
            {
                "threadId": thread_id,
                "turnId": turn_id,
            },
        )

    def wait_for_turn(
        self,
        *,
        thread_id: str,
        turn_id: str,
        timeout: float | None = None,
        server_request_handler: Callable[
            [Mapping[str, Any]], Mapping[str, Any]
        ]
        | None = None,
    ) -> dict[str, Any]:
        """Wait for the terminal notification for one turn."""

        deadline = time.monotonic() + timeout if timeout is not None else None
        completed_items: list[dict[str, Any]] = []
        usage = _RuntimeUsageAccumulator()
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise CodexAppServerError("timed out waiting for Codex turn")
            event = self.next_event(timeout=remaining)
            if "id" in event and isinstance(event.get("method"), str):
                if server_request_handler is None:
                    self._reject_server_request(event)
                else:
                    result = server_request_handler(event)
                    if not isinstance(result, Mapping):
                        raise CodexAppServerError(
                            "App Server request handler returned no object"
                        )
                    self.respond_server_request(event["id"], result)
                continue
            params = event.get("params")
            if (
                event.get("method") == "thread/tokenUsage/updated"
                and isinstance(params, dict)
                and params.get("threadId") == thread_id
                and params.get("turnId") == turn_id
            ):
                token_usage = params.get("tokenUsage")
                if isinstance(token_usage, Mapping):
                    usage.add(token_usage)
                continue
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
                observed_usage = usage.result()
                if observed_usage is not None:
                    turn = dict(turn)
                    turn["_mutiai_usage"] = observed_usage
                turn = dict(turn)
                turn["_mutiai_context_compactions"] = sum(
                    1
                    for item in turn.get("items", [])
                    if isinstance(item, dict)
                    and item.get("type") in {"context_compaction", "compaction"}
                )
                return turn

    def respond_server_request(
        self,
        request_id: str | int,
        result: Mapping[str, Any],
    ) -> None:
        """Respond to one server-initiated JSON-RPC request."""

        self._write({"id": request_id, "result": dict(result)})

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
                        self._events.put(message)
                    elif "id" in message:
                        self._responses.put(message)
                    else:
                        self._events.put(message)
        finally:
            self._responses.put(None)
            self._events.put(None)

    def _read_websocket_messages(self) -> None:
        websocket = self._websocket
        if websocket is None:
            return
        try:
            while True:
                payload = websocket.recv()
                if isinstance(payload, bytes):
                    payload = payload.decode("utf-8")
                self._dispatch_message(payload)
        except Exception as exc:  # noqa: BLE001 - transport boundary
            if self._websocket is websocket:
                self._events.put({"_mutiai_error": f"App Server connection closed: {exc}"})
        finally:
            self._responses.put(None)
            self._events.put(None)

    def _dispatch_message(self, payload: str) -> None:
        try:
            message = json.loads(payload)
        except json.JSONDecodeError as exc:
            error = {"_mutiai_error": f"invalid App Server JSON: {exc}"}
            self._responses.put(error)
            self._events.put(error)
            return
        if not isinstance(message, dict):
            return
        if "id" in message and "method" in message:
            self._events.put(message)
        elif "id" in message:
            self._responses.put(message)
        else:
            self._events.put(message)

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
            websocket = self._websocket
            if websocket is not None:
                try:
                    websocket.send(json.dumps(message, ensure_ascii=False))
                except Exception as exc:
                    raise CodexAppServerError(
                        "Codex App Server WebSocket is closed"
                    ) from exc
                return
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

    def _require_connected(self) -> None:
        if self._process is None and self._websocket is None:
            raise CodexAppServerError("Codex App Server session is not connected")

    @staticmethod
    def _connect_endpoint(endpoint: str) -> Any:
        parsed = validate_codex_app_server_endpoint(endpoint)
        if parsed.scheme in {"ws", "wss"}:
            return websocket_connect(endpoint, open_timeout=30, close_timeout=2)
        return unix_connect(
            endpoint.removeprefix("unix://"),
            open_timeout=30,
            close_timeout=2,
        )
