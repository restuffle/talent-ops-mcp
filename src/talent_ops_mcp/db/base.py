"""Declarative base and the two column types every table depends on.

Two invariants are enforced here rather than in each model, because "enforced in every caller"
is the same as "not enforced":

* **Datetimes are timezone-aware UTC.** :class:`UtcDateTime` refuses to persist a naive one.
* **Status columns hold a known value.** :func:`enum_column` emits a ``CHECK`` constraint.
"""

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import TypeVar

from sqlalchemy import CheckConstraint, DateTime, Enum, MetaData, String
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator

_EnumT = TypeVar("_EnumT", bound=StrEnum)

NAIVE_DATETIME_MESSAGE = (
    "naive datetime rejected: this column stores timezone-aware UTC. "
    "Attach a timezone (datetime.now(UTC), or zoneinfo for local wall-clock input) before saving."
)


class UtcDateTime(TypeDecorator[datetime]):
    """A datetime column that is timezone-aware UTC on both sides of the wire.

    SQLite has no timezone-aware type: the driver stores whatever naive value it is handed and
    returns it just as bare, so a value written as ``17:00-05:00`` reads back as ``17:00`` with
    no record that it was Eastern. That silent drop is the bug this type exists to prevent.

    Writes convert to UTC and strip ``tzinfo`` (the storage format), reads reattach
    :data:`datetime.UTC`. A naive write raises instead of guessing a zone -- guessing is how a
    candidate ends up with a 09:00 invitation in the wrong hemisphere. Because the check runs in
    a bind processor, SQLAlchemy re-raises it wrapped in
    :class:`sqlalchemy.exc.StatementError`; unwrap ``.orig`` to see the message above.

    The conversion is deliberately not dialect-conditional. Postgres would accept the aware
    value directly, but a type that behaves differently per backend means tests on SQLite stop
    proving anything about production.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(NAIVE_DATETIME_MESSAGE)
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


def enum_column(enum_cls: type[_EnumT]) -> Enum:
    """A ``VARCHAR`` column constrained to ``enum_cls``'s values.

    ``native_enum=False`` keeps the storage a plain string, which is what SQLite has anyway, and
    ``create_constraint=True`` is what actually emits the ``CHECK`` -- SQLAlchemy 2.0 defaults it
    to ``False``, so omitting it yields a column that documents a constraint without having one.

    ``values_callable`` stores each member's *value* (``"on_hold"``); the default stores its
    *name* (``"ON_HOLD"``), which would make hand-written SQL and seed literals disagree with
    the enum.
    """
    return Enum(
        enum_cls,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        length=40,
        values_callable=lambda members: [str(member.value) for member in members],
    )


def new_id(prefix: str) -> str:
    """A unique, readable primary key such as ``interview_9f2c1a04b7de``.

    Prefixed so an id is self-describing in a log line or an error message, and short enough to
    stay readable. Runtime rows only -- the seed uses fixed literal ids so its output is
    byte-identical on every run.
    """
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def positive_duration(start_column: str, end_column: str) -> CheckConstraint:
    """``end > start``, the §9.3 invariant, as a table constraint.

    Applied to every interval-bearing table so a zero-length or inverted window cannot exist to
    be reasoned about later.
    """
    return CheckConstraint(f"{end_column} > {start_column}", name=f"ck_{end_column}_after_{start_column}")


# Without this, SQLAlchemy names an enum's CHECK after the enum class -- two unrelated tables
# both get `partytype`, and a failure reports `CHECK constraint failed: partytype` without
# saying where. Explicit names in `__table_args__` still win over the convention.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base with the project's type mappings.

    ``type_annotation_map`` is the enforcement point for UTC: a model author writes
    ``Mapped[datetime]`` and gets :class:`UtcDateTime` without opting in, so the safe path is
    also the default one. An author who wants naive storage has to reach for an explicit
    ``DateTime``, which is visible in review.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy reads this as a plain class attribute
        datetime: UtcDateTime,
        str: String(255),
    }
