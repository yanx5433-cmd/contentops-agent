"""数据模型：账号画像、视频事实、商品事实、选题、评分、合规、审批、审计。

事实表（video/product）与决策表（topic/score）分离：
播放量、转化率这类不断变化的事实只存导入快照，评分每次现算，
避免大模型或缓存里的旧数据冒充事实（对标 dxl 的"事实必须工具现查"原则）。
"""
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AccountProfile(Base):
    """账号画像：粉丝画像、消费力、历史转化基线。人工维护 + 数据导入更新。"""

    __tablename__ = "account_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    # 粉丝画像：{"female_ratio":0.6,"age_18_24":0.35,"cities_top3":["义乌","杭州","广州"]}
    audience: Mapped[dict] = mapped_column(JSON, default=dict)
    # 粉丝消费力：价格带分布 {"0-50":0.5,"50-150":0.35,"150+":0.15}
    price_bands: Mapped[dict] = mapped_column(JSON, default=dict)
    # 历史基线：{"avg_finish_rate":0.28,"avg_view":12000,"avg_cvr":0.021}
    baselines: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class VideoFact(Base):
    """视频事实快照：创作者后台导出的单条视频数据，只增不改。"""

    __tablename__ = "video_fact"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(64), unique=True)  # 抖音视频ID
    title: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_sec: Mapped[int] = mapped_column(Integer, default=0)
    views: Mapped[int] = mapped_column(Integer, default=0)
    finish_rate: Mapped[float] = mapped_column(Float, default=0.0)  # 完播率 0-1
    likes: Mapped[int] = mapped_column(Integer, default=0)
    comments: Mapped[int] = mapped_column(Integer, default=0)
    shares: Mapped[int] = mapped_column(Integer, default=0)
    new_followers: Mapped[int] = mapped_column(Integer, default=0)  # 本条带来涨粉
    imported_batch: Mapped[str] = mapped_column(String(64), default="")  # 导入批次
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ProductFact(Base):
    """挂车商品事实快照：橱窗/挂车导出，只增不改。"""

    __tablename__ = "product_fact"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(64), unique=True)  # 商品ID
    title: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(64), default="")  # 类目
    price: Mapped[float] = mapped_column(Float, default=0.0)         # 售价
    commission_rate: Mapped[float] = mapped_column(Float, default=0.0)  # 佣金比例 0-1
    sales_count: Mapped[int] = mapped_column(Integer, default=0)     # 累计销量
    return_rate: Mapped[float] = mapped_column(Float, default=0.0)   # 退货率 0-1
    # 资质要求：["食品经营许可证"] 为空表示无特殊资质
    required_licenses: Mapped[list] = mapped_column(JSON, default=list)
    imported_batch: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Topic(Base):
    """选题：一个候选内容想法，可挂 0-1 个商品（带货位）。"""

    __tablename__ = "topic"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    outline: Mapped[str] = mapped_column(Text, default="")   # 脚本大纲/钩子描述
    category: Mapped[str] = mapped_column(String(64), default="")
    estimated_duration_sec: Mapped[int] = mapped_column(Integer, default=30)
    hook_strength: Mapped[float] = mapped_column(Float, default=0.5)  # 钩子强度 0-1（自评）
    product_id: Mapped[int | None] = mapped_column(ForeignKey("product_fact.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft/scored/reviewed/approved/rejected/published
    # 发布挂接：发布后关联到真实视频，用于"预测 vs 实际"校准
    published_video_external_id: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    product: Mapped[ProductFact | None] = relationship("ProductFact")
    scores: Mapped[list["TopicScore"]] = relationship(back_populates="topic", cascade="all, delete-orphan")


class TopicScore(Base):
    """评分快照：每次评分落一条，含双引擎明细，可解释可追溯。"""

    __tablename__ = "topic_score"
    __table_args__ = (UniqueConstraint("topic_id", "version", name="uq_topic_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topic.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    content_score: Mapped[float] = mapped_column(Float)             # 内容分 0-100
    content_detail: Mapped[dict] = mapped_column(JSON)               # 各维度明细
    product_score: Mapped[float | None] = mapped_column(Float, nullable=True)  # 商品分 0-100（无挂车为空）
    product_detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    total_score: Mapped[float] = mapped_column(Float)               # 合成总分
    weaknesses: Mapped[list] = mapped_column(JSON, default=list)     # 短板提示
    engine_version: Mapped[str] = mapped_column(String(20), default="v1")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    topic: Mapped[Topic] = relationship(back_populates="scores")


class ComplianceHit(Base):
    """合规命中记录：选题/文案送审时的违禁词与资质检查结果。"""

    __tablename__ = "compliance_hit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topic.id"))
    level: Mapped[str] = mapped_column(String(8))  # P0/P1/P2
    kind: Mapped[str] = mapped_column(String(20))  # banned_word / license_missing / sensitive_claim
    matched: Mapped[str] = mapped_column(Text, default="")   # 命中词/缺失资质
    context: Mapped[str] = mapped_column(Text, default="")
    action: Mapped[str] = mapped_column(String(20), default="")  # block / manual_review / rewrite_hint
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Approval(Base):
    """人工审批：scored 的选题经人审后才进生产（approved/rejected）。"""

    __tablename__ = "approval"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topic.id"))
    decision: Mapped[str] = mapped_column(String(20))  # approved / rejected
    reviewer: Mapped[str] = mapped_column(String(64), default="owner")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CommentDraft(Base):
    """评论草稿：助手生成的回复草稿，仅草稿不发送，人工可改可弃。"""

    __tablename__ = "comment_draft"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_external_id: Mapped[str] = mapped_column(String(64), default="")  # 关联视频（可空）
    comment_text: Mapped[str] = mapped_column(Text)
    intent: Mapped[str] = mapped_column(String(20))               # purchase/product_qa/complaint/cooperation/other
    confidence: Mapped[str] = mapped_column(String(10), default="medium")
    draft: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(10), default="template")  # template/llm
    status: Mapped[str] = mapped_column(String(20), default="draft")     # draft/edited/sent/discarded
    final_text: Mapped[str | None] = mapped_column(Text, nullable=True)  # 人工修改后的定稿
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    """审计日志：所有导入、评分、审批、拦截动作全部留痕。"""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[str] = mapped_column(String(40))  # import/score/approve/reject/block/weight_update
    entity_type: Mapped[str] = mapped_column(String(30), default="")
    entity_id: Mapped[str] = mapped_column(String(30), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SalesFact(Base):
    """销售回流快照：视频×商品维度的销售/退货数据，只增不改。

    复盘与权重修正建议的依据；幂等键为 (视频, 商品, 日期)。
    """

    __tablename__ = "sales_fact"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_external_id: Mapped[str] = mapped_column(String(64))    # 挂车视频ID
    product_external_id: Mapped[str] = mapped_column(String(64))  # 商品ID
    stat_date: Mapped[str] = mapped_column(String(20), default="")  # 统计日期 YYYY-MM-DD（可空）
    sales_count: Mapped[int] = mapped_column(Integer, default=0)
    gmv: Mapped[float] = mapped_column(Float, default=0.0)          # 成交金额
    return_count: Mapped[int] = mapped_column(Integer, default=0)
    return_amount: Mapped[float] = mapped_column(Float, default=0.0)
    imported_batch: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AppSetting(Base):
    """应用配置键值表：LLM 接入参数、每日调用计数等（不随仓库分发）。"""

    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
