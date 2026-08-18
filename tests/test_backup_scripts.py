"""Проверка скриптов бэкапа/восстановления (ЭПИК-8: TASK-081).

Статические проверки (синтаксис bash, обязательные элементы, запрет бэкапа
volumes) + end-to-end на Linux: backup.sh → архив+sha256+ротация,
restore.sh → распаковка без/с --with-env.
На macOS (нет sha256sum) e2e-тесты пропускаются — полная проверка
выполняется на VPS (этап DevOps по ЧТЗ §4).
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKUP_SH = PROJECT_ROOT / "scripts" / "backup.sh"
RESTORE_SH = PROJECT_ROOT / "scripts" / "restore.sh"

HAVE_SHA256SUM = shutil.which("sha256sum") is not None
HAVE_BASH = shutil.which("bash") is not None

pytestmark = pytest.mark.skipif(not HAVE_BASH, reason="bash не установлен")


def run_bash(
    script: Path, *args: str, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:  # noqa: E501
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def make_src(root: Path) -> Path:
    """Макет /root/monitoring с обязательными объектами из ЧТЗ §2."""
    src = root / "monitoring"
    (src / "config").mkdir(parents=True)
    (src / "prometheus").mkdir()
    (src / "grafana" / "dashboards").mkdir(parents=True)
    (src / "grafana" / "provisioning").mkdir()
    (src / "config" / "sites.yml").write_text("sites: []\n", encoding="utf-8")
    (src / "prometheus" / "prometheus.yml").write_text("global:\n", encoding="utf-8")
    (src / "grafana" / "dashboards" / "site-overview.json").write_text("{}", encoding="utf-8")
    (src / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (src / "Dockerfile").write_text("FROM python:3.12\n", encoding="utf-8")
    (src / ".env").write_text("SECRET=1\n", encoding="utf-8")
    return src


def make_env(src: Path, backup_dir: Path) -> dict[str, str]:
    return {"MONITORING_DIR": str(src), "BACKUP_DIR": str(backup_dir)}


class TestScriptsExist:
    def test_files_exist_and_executable(self) -> None:
        for script in (BACKUP_SH, RESTORE_SH):
            assert script.is_file(), f"отсутствует {script}"
            mode = script.stat().st_mode
            assert mode & stat.S_IXUSR, f"{script.name} не исполняется владельцем"

    def test_bash_syntax(self) -> None:
        for script in (BACKUP_SH, RESTORE_SH):
            res = subprocess.run(
                ["bash", "-n", str(script)], capture_output=True, text=True, check=False
            )
            assert res.returncode == 0, f"синтаксис {script.name}: {res.stderr}"


class TestBackupContent:
    def test_backup_required_elements(self) -> None:
        text = BACKUP_SH.read_text(encoding="utf-8")
        for token in (
            "set -euo pipefail",
            "sha256sum",
            "chmod 600",
            "chmod 700",
            "logger -t monitoring-backup",
            "tar -czf",
            "config prometheus grafana docker-compose.yml",
            "tail -n +",
        ):
            assert token in text, f"backup.sh: нет обязательного элемента «{token}»"

    def test_backup_excludes_volumes_and_code(self) -> None:
        text = BACKUP_SH.read_text(encoding="utf-8")
        for forbidden in ("var/lib/docker", "volumes/", "app/", "collectors/", "tests/"):
            assert forbidden not in text, f"backup.sh бэкапит лишнее (нашёл «{forbidden}»)"

    def test_restore_required_elements(self) -> None:
        text = RESTORE_SH.read_text(encoding="utf-8")
        for token in (
            "set -euo pipefail",
            "--with-env",
            "sha256sum -c",
            "mktemp -d",
            "chmod 600",
            "docker compose -p monitoring up -d --force-recreate",
        ):
            assert token in text, f"restore.sh: нет обязательного элемента «{token}»"


@pytest.mark.skipif(not HAVE_SHA256SUM, reason="sha256sum отсутствует (macOS)")
class TestBackupRestoreEndToEnd:
    def test_backup_creates_archive_sha256_and_rights(self, tmp_path: Path) -> None:
        src = make_src(tmp_path)
        backup_dir = tmp_path / "backups"
        res = run_bash(BACKUP_SH, env_extra=make_env(src, backup_dir))
        assert res.returncode == 0, res.stderr

        archives = sorted(backup_dir.glob("monitoring-*.tar.gz"))
        assert len(archives) == 1
        archive = archives[0]
        assert archive.stat().st_mode & stat.S_IRUSR
        assert not archive.stat().st_mode & stat.S_IROTH
        assert (backup_dir / f"{archive.name}.sha256").is_file()

        listing = subprocess.run(
            ["tar", "-tzf", str(archive)], capture_output=True, text=True, check=True
        ).stdout
        for expected in ("config/sites.yml", ".env", "docker-compose.yml", "Dockerfile"):
            assert expected in listing
        assert "var/lib/docker" not in listing

    def test_backup_rotation_keeps_14(self, tmp_path: Path) -> None:
        src = make_src(tmp_path)
        backup_dir = tmp_path / "backups"
        for i in range(16):
            stamp = f"20260801-0300{i:02d}"
            old = backup_dir / f"monitoring-{stamp}.tar.gz"
            backup_dir.mkdir(parents=True, exist_ok=True)
            old.write_text(f"old-{i}\n", encoding="utf-8")
            (backup_dir / f"monitoring-{stamp}.tar.gz.sha256").write_text("x\n", encoding="utf-8")
        res = run_bash(BACKUP_SH, env_extra=make_env(src, backup_dir))
        assert res.returncode == 0, res.stderr
        remaining = list(backup_dir.glob("monitoring-*.tar.gz"))
        assert len(remaining) == 14
        # Свежий реальный архив должен остаться, старейший фиктивный — удалён.
        assert not (backup_dir / "monitoring-20260801-030000.tar.gz").exists()
        assert not (backup_dir / "monitoring-20260801-030000.tar.gz.sha256").exists()

    def test_restore_without_env_keeps_existing(self, tmp_path: Path) -> None:
        src = make_src(tmp_path)
        backup_dir = tmp_path / "backups"
        assert run_bash(BACKUP_SH, env_extra=make_env(src, backup_dir)).returncode == 0
        archive = next(backup_dir.glob("monitoring-*.tar.gz"))

        dest = tmp_path / "restored"
        dest.mkdir()
        (dest / ".env").write_text("SECRET=existing\n", encoding="utf-8")

        res = run_bash(RESTORE_SH, str(archive), str(dest))
        assert res.returncode == 0, res.stderr
        assert (dest / ".env").read_text(encoding="utf-8") == "SECRET=existing\n"
        assert (dest / "config" / "sites.yml").is_file()
        assert (dest / "grafana" / "dashboards" / "site-overview.json").is_file()

    def test_restore_with_env(self, tmp_path: Path) -> None:
        src = make_src(tmp_path)
        backup_dir = tmp_path / "backups"
        assert run_bash(BACKUP_SH, env_extra=make_env(src, backup_dir)).returncode == 0
        archive = next(backup_dir.glob("monitoring-*.tar.gz"))

        dest = tmp_path / "restored-fresh"
        res = run_bash(RESTORE_SH, str(archive), str(dest), "--with-env")
        assert res.returncode == 0, res.stderr
        assert (dest / ".env").read_text(encoding="utf-8") == "SECRET=1\n"
        assert not (dest / ".env").stat().st_mode & stat.S_IROTH

    def test_restore_corrupted_archive_fails(self, tmp_path: Path) -> None:
        src = make_src(tmp_path)
        backup_dir = tmp_path / "backups"
        assert run_bash(BACKUP_SH, env_extra=make_env(src, backup_dir)).returncode == 0
        archive = next(backup_dir.glob("monitoring-*.tar.gz"))
        archive.write_bytes(b"corrupted")
        res = run_bash(RESTORE_SH, str(archive), str(tmp_path / "dest"))
        assert res.returncode != 0
        assert "sha256" in (res.stdout + res.stderr)

    def test_backup_missing_source_fails(self, tmp_path: Path) -> None:
        res = run_bash(BACKUP_SH, env_extra=make_env(tmp_path / "nope", tmp_path / "b"))
        assert res.returncode != 0
