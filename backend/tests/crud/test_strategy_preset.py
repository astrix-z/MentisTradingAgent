"""PRESET-05a: the preset tables behave as plan §4.3.1 declares.

Runs against the migrated database of the normal test setup (prestart applies
``alembic upgrade head``), so it checks the migration, not ``create_all``.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, delete, select

from app.models import (
    StrategyBuiltinSync,
    StrategyFieldOptions,
    StrategyPreset,
    User,
)

STRATEGY = "tripple_screen"


def preset(name: str, strategy: str = STRATEGY, **extra: object) -> StrategyPreset:
    return StrategyPreset(strategy=strategy, name=name, params={"tp_pct": "5"}, **extra)


@pytest.fixture(autouse=True)
def clean(db: Session):
    yield
    db.rollback()
    db.execute(delete(StrategyPreset))
    db.execute(delete(StrategyBuiltinSync))
    db.commit()


def test_deleting_the_author_keeps_the_preset(db: Session) -> None:
    author = User(email=f"{uuid.uuid4()}@example.com", hashed_password="x")
    db.add(author)
    db.commit()
    db.add(preset("Mine", created_by=author.id))
    db.commit()
    db.delete(author)
    db.commit()
    kept = db.exec(select(StrategyPreset).where(StrategyPreset.name == "Mine")).one()
    db.refresh(kept)
    assert kept.created_by is None


def test_a_name_is_unique_per_strategy_ignoring_case(db: Session) -> None:
    db.add(preset("Alpha"))
    db.add(preset("alpha", strategy="other_strategy"))
    db.commit()
    db.add(preset("ALPHA"))
    with pytest.raises(IntegrityError) as caught:
        db.commit()
    # The preset API answers 409 only for this constraint (plan §4.3.4).
    assert caught.value.orig is not None
    assert caught.value.orig.diag.constraint_name == "ux_strategy_preset_name"


def test_raw_sql_may_leave_out_the_service_columns(db: Session) -> None:
    """R84: the sync inserts with raw SQL; the database fills the rest."""
    db.execute(
        text(
            "INSERT INTO strategy_preset (strategy, name, params) "
            "VALUES (:strategy, 'Raw', '{}'::jsonb)"
        ),
        {"strategy": STRATEGY},
    )
    db.commit()
    row = db.exec(select(StrategyPreset).where(StrategyPreset.name == "Raw")).one()
    assert row.is_builtin is False
    assert isinstance(row.id, uuid.UUID)
    assert row.created_at is not None and row.created_at == row.updated_at


def test_the_sync_row_starts_as_never_and_rejects_unknown_status(db: Session) -> None:
    db.add(StrategyBuiltinSync(strategy=STRATEGY))
    db.commit()
    row = db.get(StrategyBuiltinSync, STRATEGY)
    assert row is not None
    assert (row.content_hash, row.status, row.errors, row.synced_at) == (
        "",
        "never",
        [],
        None,
    )
    row.status = "bogus"
    with pytest.raises(IntegrityError):
        db.commit()


def test_the_migration_seeds_the_t2_option_lists(db: Session) -> None:
    rows = db.exec(
        select(StrategyFieldOptions)
        .where(StrategyFieldOptions.strategy == STRATEGY)
        .order_by(col(StrategyFieldOptions.field))
    ).all()
    assert {row.field: row.options for row in rows} == {
        "quote_asset": ["USDT", "USDC"],
        "ripple_interval": ["12h", "6h", "4h", "2h", "1h", "30m", "15m", "5m", "1m"],
        "tide_interval": ["1w", "1d", "12h", "6h", "4h", "2h", "1h", "30m", "15m"],
        "wave_interval": ["1d", "12h", "6h", "4h", "2h", "1h", "30m", "15m", "5m"],
    }
