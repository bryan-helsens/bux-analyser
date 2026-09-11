"""Persist a parsed BUX export: idempotent on source rows, raw rows always kept."""
from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import RAW_DIR
from ..core.types import Txn, TxnType
from ..db import ImportBatch, ImportedRow, Security, Transaction
from .bux_csv import ParseResult, parse_bux_csv


@dataclass
class ImportSummary:
    batch_id: int | None
    filename: str
    rows: int
    new_rows: int
    new_txns: int
    already_imported: bool
    warnings: list[str]
    cash_ok: bool
    cash_messages: list[str]


def import_bux_file(session: Session, path: str | Path, archive: bool = True) -> ImportSummary:
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    sha = hashlib.sha256(text.encode()).hexdigest()
    existing = session.execute(select(ImportBatch).where(ImportBatch.file_sha256 == sha)).scalar()
    res = parse_bux_csv(text)
    if existing is not None:
        return ImportSummary(existing.id, path.name, res.row_count, 0, 0, True, res.warnings,
                             res.cash_check.ok, res.cash_check.mismatches)
    if archive:
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, RAW_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{path.name}")
    summary = persist(session, res, path.name, sha)
    return summary


def persist(session: Session, res: ParseResult, filename: str, sha: str) -> ImportSummary:
    seen = {r for (r,) in session.execute(select(ImportedRow.rid)).all()}
    batch = ImportBatch(filename=filename, file_sha256=sha, imported_at=datetime.now(timezone.utc),
                        row_count=res.row_count, new_rows=0, warnings="\n".join(res.warnings))
    session.add(batch)
    session.flush()

    for isin, meta in res.securities.items():
        sec = session.get(Security, isin)
        if sec is None:
            session.add(Security(isin=isin, name=meta["name"], currency=meta["currency"], asset_type=meta["asset_type"]))
        else:
            sec.name = sec.name or meta["name"]
            sec.currency = sec.currency or meta["currency"]

    txn_by_id = {t.id: t for t in res.txns}
    new_txns = 0
    new_rows = 0
    for rid in res.row_ids:
        if rid in seen:
            continue
        new_rows += 1
        if rid in txn_by_id:
            t = txn_by_id[rid]
            session.add(_to_row(t, batch.id))
            session.add(ImportedRow(rid=rid, batch_id=batch.id, txn_id=t.id, status="imported"))
            new_txns += 1
        elif rid in res.merged_rows:
            session.add(ImportedRow(rid=rid, batch_id=batch.id, txn_id=res.merged_rows[rid], status="merged"))
        else:
            session.add(ImportedRow(rid=rid, batch_id=batch.id, txn_id=None, status="skipped"))
    batch.new_rows = new_rows
    session.commit()
    return ImportSummary(batch.id, filename, res.row_count, new_rows, new_txns, False, res.warnings,
                         res.cash_check.ok, res.cash_check.mismatches)


def _to_row(t: Txn, batch_id: int) -> Transaction:
    return Transaction(id=t.id, batch_id=batch_id, type=t.type.value, date=t.date, isin=t.isin, name=t.name,
                       currency=t.currency, fx_rate=t.fx_rate, quantity=t.quantity, price=t.price, fee=t.fee,
                       tax=t.tax, amount=t.amount, note=t.note,
                       raw=json.dumps({"broker_pl": str(t.broker_pl) if t.broker_pl is not None else None}))


def load_txns(session: Session) -> list[Txn]:
    out = []
    for r in session.execute(select(Transaction)).scalars():
        meta = json.loads(r.raw or "{}")
        bp = meta.get("broker_pl")
        out.append(Txn(id=r.id, type=TxnType(r.type), date=r.date, currency=r.currency, fx_rate=Decimal(str(r.fx_rate)),
                       isin=r.isin, name=r.name, quantity=Decimal(str(r.quantity)), price=Decimal(str(r.price)),
                       fee=Decimal(str(r.fee)), tax=Decimal(str(r.tax)),
                       amount=Decimal(str(r.amount)) if r.amount is not None else None, note=r.note or "",
                       broker_pl=Decimal(bp) if bp is not None else None))
    return out
