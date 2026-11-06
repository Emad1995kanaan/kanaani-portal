import sqlite3

from alembic import command

from tests.conftest import _alembic_cfg


def test_migration_round_trip(tmp_path):
    url = f"sqlite:///{tmp_path}/mig.db"
    cfg = _alembic_cfg()
    cfg.attributes["url"] = url
    command.upgrade(cfg, "head")
    con = sqlite3.connect(tmp_path / "mig.db")
    tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    assert {"users", "plans", "customers", "subscriptions", "transactions", "audit_logs", "sessions", "login_attempts"} <= tables
    assert con.execute("select count(*) from plans").fetchone()[0] == 4
    con.close()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
