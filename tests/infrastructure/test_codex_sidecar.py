from collections.abc import Callable
from pathlib import Path

import pytest

from mutiai.runtime import CodexAppServerSidecar, CodexSidecarRestartPolicy


class FakeProcess:
    def __init__(
        self,
        exit_code: int,
        *,
        interrupt: bool = False,
        already_exited: bool = False,
    ) -> None:
        self.returncode: int | None = exit_code if already_exited else None
        self.exit_code = exit_code
        self.interrupt = interrupt
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.interrupt and not self.terminated:
            raise KeyboardInterrupt
        if self.returncode is None:
            self.returncode = self.exit_code
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = self.exit_code

    def kill(self) -> None:
        self.killed = True
        self.returncode = self.exit_code


def build_supervisor(
    processes: list[FakeProcess],
    *,
    policy: CodexSidecarRestartPolicy,
    sleep: Callable[[float], None] | None = None,
    ready_probe: Callable[[], None] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> tuple[CodexAppServerSidecar, list[float], list[list[str]]]:
    sleeps: list[float] = []
    commands: list[list[str]] = []

    def popen(command, **kwargs):
        del kwargs
        commands.append(command)
        return processes.pop(0)

    supervisor = CodexAppServerSidecar(
        command=("codex", "app-server"),
        cwd=Path("managed"),
        env={"CODEX_HOME": "managed"},
        ready_probe=ready_probe or (lambda: None),
        policy=policy,
        popen_factory=popen,
        sleep=sleep or sleeps.append,
        monotonic=monotonic or (lambda: 0.0),
        emit=lambda _: None,
    )
    return supervisor, sleeps, commands


def test_sidecar_restarts_with_bounded_exponential_backoff() -> None:
    policy = CodexSidecarRestartPolicy(
        max_restarts=2,
        initial_backoff_seconds=0.25,
        max_backoff_seconds=0.4,
    )
    supervisor, sleeps, commands = build_supervisor(
        [FakeProcess(11), FakeProcess(12), FakeProcess(0)],
        policy=policy,
    )

    assert supervisor.run() == 1
    assert len(commands) == 3
    assert sleeps == [0.25, 0.4]


def test_sidecar_does_not_restart_before_readiness_when_process_exits() -> None:
    probes: list[str] = []
    policy = CodexSidecarRestartPolicy(max_restarts=0)
    supervisor, _, commands = build_supervisor(
        [FakeProcess(23, already_exited=True)],
        policy=policy,
        ready_probe=lambda: probes.append("probe"),
    )

    assert supervisor.run() == 23
    assert len(commands) == 1
    assert probes == []


def test_sidecar_stops_unready_process_after_startup_timeout() -> None:
    process = FakeProcess(17)
    clock_values = iter((0.0, 0.0, 0.2))
    policy = CodexSidecarRestartPolicy(
        max_restarts=0,
        startup_timeout_seconds=0.1,
        startup_probe_interval_seconds=0.05,
    )

    def not_ready() -> None:
        raise RuntimeError("not ready")

    supervisor, sleeps, _ = build_supervisor(
        [process],
        policy=policy,
        ready_probe=not_ready,
        monotonic=lambda: next(clock_values),
    )

    assert supervisor.run() == 1
    assert process.terminated is True
    assert sleeps == [0.05]


def test_sidecar_keyboard_interrupt_terminates_child() -> None:
    process = FakeProcess(0, interrupt=True)
    policy = CodexSidecarRestartPolicy(max_restarts=3)
    supervisor, _, _ = build_supervisor([process], policy=policy)

    assert supervisor.run() == 130
    assert process.terminated is True
    assert process.killed is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_restarts", -1),
        ("initial_backoff_seconds", -0.1),
        ("max_backoff_seconds", 0),
        ("startup_timeout_seconds", 0),
        ("startup_probe_interval_seconds", 0),
        ("shutdown_timeout_seconds", 0),
    ],
)
def test_sidecar_policy_rejects_invalid_values(field: str, value: float) -> None:
    with pytest.raises(ValueError):
        CodexSidecarRestartPolicy(**{field: value})


def test_sidecar_policy_rejects_backoff_inversion() -> None:
    with pytest.raises(ValueError, match="cannot exceed"):
        CodexSidecarRestartPolicy(
            initial_backoff_seconds=2,
            max_backoff_seconds=1,
        )
