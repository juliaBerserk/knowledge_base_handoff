"""Восстановление схемы Alembic, данных, аудита и прав на двух временных БД."""

import argparse
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
BASE = ROOT / "docker-compose.yml"
ENV_FILE = ROOT / ".env.example"


def run(*args: str, cwd: Path = ROOT, capture: bool = False) -> str:
    result = subprocess.run(
        args, cwd=cwd, check=True, text=True, stdout=subprocess.PIPE if capture else None,
        timeout=180,
    )
    return result.stdout.strip() if capture else ""


def compose(project: str, override: Path) -> tuple[str, ...]:
    return ("docker", "compose", "--env-file", str(ENV_FILE), "-p", project,
            "-f", str(BASE), "-f", str(override))


def query(config: tuple[str, ...], sql: str) -> str:
    return run(*config, "exec", "-T", "db", "psql", "-XqAt", "-U", "postgres",
               "-d", os.environ["POSTGRES_DB"], "-v", "ON_ERROR_STOP=1", "-c", sql, capture=True)


def configure() -> None:
    env = {
        name: secrets.token_hex(16) for name in (
            "POSTGRES_PASSWORD", "ESTAFETA_OWNER_PASSWORD", "ESTAFETA_APP_PASSWORD",
            "ESTAFETA_ADMIN_PASSWORD", "ESTAFETA_WORKER_PASSWORD", "APP_SECRET_KEY",
        )
    }
    os.environ.update(env, POSTGRES_DB="estafeta_backup_test")


def migrate(config: tuple[str, ...]) -> None:
    port = run(*config, "port", "db", "5432", capture=True).rsplit(":", 1)[-1]
    name = os.environ["POSTGRES_DB"]
    for setting, role in (("OWNER", "owner"), ("APP", "app"), ("ADMIN", "admin")):
        password = os.environ[f"ESTAFETA_{setting}_PASSWORD"]
        variable = "DATABASE_URL" if setting == "APP" else f"DATABASE_{setting}_URL"
        os.environ[variable] = (
            f"postgresql+psycopg://estafeta_{role}:{password}@127.0.0.1:{port}/{name}"
        )
    run(sys.executable, "-m", "alembic", "upgrade", "head", cwd=BACKEND)
    query(config, """
        INSERT INTO knowledge.employees(id, full_name, role)
        VALUES ('10000000-0000-4000-8000-000000000001', 'Backup Fixture', 'employee');
        INSERT INTO auth.credentials(employee_id, password_hash)
        SELECT id, 'fixture-hash' FROM knowledge.employees;
        INSERT INTO auth.sessions(
            token_hash, employee_id, created_at, last_seen_at, enrollment_only)
        SELECT decode(repeat('a0', 32), 'hex'), id, now(), now(), false FROM knowledge.employees;
        INSERT INTO knowledge.spaces(id, kind, employee_id)
        SELECT '10000000-0000-4000-8000-000000000002', 'personal', id FROM knowledge.employees;
        INSERT INTO knowledge.materials(id, space_id, space_kind, author_id, status,
                                        cleaned_text, source_kind)
        SELECT '10000000-0000-4000-8000-000000000003', spaces.id, 'personal', employees.id,
               'draft', 'Backup content fixture', 'note' FROM knowledge.employees
        JOIN knowledge.spaces ON spaces.employee_id = employees.id;
        INSERT INTO audit.events(action, outcome) VALUES ('login', 'success');
    """)


def try_restore(
    cmd: tuple[str, ...], archive: Path, key: Path
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [*cmd, "restore", "--archive", str(archive), "--identity", str(key)],
        capture_output=True, timeout=180,
    )


def test_backup(folder: Path) -> None:
    configure()
    tag = secrets.token_hex(5)
    source_name, target_name = (f"estafeta-backup-{side}-{tag}" for side in ("src", "dst"))
    override = folder / "ports.yml"
    override.write_text('services:\n  db:\n    ports: !override ["127.0.0.1::5432"]\n')
    source, target = [compose(project, override) for project in (source_name, target_name)]
    prefix = (sys.executable, str(ROOT / "scripts/backup.py"), "--env-file", str(ENV_FILE))
    source_cli = (*prefix, "--project-name", source_name)
    target_cli = (*prefix, "--project-name", target_name)
    key, damaged = folder / "restore.age-key", folder / "damaged.age"
    recipients = folder / "recipients.txt"
    run("age-keygen", "-o", str(key))
    recipients.write_text(run("age-keygen", "-y", str(key), capture=True) + "\n", encoding="ascii")
    try:
        run(*source, "up", "-d", "--wait", "db")
        migrate(source)
        run(*target, "up", "-d", "--wait", "db")
        archive = Path(run(*source_cli, "backup", "--directory", str(folder),
                           "--recipients", str(recipients), capture=True))
        encrypted = archive.read_bytes()
        assert encrypted.startswith(b"age-encryption.org/v1\n")
        assert b"Backup content fixture" not in encrypted
        damaged.write_bytes(encrypted[:-1])
        assert try_restore(target_cli, damaged, key).returncode == 1
        assert query(target, "SELECT to_regclass('knowledge.materials') IS NULL") == "t"
        run(*target_cli, "restore", "--archive", str(archive), "--identity", str(key))
        assert query(target, """
            SELECT cleaned_text, (SELECT count(*) FROM audit.events),
                (SELECT count(*) FROM auth.sessions),
                NOT has_table_privilege('estafeta_admin', 'knowledge.materials', 'SELECT'),
                relrowsecurity
            FROM knowledge.materials
            JOIN pg_class ON pg_class.oid = 'knowledge.materials'::regclass
        """) == "Backup content fixture|1|0|t|t"
        assert query(target, """
            SET ROLE estafeta_app;
            SELECT count(*) FROM knowledge.materials
        """) == "0"
        assert query(target, """
            SET ROLE estafeta_app;
            SET app.user_id = '10000000-0000-4000-8000-000000000001';
            SELECT count(*) FROM knowledge.materials
        """) == "1"
        assert query(target, """
            SELECT NOT has_function_privilege(
                'estafeta_admin', 'knowledge.rank_chunk_ids(text,text,vector,text,integer)',
                'EXECUTE'
            ) AND NOT has_function_privilege(
                'estafeta_app', 'knowledge.rank_vectors(vector,text,jsonb,integer)', 'EXECUTE'
            )
        """) == "t"
        assert try_restore(target_cli, archive, key).returncode == 1
        assert query(target, "SELECT count(*) FROM knowledge.materials") == "1"
    finally:
        for config in (source, target):
            subprocess.run([*config, "down", "--volumes"], cwd=ROOT, check=False, timeout=90)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="estafeta-backup-") as directory:
        test_backup(Path(directory))
    print("Копия, восстановление, отказ для повреждённой копии, аудит и RLS проверены.")


if __name__ == "__main__":
    main()
