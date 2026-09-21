"""SQLite schema. Personal data (transactions) and public cache (prices, fx) live in the
same local file for simplicity; the cache tables are fully rebuildable."""
from __future__ import annotations

from datetime import date, datetime

from decimal import Decimal

from sqlalchemy import (Boolean, Date, DateTime, ForeignKey, Integer, String, Text, TypeDecorator,
                        UniqueConstraint, create_engine, event)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import DB_PATH


class Money(TypeDecorator):
    """Exact decimals stored as text. SQLite has no decimal type and SQLAlchemy's Numeric
    would silently go through float; personal accounting must round-trip exactly."""
    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else str(Decimal(value))

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


class Base(DeclarativeBase):
    pass


class ImportBatch(Base):
    __tablename__ = "import_batch"
    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String)
    file_sha256: Mapped[str] = mapped_column(String, unique=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime)
    row_count: Mapped[int] = mapped_column(Integer)
    new_rows: Mapped[int] = mapped_column(Integer)
    warnings: Mapped[str] = mapped_column(Text, default="")


class ImportedRow(Base):
    """Every source row ever seen, keyed by its hash → idempotent re-imports, even for
    rows that were merged into another transaction or deliberately skipped."""
    __tablename__ = "imported_row"
    rid: Mapped[str] = mapped_column(String, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batch.id"))
    txn_id: Mapped[str | None] = mapped_column(String)   # transaction it produced or was merged into
    status: Mapped[str] = mapped_column(String)          # imported | merged | skipped


class Security(Base):
    __tablename__ = "security"
    isin: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str | None] = mapped_column(String)
    currency: Mapped[str | None] = mapped_column(String(3))   # trading currency per broker
    ticker: Mapped[str | None] = mapped_column(String)         # resolved provider symbol
    ticker_provider: Mapped[str | None] = mapped_column(String)
    ticker_manual: Mapped[bool] = mapped_column(Boolean, default=False)  # user override wins
    exchange: Mapped[str | None] = mapped_column(String)
    asset_type: Mapped[str | None] = mapped_column(String)     # stock / etf / crypto / benchmark
    sector: Mapped[str | None] = mapped_column(String)
    industry: Mapped[str | None] = mapped_column(String)
    country: Mapped[str | None] = mapped_column(String)
    market_cap: Mapped[Decimal | None] = mapped_column(Money)
    meta_provider: Mapped[str | None] = mapped_column(String)
    meta_retrieved_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)


class Transaction(Base):
    __tablename__ = "transaction"
    id: Mapped[str] = mapped_column(String, primary_key=True)  # sha1 of source row → idempotent
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batch.id"))
    type: Mapped[str] = mapped_column(String)
    date: Mapped[date] = mapped_column(Date)
    isin: Mapped[str | None] = mapped_column(ForeignKey("security.isin"))
    name: Mapped[str | None] = mapped_column(String)
    currency: Mapped[str] = mapped_column(String(3))
    fx_rate: Mapped[Decimal] = mapped_column(Money)
    quantity: Mapped[Decimal] = mapped_column(Money, default=0)
    price: Mapped[Decimal] = mapped_column(Money, default=0)
    fee: Mapped[Decimal] = mapped_column(Money, default=0)
    tax: Mapped[Decimal] = mapped_column(Money, default=0)
    amount: Mapped[Decimal | None] = mapped_column(Money)
    note: Mapped[str] = mapped_column(Text, default="")
    raw: Mapped[str] = mapped_column(Text)                      # original row as JSON, always kept


class PriceEod(Base):
    __tablename__ = "price_eod"
    __table_args__ = (UniqueConstraint("isin", "date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    isin: Mapped[str] = mapped_column(String, index=True)
    date: Mapped[date] = mapped_column(Date)
    close: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3))
    provider: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime)


class FxRate(Base):
    __tablename__ = "fx_rate"
    __table_args__ = (UniqueConstraint("currency", "base", "date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    currency: Mapped[str] = mapped_column(String(3), index=True)
    base: Mapped[str] = mapped_column(String(3))
    date: Mapped[date] = mapped_column(Date)
    rate: Mapped[Decimal] = mapped_column(Money)   # base per 1 unit of currency
    provider: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime)


class QuoteCache(Base):
    __tablename__ = "quote_cache"
    isin: Mapped[str] = mapped_column(String, primary_key=True)
    price: Mapped[Decimal] = mapped_column(Money)
    previous_close: Mapped[Decimal | None] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3))
    provider: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime)
    note: Mapped[str] = mapped_column(String, default="")


class FundamentalsCache(Base):
    """A provider's financial statements, stored whole.

    Kept as the adapter's own JSON rather than shredded into columns: sources disagree
    about what a period is, and re-parsing from the original beats migrating a schema
    every time one of them changes.
    """
    __tablename__ = "fundamentals_cache"
    __table_args__ = (UniqueConstraint("isin", "provider"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    isin: Mapped[str] = mapped_column(String, index=True)
    provider: Mapped[str] = mapped_column(String)
    payload: Mapped[str] = mapped_column(Text)
    currency: Mapped[str | None] = mapped_column(String)
    quality: Mapped[str] = mapped_column(String, default="reported")
    latest_report: Mapped[date | None] = mapped_column(Date)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime)


class EtfHolding(Base):
    """One constituent of an ETF, as published by the issuer on a given date."""
    __tablename__ = "etf_holding"
    __table_args__ = (UniqueConstraint("etf_isin", "as_of", "constituent"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    etf_isin: Mapped[str] = mapped_column(String, index=True)
    as_of: Mapped[date] = mapped_column(Date)
    constituent: Mapped[str] = mapped_column(String)          # ISIN where published, else ticker
    name: Mapped[str | None] = mapped_column(String)
    weight: Mapped[Decimal] = mapped_column(Money)            # fraction of the fund
    sector: Mapped[str | None] = mapped_column(String)
    country: Mapped[str | None] = mapped_column(String)
    currency: Mapped[str | None] = mapped_column(String)
    asset_class: Mapped[str | None] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime)


class AlertRule(Base):
    """A threshold the user chose. Rules are data, not code, so they can be edited
    in the dashboard without touching the engine."""
    __tablename__ = "alert_rule"
    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String)
    scope: Mapped[str] = mapped_column(String)              # security | portfolio
    metric: Mapped[str] = mapped_column(String)
    operator: Mapped[str] = mapped_column(String)           # gt | lt | gte | lte
    threshold: Mapped[Decimal] = mapped_column(Money)
    isin: Mapped[str | None] = mapped_column(String)        # None = every holding
    group: Mapped[str | None] = mapped_column(String)       # e.g. a sector name
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    cooldown_days: Mapped[int] = mapped_column(Integer, default=7)
    category: Mapped[str] = mapped_column(String, default="price")


class AlertEvent(Base):
    __tablename__ = "alert_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("alert_rule.id"))
    fired_at: Mapped[datetime] = mapped_column(DateTime)
    as_of: Mapped[date] = mapped_column(Date)
    isin: Mapped[str | None] = mapped_column(String)
    subject: Mapped[str] = mapped_column(String)
    value: Mapped[Decimal] = mapped_column(Money)
    message: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String, default="price")
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)


class ScoreSnapshot(Base):
    """Stored so a score change can be explained by subtraction rather than guessed."""
    __tablename__ = "score_snapshot"
    __table_args__ = (UniqueConstraint("isin", "as_of"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    isin: Mapped[str] = mapped_column(String, index=True)
    as_of: Mapped[date] = mapped_column(Date)
    overall: Mapped[Decimal | None] = mapped_column(Money)
    pillars: Mapped[str] = mapped_column(Text)              # JSON: pillar key -> score
    model_version: Mapped[str] = mapped_column(String, default="1")


def make_engine(path=None):
    p = str(path or DB_PATH)
    eng = create_engine(f"sqlite:///{p}", future=True)

    @event.listens_for(eng, "connect")
    def _pragmas(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA journal_mode=WAL")
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(eng)
    _add_missing_columns(eng)
    return eng


def _add_missing_columns(engine) -> None:
    """Add columns introduced after a database was created.

    The personal database holds imported transactions that would be tedious to rebuild,
    so new nullable columns are added in place rather than requiring a fresh import.
    Anything more involved than adding a column needs a real migration.
    """
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have or not col.nullable:
                    continue
                ddl = col.type.compile(engine.dialect)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl}'))


def session_factory(engine=None) -> sessionmaker[Session]:
    return sessionmaker(bind=engine or make_engine(), expire_on_commit=False, future=True)
