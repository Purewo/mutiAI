from pathlib import Path

from mutiai.runtime import WorkspaceManager
from mutiai.services.workspaces import WorkspaceProvisioner


def test_codex_home_bootstrap_copies_only_provider_configuration(tmp_path) -> None:
    source = tmp_path / "interactive-codex-home"
    source.mkdir()
    (source / "config.toml").write_text('model_provider = "proxy"\n')
    (source / "auth.json").write_text('{"auth_mode":"apikey"}\n')
    (source / "history.jsonl").write_text("interactive history\n")
    (source / "sessions").mkdir()
    (source / "sessions" / "existing.jsonl").write_text("existing session\n")

    manager = WorkspaceManager(tmp_path / "managed", protected_roots=())
    provisioner = WorkspaceProvisioner(manager)
    result = provisioner.bootstrap_codex_home(source)

    assert result.copied == ("config.toml", "auth.json")
    assert result.skipped == ()
    assert (result.codex_home / "config.toml").read_text() == (
        'model_provider = "proxy"\n'
    )
    assert (result.codex_home / "auth.json").read_text() == (
        '{"auth_mode":"apikey"}\n'
    )
    assert not (result.codex_home / "history.jsonl").exists()
    assert not (result.codex_home / "sessions").exists()


def test_codex_home_bootstrap_does_not_replace_configuration_by_default(
    tmp_path: Path,
) -> None:
    source = tmp_path / "interactive-codex-home"
    source.mkdir()
    (source / "config.toml").write_text('model = "new"\n')
    (source / "auth.json").write_text('{"token":"new"}\n')

    manager = WorkspaceManager(tmp_path / "managed", protected_roots=())
    provisioner = WorkspaceProvisioner(manager)
    codex_home = provisioner.ensure_codex_home()
    (codex_home / "config.toml").write_text('model = "existing"\n')
    (codex_home / "auth.json").write_text('{"token":"existing"}\n')

    result = provisioner.bootstrap_codex_home(source)

    assert result.copied == ()
    assert result.skipped == ("config.toml", "auth.json")
    assert (codex_home / "config.toml").read_text() == 'model = "existing"\n'
    assert (codex_home / "auth.json").read_text() == '{"token":"existing"}\n'
