import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import EmailStr
from sqlalchemy import CheckConstraint, DateTime, Index, false, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship, SQLModel, col


def get_datetime_utc() -> datetime:
    return datetime.now(UTC)


# Shared properties
class UserBase(SQLModel):
    email: EmailStr = Field(unique=True, index=True, max_length=255)
    is_active: bool = True
    is_superuser: bool = False
    full_name: str | None = Field(default=None, max_length=255)


# Properties to receive via API on creation
class UserCreate(UserBase):
    password: str = Field(min_length=8, max_length=128)


class UserRegister(SQLModel):
    email: EmailStr = Field(max_length=255)
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = Field(default=None, max_length=255)


# Properties to receive via API on update, all are optional
class UserUpdate(SQLModel):
    email: EmailStr | None = Field(default=None, max_length=255)
    is_active: bool | None = None
    is_superuser: bool | None = None
    full_name: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, min_length=8, max_length=128)


class UserUpdateMe(SQLModel):
    full_name: str | None = Field(default=None, max_length=255)
    email: EmailStr | None = Field(default=None, max_length=255)


class UpdatePassword(SQLModel):
    current_password: str = Field(min_length=8, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


# Database model, database table inferred from class name
class User(UserBase, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    hashed_password: str
    created_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
    items: list[Item] = Relationship(back_populates="owner", cascade_delete=True)


# Properties to return via API, id is always required
class UserPublic(UserBase):
    id: uuid.UUID
    created_at: datetime | None = None


class UsersPublic(SQLModel):
    data: list[UserPublic]
    count: int


# Shared properties
class ItemBase(SQLModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=255)


# Properties to receive on item creation
class ItemCreate(ItemBase):
    pass


# Properties to receive on item update
class ItemUpdate(SQLModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=255)


# Database model, database table inferred from class name
class Item(ItemBase, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    created_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    owner: User | None = Relationship(back_populates="items")


# Properties to return via API, id is always required
class ItemPublic(ItemBase):
    id: uuid.UUID
    owner_id: uuid.UUID
    created_at: datetime | None = None


class ItemsPublic(SQLModel):
    data: list[ItemPublic]
    count: int


# A "position" opened by a strategy ENTRY signal (D-SIGNAL-ONLY: the strategy
# never places a real order). Populated by app.core.signal_listener as ENTRY
# signals arrive on Redis; closed lazily on read once the last known price has
# actually crossed its stop or take level (see GET /trading/positions/).
# Plain string status (not a Python/SQL enum type) to match this codebase's
# existing convention (e.g. agent's ``SignalType``/``AgentState`` are
# ``Literal[...]``, not ``enum.Enum``) and to keep the column a plain VARCHAR.
SignalPositionStatus = Literal["open", "hit_take", "hit_stop"]


class SignalPosition(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    signal_id: str = Field(unique=True, index=True)
    symbol: str = Field(index=True)
    network: str
    epoch_id: str
    # Decimal-precise string amounts (never binary float), matching how the
    # agent/backend already carry these values everywhere else.
    entry_ref: str
    swing_low_ref: str  # stop
    tp_rule: str
    take_ref: str  # entry_ref * multiplier parsed from tp_rule, cached once
    emitted_at: datetime = Field(sa_type=DateTime(timezone=True))  # type: ignore
    valid_until: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
    status: str = "open"
    closed_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
    closed_price: str | None = None
    #: Who closed it: ``exit`` — the agent's EXIT, at the bar where the price
    #: first crossed a level and that price; ``view`` — a read that saw the
    #: last known price beyond a level, a fallback the EXIT may still correct.
    closed_by: str | None = None
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )


class SignalExitPending(SQLModel, table=True):
    """An EXIT that found no open position of its symbol to close.

    Pub/sub handling may reorder: after a leadership handover the old
    leader's ENTRY write can land after the new leader handled a later EXIT.
    The EXIT waits here, and the ENTRY it belongs to (emitted at or before
    it) is recorded already closed by it.
    """

    __tablename__ = "signal_exit_pending"
    __table_args__ = (
        Index("uq_signal_exit_pending", "network", "symbol", "emitted_at", unique=True),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    network: str
    symbol: str
    emitted_at: datetime = Field(sa_type=DateTime(timezone=True))  # type: ignore
    exit_reason: str | None = None
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )


# --- Strategy presets and option lists (docs/STRATEGY_PRESETS_PLAN.md §4.3.1) ---
#
# Table names are explicit: the implicit ones (cf. ``signalposition``) would not
# match the migration's. ``params``/``options``/``errors`` are JSONB, times are
# timestamptz. Service columns (id, is_builtin, times) also have server
# defaults, so the sync's raw SQL may leave them out. The rows are written by the preset API (PRESET-06) and the
# built-in sync (PRESET-05b); this module only declares them.


class StrategyPreset(SQLModel, table=True):
    """A named full snapshot of a strategy's non-const parameters (§4.3.3)."""

    __tablename__ = "strategy_preset"

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        primary_key=True,
        sa_column_kwargs={"server_default": text("gen_random_uuid()")},
    )
    strategy: str = Field(max_length=64)
    # Stored trimmed; unique per strategy ignoring case (ux_strategy_preset_name).
    name: str = Field(max_length=64)
    params: dict[str, Any] = Field(sa_type=JSONB)
    is_builtin: bool = Field(
        default=False, sa_column_kwargs={"server_default": false()}
    )
    # A built-in preset that disappeared from the catalog; hidden, id kept.
    retired_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
    # Deleting the author keeps the preset (ON DELETE SET NULL).
    created_by: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", ondelete="SET NULL"
    )
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
        sa_column_kwargs={"server_default": func.now()},
    )
    updated_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
        sa_column_kwargs={"server_default": func.now()},
    )


Index(
    "ux_strategy_preset_name",
    col(StrategyPreset.strategy),
    func.lower(col(StrategyPreset.name)),
    unique=True,
)


class StrategyFieldOptions(SQLModel, table=True):
    """The ordered option list of one select field (T2), edited in the UI."""

    __tablename__ = "strategy_field_options"

    strategy: str = Field(max_length=64, primary_key=True)
    field: str = Field(max_length=64, primary_key=True)
    # Binance codes in display order ("1w", "1d", ..., "1m").
    options: list[str] = Field(sa_type=JSONB)
    updated_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
        sa_column_kwargs={"server_default": func.now()},
    )


class StrategyBuiltinSync(SQLModel, table=True):
    """Lock row and state of the built-in preset sync of one strategy (§4.3.2).

    No publication timestamp and no sentinel values (R27/R28): the row exists
    to be locked ``FOR UPDATE``; ``content_hash`` is the idempotency key of the
    last applied parse.
    """

    __tablename__ = "strategy_builtin_sync"
    __table_args__ = (
        CheckConstraint(
            "status IN ('never', 'ok', 'partial')",
            name="ck_strategy_builtin_sync_status",
        ),
    )

    strategy: str = Field(max_length=64, primary_key=True)
    content_hash: str = Field(
        default="", max_length=64, sa_column_kwargs={"server_default": ""}
    )
    status: str = Field(
        default="never", max_length=16, sa_column_kwargs={"server_default": "never"}
    )
    errors: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_type=JSONB,
        sa_column_kwargs={"server_default": text("'[]'::jsonb")},
    )
    # Null only while status = never.
    synced_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore
    )


# Generic message
class Message(SQLModel):
    message: str


# JSON payload containing access token
class Token(SQLModel):
    access_token: str
    token_type: str = "bearer"


# Contents of JWT token
class TokenPayload(SQLModel):
    sub: str | None = None


class NewPassword(SQLModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)
