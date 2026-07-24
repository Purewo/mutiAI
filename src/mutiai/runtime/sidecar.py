"""Supervise an external Codex App Server process without owning API state."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class SidecarProcess(Protocol):
    """Small process surface used by the supervisor and its tests."""

    returncode: int | None

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


PopenFactory = Callable[..., SidecarProcess]


@dataclass(frozen=True, slots=True)
class CodexSidecarRestartPolicy:
    """Bound restart and startup behavior for the local sidecar."""

    max_restarts: int = 10
    initial_backoff_seconds: float = 0.5
    max_backoff_seconds: float = 5.0
    startup_timeout_seconds: float = 10.0
    startup_probe_interval_seconds: float = 0.25
    shutdown_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if self.max_restarts < 0:
            raise ValueError("max_restarts must be non-negative")
        if self.initial_backoff_seconds < 0:
            raise ValueError("initial_backoff_seconds must be non-negative")
        if self.max_backoff_seconds <= 0:
            raise ValueError("max_backoff_seconds must be positive")
        if self.initial_backoff_seconds > self.max_backoff_seconds:
            raise ValueError(
                "initial_backoff_seconds cannot exceed max_backoff_seconds"
            )
        if self.startup_timeout_seconds <= 0:
            raise ValueError("startup_timeout_seconds must be positive")
        if self.startup_probe_interval_seconds <= 0:
            raise ValueError("startup_probe_interval_seconds must be positive")
        if self.shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")


class CodexAppServerSidecar:
    """Run and restart an independent Codex App Server process.

    The sidecar owns only the OS process. Product execution state, Thread IDs,
    and retry decisions remain in the API and database. A process restart does
    not imply that an interrupted Turn can be replayed safely.
    """

    def __init__(
        self,
        *,
        command: Sequence[str],
        cwd: str | Path,
        env: Mapping[str, str],
        ready_probe: Callable[[], None],
        policy: CodexSidecarRestartPolicy | None = None,
        popen_factory: PopenFactory = subprocess.Popen,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        emit: Callable[[str], None] = print,
    ) -> None:
        if not command:
            raise ValueError("sidecar command cannot be empty")
        self.command = tuple(command)
        self.cwd = Path(cwd)
        self.env = dict(env)
        self.ready_probe = ready_probe
        self.policy = policy or CodexSidecarRestartPolicy()
        self._popen = popen_factory
        self._sleep = sleep
        self._monotonic = monotonic
        self._emit = emit

    def run(self) -> int:
        """Run until a clean stop, a user interrupt, or restart exhaustion."""

        restart_count = 0
        while True:
            process = self._popen(
                list(self.command),
                cwd=self.cwd,
                env=self.env,
            )
            try:
                ready = self._wait_until_ready(process)
                if ready:
                    exit_code = process.wait()
                else:
                    exit_code = process.poll()
                    if exit_code is None:
                        self._stop_process(process)
                        exit_code = 1
                    else:
                        exit_code = exit_code or 1
            except KeyboardInterrupt:
                self._stop_process(process)
                return 130

            exit_code = exit_code or 1
            self._emit(f"Codex App Server exited with code {exit_code}")
            if restart_count >= self.policy.max_restarts:
                self._emit("Codex App Server restart budget exhausted")
                return exit_code

            restart_count += 1
            delay = min(
                self.policy.initial_backoff_seconds * (2 ** (restart_count - 1)),
                self.policy.max_backoff_seconds,
            )
            self._emit(
                "Restarting Codex App Server "
                f"({restart_count}/{self.policy.max_restarts}) after {delay:g}s"
            )
            try:
                self._sleep(delay)
            except KeyboardInterrupt:
                return 130

    def _wait_until_ready(self, process: SidecarProcess) -> bool:
        deadline = self._monotonic() + self.policy.startup_timeout_seconds
        last_error: Exception | None = None
        while process.poll() is None:
            try:
                self.ready_probe()
            except Exception as exc:  # noqa: BLE001 - readiness boundary
                last_error = exc
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    self._emit(
                        "Codex App Server readiness timed out: "
                        f"{str(last_error)[:500]}"
                    )
                    return False
                self._sleep(
                    min(self.policy.startup_probe_interval_seconds, remaining)
                )
            else:
                self._emit("Codex App Server is ready")
                return True
        return False

    def _stop_process(self, process: SidecarProcess) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=self.policy.shutdown_timeout_seconds)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=self.policy.shutdown_timeout_seconds)
            except (OSError, subprocess.TimeoutExpired):
                pass
