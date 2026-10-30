from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_baseline_revision_is_head() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert heads == ["0001_baseline"]
