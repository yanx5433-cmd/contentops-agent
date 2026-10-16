"""CSV 导入服务：创作者后台/橱窗导出 -> 事实表快照。

原则：
- 只接受 UTF-8（含 BOM）CSV，先全量校验、有错整批拒绝，不做半批导入；
- external_id 已存在则跳过（事实快照只增不改，历史数据靠批次字段追溯）；
- 每次导入写审计日志，含成功/跳过/拒绝计数。
"""
from __future__ import annotations

import csv
import io
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, ProductFact, SalesFact, VideoFact

VIDEO_REQUIRED = {"external_id", "title", "views", "finish_rate"}
PRODUCT_REQUIRED = {"external_id", "title", "category", "price", "commission_rate"}
SALES_REQUIRED = {"video_external_id", "product_external_id", "sales_count"}


class ImportRejected(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors[:5]))


def _read_rows(content: bytes, required: set[str]) -> list[dict]:
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise ImportRejected(["CSV 为空"])
    missing = required - set(rows[0].keys())
    if missing:
        raise ImportRejected([f"缺少必需列: {', '.join(sorted(missing))}"])
    return rows


def _to_int(v, default=0) -> int:
    try:
        return int(float(str(v).strip()))
    except (ValueError, TypeError):
        return default


def _to_float(v, default=0.0) -> float:
    try:
        return float(str(v).strip())
    except (ValueError, TypeError):
        return default


def _to_dt(v) -> datetime | None:
    v = (v or "").strip()
    if not v:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None


def import_videos(db: Session, content: bytes, batch: str) -> dict:
    rows = _read_rows(content, VIDEO_REQUIRED)
    errors: list[str] = []
    for i, row in enumerate(rows, start=2):
        if not (row.get("external_id") or "").strip():
            errors.append(f"第{i}行: external_id 为空")
        if _to_float(row.get("finish_rate"), -1) < 0 or _to_float(row.get("finish_rate"), 2) > 1:
            errors.append(f"第{i}行: finish_rate 应在 0-1 之间")
    if errors:
        raise ImportRejected(errors)

    existing = {x for (x,) in db.execute(select(VideoFact.external_id))}
    created, skipped, duplicate = 0, 0, 0
    seen_this_batch: set[str] = set()
    for row in rows:
        ext = row["external_id"].strip()
        if ext in existing or ext in seen_this_batch:
            # 本批首次出现且库中已有 -> skipped；本批第二次出现 -> duplicate
            if ext in seen_this_batch:
                duplicate += 1
            else:
                skipped += 1
            continue
        seen_this_batch.add(ext)
        db.add(VideoFact(
            external_id=ext,
            title=row.get("title", ""),
            published_at=_to_dt(row.get("published_at")),
            duration_sec=_to_int(row.get("duration_sec")),
            views=_to_int(row.get("views")),
            finish_rate=_to_float(row.get("finish_rate")),
            likes=_to_int(row.get("likes")),
            comments=_to_int(row.get("comments")),
            shares=_to_int(row.get("shares")),
            new_followers=_to_int(row.get("new_followers")),
            imported_batch=batch,
        ))
        existing.add(ext)
        seen_this_batch.add(ext)
        created += 1

    db.add(AuditLog(action="import", entity_type="video_fact",
                    detail={"batch": batch, "created": created, "skipped": skipped, "duplicate": duplicate}))
    db.flush()
    return {"created": created, "skipped": skipped, "duplicate": duplicate, "batch": batch}


def import_products(db: Session, content: bytes, batch: str) -> dict:
    rows = _read_rows(content, PRODUCT_REQUIRED)
    errors: list[str] = []
    for i, row in enumerate(rows, start=2):
        if _to_float(row.get("price"), -1) < 0:
            errors.append(f"第{i}行: price 应为非负数")
        fr = _to_float(row.get("commission_rate"), -1)
        if fr < 0 or fr > 1:
            errors.append(f"第{i}行: commission_rate 应在 0-1 之间")
    if errors:
        raise ImportRejected(errors)

    existing = {x for (x,) in db.execute(select(ProductFact.external_id))}
    created, skipped = 0, 0
    for row in rows:
        ext = row["external_id"].strip()
        if ext in existing:
            skipped += 1
            continue
        db.add(ProductFact(
            external_id=ext,
            title=row.get("title", ""),
            category=row.get("category", ""),
            price=_to_float(row.get("price")),
            commission_rate=_to_float(row.get("commission_rate")),
            sales_count=_to_int(row.get("sales_count")),
            return_rate=_to_float(row.get("return_rate")),
            required_licenses=[x.strip() for x in (row.get("required_licenses") or "").split("|") if x.strip()],
            imported_batch=batch,
        ))
        existing.add(ext)
        created += 1

    db.add(AuditLog(action="import", entity_type="product_fact",
                    detail={"batch": batch, "created": created, "skipped": skipped}))
    db.flush()
    return {"created": created, "skipped": skipped, "batch": batch}


def import_sales(db: Session, content: bytes, batch: str) -> dict:
    """销售回流导入：视频×商品×日期维度，幂等键为三元组。

    允许引用尚未导入的视频/商品（只列出 unmatched 提示，不整批拒绝），
    因为销售数据可能先于全量数据到达。
    """
    rows = _read_rows(content, SALES_REQUIRED)
    errors: list[str] = []
    for i, row in enumerate(rows, start=2):
        if not (row.get("video_external_id") or "").strip():
            errors.append(f"第{i}行: video_external_id 为空")
        if not (row.get("product_external_id") or "").strip():
            errors.append(f"第{i}行: product_external_id 为空")
        sc = _to_int(row.get("sales_count"), -1)
        rc = _to_int(row.get("return_count"), 0)
        if sc < 0:
            errors.append(f"第{i}行: sales_count 应为非负整数")
        if rc < 0 or rc > max(sc, 0):
            errors.append(f"第{i}行: return_count 不能为负且不能大于 sales_count")
        if (row.get("stat_date") or "").strip() and _to_dt(row.get("stat_date")) is None:
            errors.append(f"第{i}行: stat_date 格式应为 YYYY-MM-DD")
    if errors:
        raise ImportRejected(errors)

    existing = {
        (v, p, d) for (v, p, d) in db.execute(
            select(SalesFact.video_external_id, SalesFact.product_external_id, SalesFact.stat_date))
    }
    created, skipped, duplicate = 0, 0, 0
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        key = (row["video_external_id"].strip(), row["product_external_id"].strip(),
               (row.get("stat_date") or "").strip())
        if key in existing or key in seen:
            if key in seen:
                duplicate += 1
            else:
                skipped += 1
            continue
        seen.add(key)
        db.add(SalesFact(
            video_external_id=key[0], product_external_id=key[1], stat_date=key[2],
            sales_count=_to_int(row.get("sales_count")),
            gmv=_to_float(row.get("gmv")),
            return_count=_to_int(row.get("return_count")),
            return_amount=_to_float(row.get("return_amount")),
            imported_batch=batch,
        ))
        existing.add(key)
        created += 1

    video_ids = {x for (x,) in db.execute(select(VideoFact.external_id))}
    product_ids = {x for (x,) in db.execute(select(ProductFact.external_id))}
    unmatched = sorted(
        {k[0] for k in seen if k[0] not in video_ids} | {k[1] for k in seen if k[1] not in product_ids}
    )

    db.add(AuditLog(action="import", entity_type="sales_fact",
                    detail={"batch": batch, "created": created, "skipped": skipped,
                            "duplicate": duplicate, "unmatched": unmatched}))
    db.flush()
    return {"created": created, "skipped": skipped, "duplicate": duplicate,
            "unmatched": unmatched, "batch": batch}
