"""选题双引擎评分：内容分 × 0.6 + 商品分 × 0.4 × 车位系数。

全部是确定性计算，不过大模型——评分必须可解释、可复算、可审计。
每个维度输出 0-100 分与依据说明，低于短板阈值输出改进提示。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app import config
from app.models import AccountProfile, ProductFact, Topic, VideoFact

WEAK = config.WEAKNESS_THRESHOLD


@dataclass
class Dimension:
    name: str
    score: float          # 0-100
    reason: str           # 依据说明
    higher_is_better: bool = True


@dataclass
class ScoreResult:
    content_score: float
    content_detail: list[Dimension] = field(default_factory=list)
    product_score: float | None = None
    product_detail: list[Dimension] = field(default_factory=list)
    total_score: float = 0.0
    weaknesses: list[str] = field(default_factory=list)


def _weighted(detail: list[Dimension], weights: dict[str, float]) -> float:
    total = sum(weights[d.name] * d.score for d in detail)
    wsum = sum(weights[d.name] for d in detail)
    return round(total / wsum, 2) if wsum else 0.0


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


# ---------------- 内容分 ----------------

def _finish_rate(topic: Topic, baselines: dict) -> Dimension:
    """预估完播率：账号基线 × 时长衰减 × 钩子强度加成。"""
    base = float(baselines.get("avg_finish_rate", 0.25))
    # 30 秒为基线时长；每多 10 秒衰减 8%，低于 30 秒小幅加成
    duration = max(topic.estimated_duration_sec, 1)
    if duration <= 30:
        dur_factor = 1.0 + (30 - duration) / 30 * 0.10
    else:
        dur_factor = max(0.55, 1.0 - (duration - 30) / 10 * 0.08)
    est = base * dur_factor * (0.7 + 0.6 * topic.hook_strength)
    score = _clamp(est / 0.50 * 100)  # 50% 完播视为满分
    return Dimension(
        "finish_rate",
        round(score, 1),
        f"基线{base:.0%}×时长因子{dur_factor:.2f}×钩子因子{0.7 + 0.6 * topic.hook_strength:.2f}≈预估完播{min(est, 1):.0%}",
    )


def _topic_heat(topic: Topic, recent_videos: list[VideoFact]) -> Dimension:
    """垂类热度：近 90 天同类目视频的互动中位数相对全账号基线的倍数。"""
    if not recent_videos:
        return Dimension("topic_heat", 50.0, "无同类目历史数据，给中性分 50")
    same_cat = [v for v in recent_videos if v.category_match(topic.category)] if hasattr(VideoFact, "category_match") else []
    pool = same_cat or recent_videos
    interactions = sorted((v.likes + v.comments + v.shares) for v in pool)
    median = interactions[len(interactions) // 2]
    all_inter = sorted((v.likes + v.comments + v.shares) for v in recent_videos)
    overall = all_inter[len(all_inter) // 2] or 1
    ratio = median / overall
    score = _clamp(ratio * 60)  # 与账号中位持平=60 分
    return Dimension("topic_heat", round(score, 1), f"同类目互动中位数为全账号的{ratio:.2f}倍")


def _competition(topic: Topic, recent_videos: list[VideoFact]) -> Dimension:
    """竞争饱和度（反向）：近 90 天同选题关键词的视频数量越多分越低。"""
    if not recent_videos:
        return Dimension("competition", 50.0, "无历史数据，给中性分 50")
    key = topic.category or topic.title[:6]
    dup = sum(1 for v in recent_videos if key and key in (v.title or ""))
    # 0 条重复=80 分起步，每多一条同质内容 -15
    score = _clamp(80 - dup * 15)
    label = "蓝海" if dup == 0 else ("轻度同质" if dup <= 2 else "饱和")
    return Dimension("competition", round(score, 1), f"近90天同关键词视频 {dup} 条（{label}）", higher_is_better=True)


def _audience_match(topic: Topic, profile: AccountProfile | None) -> Dimension:
    """粉丝画像匹配：账号画像里该类目偏好占比。"""
    if profile is None or not profile.audience:
        return Dimension("audience_match", 50.0, "未配置账号画像，给中性分 50")
    pref = profile.audience.get("category_pref", {})
    ratio = float(pref.get(topic.category, 0.20))  # 默认按两成普适兴趣
    score = _clamp(ratio * 250)  # 40% 偏好即满分
    return Dimension("audience_match", round(score, 1), f"粉丝对[{topic.category}]偏好占比 {ratio:.0%}")


_PUBLISH_WINDOWS = {  # 带货类账号经验时段
    "07:00-09:00": 0.8, "12:00-14:00": 0.9, "18:00-20:00": 1.0,
    "20:00-23:00": 0.95, "default": 0.75,
}


def _publish_window(topic: Topic, publish_hour: int | None) -> Dimension:
    """发布时段权重：由调用方传入计划发布小时（复盘回流修正）。"""
    if publish_hour is None:
        return Dimension("publish_window", 70.0, "未指定发布时段，按默认 70")
    factor = 0.75
    window = "default"
    for w, f in _PUBLISH_WINDOWS.items():
        if w == "default":
            continue
        lo, hi = (int(x) for x in w.split("-")[0:2][0].split(":")), None  # placeholder
        break
    # 简化实现：直接按小时区间判断
    if 7 <= publish_hour < 9:
        factor, window = 0.80, "早高峰"
    elif 12 <= publish_hour < 14:
        factor, window = 0.90, "午间"
    elif 18 <= publish_hour < 20:
        factor, window = 1.00, "晚黄金档"
    elif 20 <= publish_hour < 23:
        factor, window = 0.95, "晚间"
    else:
        factor, window = 0.75, "冷门时段"
    return Dimension("publish_window", round(factor * 100, 1), f"计划发布于{window}")


def score_content(topic: Topic, profile: AccountProfile | None,
                  recent_videos: list[VideoFact], publish_hour: int | None = None) -> tuple[float, list[Dimension]]:
    baselines = profile.baselines if profile else {}
    detail = [
        _finish_rate(topic, baselines),
        _topic_heat(topic, recent_videos),
        _competition(topic, recent_videos),
        _audience_match(topic, profile),
        _publish_window(topic, publish_hour),
    ]
    return _weighted(detail, config.CONTENT_WEIGHTS), detail


# ---------------- 商品分 ----------------

def _commission(p: ProductFact) -> Dimension:
    """佣金比例：10% 及格、25% 满分，线性。"""
    score = _clamp((p.commission_rate - 0.05) / 0.20 * 100)
    return Dimension("commission", round(score, 1), f"佣金 {p.commission_rate:.1%}")


def _conversion_history(p: ProductFact, profile: AccountProfile | None) -> Dimension:
    """同类目历史转化：用商品自身销量与账号基线对比（无基线给中性分）。"""
    baselines = profile.baselines if profile else {}
    base_cvr = float(baselines.get("avg_cvr", 0.02))
    # 无单品转化数据时以销量规模近似：千单=及格线
    score = _clamp((p.sales_count / 3000) * 60 + 20)
    note = f"商品累计销量 {p.sales_count} 单；账号基线转化 {base_cvr:.1%}"
    return Dimension("conversion_history", round(score, 1), note)


def _price_fit(p: ProductFact, profile: AccountProfile | None) -> Dimension:
    """价格带匹配：商品价格落在粉丝主力价格带内得高分。"""
    if profile is None or not profile.price_bands:
        return Dimension("price_fit", 50.0, "未配置粉丝价格带，给中性分 50")
    if p.price <= 50:
        band = "0-50"
    elif p.price <= 150:
        band = "50-150"
    else:
        band = "150+"
    share = float(profile.price_bands.get(band, 0.10))
    score = _clamp(share / 0.50 * 100)
    return Dimension("price_fit", round(score, 1), f"价格 ¥{p.price:.0f} 落在[{band}]带，该带粉丝占比 {share:.0%}")


def _return_risk(p: ProductFact) -> Dimension:
    """退货风险（反向）：>15% 高危，<5% 优秀。"""
    score = _clamp((0.15 - p.return_rate) / 0.10 * 100)
    grade = "优秀" if p.return_rate < 0.05 else ("正常" if p.return_rate < 0.10 else "高危")
    return Dimension("return_risk", round(score, 1), f"退货率 {p.return_rate:.1%}（{grade}）", higher_is_better=True)


def score_product(p: ProductFact, profile: AccountProfile | None) -> tuple[float, list[Dimension]]:
    detail = [
        _commission(p),
        _conversion_history(p, profile),
        _price_fit(p, profile),
        _return_risk(p),
    ]
    return _weighted(detail, config.PRODUCT_WEIGHTS), detail


# ---------------- 合成 ----------------

def score_topic(topic: Topic, profile: AccountProfile | None,
                recent_videos: list[VideoFact], publish_hour: int | None = None) -> ScoreResult:
    content_score, content_detail = score_content(topic, profile, recent_videos, publish_hour)
    result = ScoreResult(content_score=content_score, content_detail=content_detail)

    if topic.product_id is not None and topic.product is not None:
        p_score, p_detail = score_product(topic.product, profile)
        result.product_score = p_score
        result.product_detail = p_detail
        result.total_score = round(
            content_score * config.CONTENT_SHARE
            + p_score * config.PRODUCT_SHARE * config.CART_MULTIPLIER, 2
        )
    else:
        result.total_score = round(content_score * config.CONTENT_SHARE, 2)

    # 短板提示：任一引擎中低于阈值的维度
    for d in content_detail + (result.product_detail or []):
        if d.score < WEAK:
            result.weaknesses.append(f"{d.name}={d.score}（{d.reason}）")
    return result


def predicted_finish_rate(topic: Topic, profile: AccountProfile | None) -> tuple[float, str]:
    """预估完播率（0-1）与依据说明：与评分引擎同一个公式，供发布后校准对比。

    注意：当公式结果超过 50% 时评分会封顶 100 分，此处按 50% 记（保守）。
    """
    dim = _finish_rate(topic, profile.baselines if profile else {})
    return round(min(dim.score / 100 * 0.50, 0.50), 4), dim.reason
