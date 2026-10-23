from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from holo_growth import db
from holo_growth.crm.local import LocalCRM


@pytest.fixture
def conn() -> sqlite3.Connection:
    return db.connect(":memory:")


@pytest.fixture
def crm(conn: sqlite3.Connection) -> LocalCRM:
    return LocalCRM(conn)


@pytest.fixture
def write_csv(tmp_path: Path):  # type: ignore[no-untyped-def]
    def _write(text: str, name: str = "leads.csv") -> Path:
        p = tmp_path / name
        p.write_text(text.strip() + "\n", encoding="utf-8")
        return p

    return _write
