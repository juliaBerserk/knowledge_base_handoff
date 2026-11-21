"""Зашифрованные копии PostgreSQL для оператора Docker Compose."""

import argparse
import os
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
DB_EXEC = (
    "sh", "-ec",
    'exec "$@" --no-password --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"',
    "estafeta",
)
# Готовая к работе или уже восстановленная БД содержит прикладные схемы.
EMPTY_CHECK = (
    "SELECT NOT EXISTS (SELECT FROM pg_namespace "
    "WHERE nspname IN ('auth', 'knowledge', 'audit')) "
    "AND to_regclass('public.alembic_version') IS NULL"
)


def db_command(project: str, env_file: Path) -> list[str]:
    return [
        "docker", "compose", "--project-name", project, "--env-file", str(env_file.resolve()),
        "-f", str(ROOT / "docker-compose.yml"), "exec", "-T", "db", *DB_EXEC,
    ]


def pipe(source: list[str], target: list[str], sink: int = subprocess.DEVNULL) -> None:
    with subprocess.Popen(source, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE) as producer:
        assert producer.stdout is not None
        try:
            with subprocess.Popen(target, stdin=producer.stdout, stdout=sink) as consumer:
                producer.stdout.close()
                target_status = consumer.wait()
            source_status = producer.wait()
        except BaseException:
            producer.stdout.close()
            producer.kill()
            raise
    if source_status or target_status:
        raise RuntimeError("Операция прервана. Проверьте сообщения PostgreSQL и age.")


def backup(db: list[str], directory: Path, recipients: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    archive = directory / f"estafeta-{stamp}-{uuid4().hex[:12]}.pgdump.age"
    with tempfile.TemporaryDirectory(prefix=".estafeta-", dir=directory) as scratch:
        pending = Path(scratch) / "encrypted.age"
        pipe(
            [
                *db, "pg_dump", "--format=custom",
                "--exclude-table-data=auth.sessions",
                "--exclude-table-data=auth.invitations",
                "--exclude-table-data=auth.rate_limits",
            ],
            ["age", "--encrypt", "--recipients-file", str(recipients), "--output", str(pending)],
        )
        with pending.open("rb+") as ciphertext:
            os.fsync(ciphertext.fileno())
        pending.replace(archive)
    return archive


def restore(db: list[str], archive: Path, identity: Path) -> None:
    result = subprocess.check_output(
        [*db, "psql", "-XAt", "--set=ON_ERROR_STOP=1", "--command", EMPTY_CHECK],
        stdin=subprocess.DEVNULL,
    )
    if result.strip() != b"t":
        raise RuntimeError("Для восстановления нужен новый экземпляр БД без схем приложения.")
    with tempfile.TemporaryDirectory(prefix="estafeta-restore-") as scratch:
        snapshot = Path(scratch) / "encrypted.age"
        shutil.copyfile(archive, snapshot)
        decrypt = ["age", "--decrypt", "--identity", str(identity), str(snapshot)]
        # Проверяем весь шифротекст до запуска pg_restore. Оба прохода читают одну копию.
        subprocess.run(decrypt, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, check=True)
        pipe(decrypt, [*db, "pg_restore", "--single-transaction", "--exit-on-error"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--env-file", required=True, type=Path)
    commands = parser.add_subparsers(dest="operation", required=True)
    export = commands.add_parser("backup", help="создать зашифрованную копию")
    export.add_argument("--directory", required=True, type=Path)
    export.add_argument("--recipients", required=True, type=Path)
    import_ = commands.add_parser("restore", help="восстановить в изолированный экземпляр")
    import_.add_argument("--archive", required=True, type=Path)
    import_.add_argument("--identity", required=True, type=Path)
    args = parser.parse_args()
    db = db_command(args.project_name, args.env_file)
    try:
        if args.operation == "backup":
            print(backup(db, args.directory.resolve(), args.recipients.resolve()))
        else:
            restore(db, args.archive.resolve(), args.identity.resolve())
            print("Копия восстановлена.")
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Резервное копирование или восстановление не завершено: {error}\n")


if __name__ == "__main__":
    main()
