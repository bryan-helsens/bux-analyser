"""SQLite schema. Personal data (transactions) and public cache (prices, fx) live in the
same local file for simplicity; the cache tables are fully rebuildable."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text,
                        UniqueConstraint, create_engine, event)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import DB_PATH


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


class Security(Base):
    __tablename__ = "security"
    isin: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str | None] = mapped_column(String)
    currency: Mapped[str | None] = mapped_column(String(3))   # trading currency per broker
    ticker: Mapped[str | None] = mapped_column(String)         # resolved provider symbol
    ticker_provider: Mapped[str | None] = mapped_column(String)
    ticker_manual: Mapped[bool] = mapped_column(Boolean, default=False)  # user override wins
    exchange: Mapped[str | None] = mapped_column(String)
    asset_type: Mapped[str | None] = mapped_column(String)     # stock / etf / unknown
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
    fx_rate: Mapped[float] = mapped_column(Numeric(20, 10))
    quantity: Mapped[float] = mapped_column(Numeric(20, 8), default=0)
    price: Mapped[float] = mapped_column(Numeric(20, 8), default=0)
    fee: Mapped[float] = mapped_column(Numeric(20, 8), default=0)
    tax: Mapped[float] = mapped_column(Numeric(20, 8), default=0)
    amount: Mapped[float | None] = mapped_column(Numeric(20, 8))
    note: Mapped[str] = mapped_column(Text, default="")
    raw: Mapped[str] = mapped_column(Text)                      # original row as JSON, always kept


class PriceEod(Base):
    __tablename__ = "price_eod"
    __table_args__ = (UniqueConstraint("isin", "date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    isin: Mapped[str] = mapped_column(String, index=True)
    date: Mapped[date] = mapped_column(Date)
    close: Mapped[float] = mapped_column(Numeric(20, 8))
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
    rate: Mapped[float] = mapped_column(Numeric(20, 10))   # base per 1 unit of currency
    provider: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime)


class QuoteCache(Base):
    __tablename__ = "quote_cache"
    isin: Mapped[str] = mapped_column(String, primary_key=True)
    price: Mapped[float] = mapped_column(Numeric(20, 8))
    previous_close: Mapped[float | None] = mapped_column(Numeric(20, 8))
    currency: Mapped[str] = mapped_column(String(3))
    provider: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime)
    note: Mapped[str] = mapped_column(String, default="")


def make_engine(path=None):
    p = str(path or DB_PATH)
    eng = create_engine(f"sqlite:///{p}", future=True)

    @event.listens_for(eng, "connect")
    def _pragmas(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA journal_mode=WAL")
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(eng)
    return eng


def session_factory(engine=None) -> sessionmaker[Session]:
    return sessionmaker(bind=engine or make_engine(), expire_on_commit=False, future=True)
