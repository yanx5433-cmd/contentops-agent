"""API schema：Pydantic 请求/响应模型。"""
from datetime import datetime

from pydantic import BaseModel, Field


class TopicIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    outline: str = ""
    category: str = ""
    estimated_duration_sec: int = Field(default=30, ge=5, le=600)
    hook_strength: float = Field(default=0.5, ge=0.0, le=1.0)
    product_external_id: str | None = None
    publish_hour: int | None = Field(default=None, ge=0, le=23)


class ScoreOut(BaseModel):
    topic_id: int
    version: int
    content_score: float
    product_score: float | None
    total_score: float
    weaknesses: list[str]
    detail: dict


class ComplianceHitOut(BaseModel):
    level: str
    kind: str
    matched: str
    action: str


class ReviewOut(BaseModel):
    topic_id: int
    blocked: bool
    need_manual_review: bool
    hits: list[ComplianceHitOut]
    topic_status: str


class ApprovalIn(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    reviewer: str = "owner"
    note: str = ""


class ImportOut(BaseModel):
    created: int
    skipped: int
    batch: str


class AuditOut(BaseModel):
    id: int
    action: str
    entity_type: str
    entity_id: str
    detail: dict
    created_at: datetime


class CommentDraftIn(BaseModel):
    comment_text: str = Field(min_length=1, max_length=1000)
    video_external_id: str = ""


class CommentDraftOut(BaseModel):
    id: int
    intent: str
    intent_label: str
    confidence: str
    draft: str
    source: str
    status: str
    notes: list[str] = []


class CommentFinalizeIn(BaseModel):
    final_text: str = Field(min_length=1, max_length=1000)
    status: str = Field(default="edited", pattern="^(edited|sent|discarded)$")


class KBQueryIn(BaseModel):
    query: str = Field(min_length=1, max_length=300)
    top_k: int = Field(default=5, ge=1, le=10)


class CheckTextIn(BaseModel):
    """文案体检：任意文案（标题/简介/评论回复）。"""
    text: str = Field(min_length=1, max_length=2000)


class BrainstormIn(BaseModel):
    """AI 选题风暴：类目 + 关键词 → 候选选题列表。"""
    category: str = Field(default="女装", max_length=32)
    keywords: list[str] = Field(default_factory=list, max_length=8)
    count: int = Field(default=10, ge=3, le=20)
    publish_hour: int | None = Field(default=19, ge=0, le=23)


class PublishIn(BaseModel):
    """发布挂接：把选题关联到已发布视频（用于预测校准）。"""
    video_external_id: str = Field(min_length=1, max_length=64)


class LLMSettingsIn(BaseModel):
    """LLM 接入配置（界面写入）：只传要修改的字段。"""
    api_key: str | None = Field(default=None, max_length=200)
    base_url: str | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, max_length=100)
    daily_cap: int | None = Field(default=None, ge=0, le=10000)
