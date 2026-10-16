"""路由：导入、选题、评分、合规、审批、审计。"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.database import get_db
from app.models import (
    AccountProfile,
    AppSetting,
    Approval,
    AuditLog,
    CommentDraft,
    ComplianceHit,
    ProductFact,
    Topic,
    TopicScore,
    VideoFact,
)
from app.schemas import (
    ApprovalIn,
    AuditOut,
    BrainstormIn,
    CheckTextIn,
    CommentDraftIn,
    CommentDraftOut,
    CommentFinalizeIn,
    ComplianceHitOut,
    ImportOut,
    KBQueryIn,
    LLMSettingsIn,
    PublishIn,
    ReviewOut,
    ScoreOut,
    TopicIn,
)
from app.services import (
    analytics,
    brainstorm,
    comment_assistant,
    compliance,
    importer,
    llm,
    rag,
    scoring,
)

router = APIRouter(prefix="/api")


# ---------- 数据导入 ----------

@router.post("/import/videos", response_model=ImportOut)
def upload_videos(file: UploadFile = File(...), db: Session = Depends(get_db)):
    content = file.file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(413, "文件超过 5MB 限制")
    batch = f"video-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    try:
        result = importer.import_videos(db, content, batch)
    except importer.ImportRejected as e:
        raise HTTPException(422, detail={"errors": e.errors})
    db.commit()
    return result


@router.post("/import/products", response_model=ImportOut)
def upload_products(file: UploadFile = File(...), db: Session = Depends(get_db)):
    content = file.file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(413, "文件超过 5MB 限制")
    batch = f"product-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    try:
        result = importer.import_products(db, content, batch)
    except importer.ImportRejected as e:
        raise HTTPException(422, detail={"errors": e.errors})
    db.commit()
    return result


@router.post("/import/sales")
def upload_sales(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """销售回流导入：视频×商品×日期，幂等键三元组，供复盘与校准使用。"""
    content = file.file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(413, "文件超过 5MB 限制")
    batch = f"sales-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    try:
        result = importer.import_sales(db, content, batch)
    except importer.ImportRejected as e:
        raise HTTPException(422, detail={"errors": e.errors})
    db.commit()
    return result


# ---------- 选题 ----------

def _get_topic(db: Session, topic_id: int) -> Topic:
    topic = db.get(Topic, topic_id)
    if topic is None:
        raise HTTPException(404, "选题不存在")
    return topic


def _load_context(db: Session, topic: Topic) -> tuple[AccountProfile | None, list[VideoFact]]:
    profile = db.execute(select(AccountProfile)).scalars().first()
    since = datetime.now() - timedelta(days=90)
    videos = list(db.execute(
        select(VideoFact).where(VideoFact.published_at >= since).order_by(VideoFact.published_at.desc())
    ).scalars())
    if not videos:  # 没有带时间的数据时退回最近全部
        videos = list(db.execute(select(VideoFact)).scalars())
    return profile, videos


@router.post("/topics", status_code=201)
def create_topic(payload: TopicIn, db: Session = Depends(get_db)):
    product_id = None
    if payload.product_external_id:
        product = db.execute(
            select(ProductFact).where(ProductFact.external_id == payload.product_external_id)
        ).scalars().first()
        if product is None:
            raise HTTPException(404, f"商品 {payload.product_external_id} 不存在，请先导入")
        product_id = product.id
    topic = Topic(
        title=payload.title, outline=payload.outline, category=payload.category,
        estimated_duration_sec=payload.estimated_duration_sec,
        hook_strength=payload.hook_strength, product_id=product_id,
    )
    db.add(topic)
    db.commit()
    db.refresh(topic)
    return {"id": topic.id, "status": topic.status}


@router.post("/topics/{topic_id}/score", response_model=ScoreOut)
def score_topic_api(topic_id: int, publish_hour: int | None = None, db: Session = Depends(get_db)):
    topic = _get_topic(db, topic_id)
    profile, videos = _load_context(db, topic)
    result = scoring.score_topic(topic, profile, videos, publish_hour)
    prev = db.execute(
        select(TopicScore).where(TopicScore.topic_id == topic.id).order_by(TopicScore.version.desc())
    ).scalars().first()
    version = (prev.version + 1) if prev else 1

    db.add(TopicScore(
        topic_id=topic.id, version=version,
        content_score=result.content_score,
        content_detail=[{"name": d.name, "score": d.score, "reason": d.reason} for d in result.content_detail],
        product_score=result.product_score,
        product_detail=[{"name": d.name, "score": d.score, "reason": d.reason} for d in result.product_detail] if result.product_detail else None,
        total_score=result.total_score,
        weaknesses=result.weaknesses,
    ))
    db.add(AuditLog(action="score", entity_type="topic", entity_id=str(topic.id),
                    detail={"version": version, "total": result.total_score}))
    if topic.status == "draft":
        topic.status = "scored"
    db.commit()
    return ScoreOut(
        topic_id=topic.id, version=version,
        content_score=result.content_score, product_score=result.product_score,
        total_score=result.total_score, weaknesses=result.weaknesses,
        detail={"content": [d.__dict__ for d in result.content_detail],
                "product": [d.__dict__ for d in (result.product_detail or [])]},
    )


@router.post("/topics/{topic_id}/review", response_model=ReviewOut)
def review_topic_api(topic_id: int, db: Session = Depends(get_db)):
    topic = _get_topic(db, topic_id)
    result = compliance.review_topic(db, topic)
    db.add(AuditLog(action="compliance_review", entity_type="topic", entity_id=str(topic.id),
                    detail={"blocked": result.blocked, "levels": sorted(result.levels),
                            "hits": len(result.hits)}))
    if not result.blocked and topic.status == "scored":
        topic.status = "reviewed"  # P1 命中也允许进审批，由人终审
    db.commit()
    return ReviewOut(
        topic_id=topic.id, blocked=result.blocked,
        need_manual_review=result.need_manual_review,
        hits=[ComplianceHitOut(level=h.level, kind=h.kind, matched=h.matched, action=h.action) for h in result.hits],
        topic_status=topic.status,
    )


@router.post("/topics/{topic_id}/approve")
def approve_topic_api(topic_id: int, payload: ApprovalIn, db: Session = Depends(get_db)):
    if not config.ENABLE_APPROVAL_API:
        raise HTTPException(403, "审批接口已被配置关闭")
    topic = _get_topic(db, topic_id)
    if topic.status not in ("scored", "reviewed"):
        raise HTTPException(409, f"当前状态 {topic.status} 不可审批（需先评分）")
    if topic.status == "rejected":
        raise HTTPException(409, "选题已被合规拦截，不可审批通过")
    topic.status = payload.decision
    db.add(Approval(topic_id=topic.id, decision=payload.decision,
                    reviewer=payload.reviewer, note=payload.note))
    db.add(AuditLog(action=payload.decision, entity_type="topic", entity_id=str(topic.id),
                    detail={"reviewer": payload.reviewer, "note": payload.note}))
    db.commit()
    return {"topic_id": topic.id, "status": topic.status}


@router.post("/topics/{topic_id}/publish")
def publish_topic_api(topic_id: int, payload: PublishIn, db: Session = Depends(get_db)):
    """发布挂接：把审批通过的选题关联到真实视频，供预测校准使用。"""
    topic = _get_topic(db, topic_id)
    if topic.status not in ("approved", "published"):
        raise HTTPException(409, f"当前状态 {topic.status} 不可标记发布（需先审批通过）")
    if topic.published_video_external_id == payload.video_external_id:
        raise HTTPException(409, "该选题已挂接此视频")
    video = db.execute(
        select(VideoFact).where(VideoFact.external_id == payload.video_external_id)
    ).scalars().first()
    if video is None:
        raise HTTPException(404, f"视频 {payload.video_external_id} 不存在，请先导入视频数据")
    topic.published_video_external_id = payload.video_external_id
    topic.status = "published"
    db.add(AuditLog(action="publish", entity_type="topic", entity_id=str(topic.id),
                    detail={"video_external_id": payload.video_external_id}))
    db.commit()
    return {"topic_id": topic.id, "status": topic.status,
            "video_external_id": topic.published_video_external_id}


@router.get("/topics/{topic_id}")
def get_topic_api(topic_id: int, db: Session = Depends(get_db)):
    topic = _get_topic(db, topic_id)
    latest = db.execute(
        select(TopicScore).where(TopicScore.topic_id == topic.id).order_by(TopicScore.version.desc())
    ).scalars().first()
    hits = db.execute(select(ComplianceHit).where(ComplianceHit.topic_id == topic.id)).scalars().all()
    return {
        "id": topic.id, "title": topic.title, "status": topic.status,
        "category": topic.category, "product_id": topic.product_id,
        "published_video_external_id": topic.published_video_external_id,
        "latest_score": latest and {
            "version": latest.version, "content_score": latest.content_score,
            "product_score": latest.product_score, "total_score": latest.total_score,
            "weaknesses": latest.weaknesses,
        },
        "compliance_hits": [
            {"level": h.level, "kind": h.kind, "matched": h.matched, "action": h.action} for h in hits
        ],
    }


@router.get("/audit", response_model=list[AuditOut])
def list_audit(limit: int = 50, db: Session = Depends(get_db)):
    logs = db.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 200))).scalars().all()
    return logs


# ---------- 工作台总览 ----------

@router.get("/overview")
def overview_api(db: Session = Depends(get_db)):
    from sqlalchemy import func
    videos = db.execute(select(func.count()).select_from(VideoFact)).scalar() or 0
    products = db.execute(select(func.count()).select_from(ProductFact)).scalar() or 0
    drafts = db.execute(select(func.count()).select_from(CommentDraft)).scalar() or 0
    status_counts: dict[str, int] = {}
    for status, n in db.execute(select(Topic.status, func.count()).group_by(Topic.status)):
        status_counts[status] = n
    # "已通过"口径 = approved + published（发布挂接后状态会从 approved 变为 published）
    status_counts["approved_total"] = status_counts.get("approved", 0) + status_counts.get("published", 0)
    return {"videos": videos, "products": products,
            "comment_drafts": drafts, "topics_by_status": status_counts}


@router.get("/topics")
def list_topics_api(limit: int = 50, db: Session = Depends(get_db)):
    topics = db.execute(select(Topic).order_by(Topic.id.desc()).limit(min(limit, 200))).scalars().all()
    out = []
    for t in topics:
        latest = db.execute(
            select(TopicScore).where(TopicScore.topic_id == t.id).order_by(TopicScore.version.desc())
        ).scalars().first()
        out.append({
            "id": t.id, "title": t.title, "category": t.category, "status": t.status,
            "product_id": t.product_id,
            "content_score": latest.content_score if latest else None,
            "product_score": latest.product_score if latest else None,
            "total_score": latest.total_score if latest else None,
            "created_at": t.created_at.isoformat() if t.created_at else "",
        })
    return out


# ---------- 爆款拆解知识库（RAG） ----------

@router.post("/kb/query")
def kb_query_api(payload: KBQueryIn):
    result = rag.query_kb(payload.query, top_k=payload.top_k)
    return result


@router.post("/kb/reload")
def kb_reload_api(db: Session = Depends(get_db)):
    chunks = rag.reload_index()
    db.add(AuditLog(action="kb_reload", entity_type="knowledge_base",
                    detail={"chunks": chunks}))
    db.commit()
    return {"chunks": chunks}


# ---------- 评论运营助手 ----------

@router.post("/comments/draft", response_model=CommentDraftOut, status_code=201)
def comment_draft_api(payload: CommentDraftIn, db: Session = Depends(get_db)):
    if not config.ENABLE_COMMENT_DRAFT_TOOL:
        raise HTTPException(403, "评论草稿工具已被配置关闭")
    result = comment_assistant.generate_draft(payload.comment_text)
    row = CommentDraft(
        video_external_id=payload.video_external_id,
        comment_text=payload.comment_text,
        intent=result.intent, confidence=result.confidence,
        draft=result.draft, source=result.source,
    )
    db.add(row)
    db.add(AuditLog(action="comment_draft", entity_type="comment",
                    entity_id=str(row.id) if row.id else "",
                    detail={"intent": result.intent, "confidence": result.confidence,
                            "source": result.source}))
    db.commit()
    db.refresh(row)
    return CommentDraftOut(
        id=row.id, intent=row.intent,
        intent_label=comment_assistant.INTENT_LABELS[row.intent],
        confidence=row.confidence, draft=row.draft, source=row.source,
        status=row.status, notes=result.notes,
    )


@router.get("/comments/drafts")
def list_comment_drafts(status: str | None = None, limit: int = 50, db: Session = Depends(get_db)):
    q = select(CommentDraft).order_by(CommentDraft.id.desc()).limit(min(limit, 200))
    if status:
        q = q.where(CommentDraft.status == status)
    drafts = db.execute(q).scalars().all()
    return [
        {"id": d.id, "video_external_id": d.video_external_id,
         "comment_text": d.comment_text, "intent": d.intent,
         "intent_label": comment_assistant.INTENT_LABELS.get(d.intent, d.intent),
         "draft": d.draft, "source": d.source, "status": d.status,
         "final_text": d.final_text, "created_at": d.created_at.isoformat()}
        for d in drafts
    ]


@router.post("/comments/drafts/{draft_id}/finalize")
def finalize_comment_api(draft_id: int, payload: CommentFinalizeIn, db: Session = Depends(get_db)):
    """人工定稿：可改写、可标记已发送、可废弃。发送动作由人在抖音端执行。"""
    draft = db.get(CommentDraft, draft_id)
    if draft is None:
        raise HTTPException(404, "草稿不存在")
    draft.final_text = payload.final_text
    draft.status = payload.status
    db.add(AuditLog(action="comment_finalize", entity_type="comment", entity_id=str(draft.id),
                    detail={"status": payload.status, "edited": payload.final_text != draft.draft}))
    db.commit()
    return {"id": draft.id, "status": draft.status, "edited": draft.final_text != draft.draft}


# ---------- 文案体检 ----------

@router.post("/compliance/check-text")
def check_text_api(payload: CheckTextIn):
    """任意文案的实时合规检查：返回全部命中位置 + 改写建议 + 总体结论。"""
    spans = compliance.check_text_spans(payload.text)
    counts = {"P0": 0, "P1": 0, "P2": 0}
    for s in spans:
        counts[s["level"]] += 1
    if counts["P0"]:
        verdict = "block"
    elif counts["P1"]:
        verdict = "manual_review"
    elif counts["P2"]:
        verdict = "rewrite_hint"
    else:
        verdict = "clean"
    return {"hits": spans, "counts": counts, "total": len(spans), "verdict": verdict}


# ---------- AI 选题风暴 ----------

@router.post("/brainstorm")
def brainstorm_api(payload: BrainstormIn, db: Session = Depends(get_db)):
    """生成候选选题 → 合规预筛 → 批量评分（只给候选，确认后手动加入选题库）。"""
    result = brainstorm.generate(db, payload.category, payload.keywords,
                                 payload.count, payload.publish_hour)
    db.add(AuditLog(action="brainstorm", entity_type="topic",
                    detail={"source": result["source"], "count": result["count"],
                            "category": result["category"]}))
    db.commit()
    return result


# ---------- 运营看板 / 复盘 / 校准 ----------

@router.get("/dashboard")
def dashboard_api(db: Session = Depends(get_db)):
    return analytics.dashboard(db)


@router.get("/review/conversion")
def review_conversion_api(days: int = 90, db: Session = Depends(get_db)):
    days = max(1, min(days, 365))
    return {"days": days, "rows": analytics.conversion_detail(db, days=days)}


@router.get("/review/report")
def review_report_api(days: int = 30, db: Session = Depends(get_db)):
    days = max(1, min(days, 365))
    return analytics.report(db, days=days)


@router.get("/review/calibration")
def review_calibration_api(db: Session = Depends(get_db)):
    return analytics.calibration(db)


# ---------- LLM 接入配置（问答/生成的可选增强） ----------

@router.get("/settings/llm")
def get_llm_settings_api():
    return llm.status()


@router.put("/settings/llm")
def put_llm_settings_api(payload: LLMSettingsIn, db: Session = Depends(get_db)):
    updates: dict[str, str] = {}
    if payload.api_key is not None:
        updates["llm_api_key"] = payload.api_key.strip()
    if payload.base_url is not None:
        updates["llm_base_url"] = payload.base_url.strip()
    if payload.model is not None:
        updates["llm_model"] = payload.model.strip()
    if payload.daily_cap is not None:
        updates["llm_daily_cap"] = str(payload.daily_cap)
    for key, value in updates.items():
        row = db.get(AppSetting, key)
        if row is None:
            db.add(AppSetting(key=key, value=value))
        else:
            row.value = value
    # 审计只记改了哪些键，不记录 Key 内容
    db.add(AuditLog(action="llm_config", entity_type="setting", detail={"keys": sorted(updates)}))
    db.commit()
    llm.configure(api_key=payload.api_key, base_url=payload.base_url,
                  model=payload.model, daily_cap=payload.daily_cap)
    return llm.status()


@router.post("/settings/llm/test")
def test_llm_settings_api():
    if not llm.status()["configured"]:
        raise HTTPException(400, "尚未配置 API Key")
    result = llm.chat("请回复：ok", system="你是一个连通性测试，只回复 ok 两个字符。")
    if result is None:
        return {"ok": False, "message": "调用失败：请检查 Key、Base URL、模型名或额度"}
    return {"ok": True, "message": f"连接成功（模型 {llm.status()['model']}）"}
