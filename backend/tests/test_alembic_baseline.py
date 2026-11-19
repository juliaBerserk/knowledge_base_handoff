from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_auth_revision_follows_org_model() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    script = ScriptDirectory.from_config(config)
    head = script.get_revision("head")
    assert head is not None
    assert head.revision == "0003_auth"
    assert head.down_revision == "0002_org_model"
