"""运营分析：看板聚合、复盘（转化明细/报告/权重建议）、预测校准。

全部为确定性聚合计算，不经过大模型；权重修正只输出建议文本，
人工确认后改 config.py 才生效（ENABLE_WEIGHT_WRITEBACK 保持默认关闭）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AccountProfile,
    CommentDraft,
    ComplianceHit,
    ProductFact,
    SalesFact,
    Topic,
    TopicScore,
    VideoFact,
)
from app.services import scoring

_FLOW_ORDER = ["draft", "scored", "reviewed", "approved", "published"]
_DIM_LABELS = {
    "finish_rate": "预估完播率", "topic_heat": "垂类热度", "competition": "竞争饱和度",
    "audience_match": "粉丝画像匹配", "publish_window": "发布时段",
    "commission": "佣金比例", "conversion_history": "同类目转化",
    "price_fit": "价格带匹配", "return_risk": "退货风险",
}


def _latest_scores(db: Session) -> dict[int, TopicScore]:
    latest: dict[int, TopicScore] = {}
    for s in db.execute(select(TopicScore).order_by(TopicScore.version)).scalars():
        latest[s.topic_id] = s
    return latest


# ---------------- 看板 ----------------

def dashboard(db: Session) -> dict:
    topics = list(db.execute(select(Topic)).scalars())
    status_count: dict[str, int] = {}
    cat_count: dict[str, int] = {}
    for t in topics:
        status_count[t.status] = status_count.get(t.status, 0) + 1
        c = t.category or "未分类"
        cat_count[c] = cat_count.get(c, 0) + 1

    buckets = {"90分以上": 0, "75-89分": 0, "60-74分": 0, "60分以下": 0}
    for s in _latest_scores(db).values():
        v = s.total_score
        if v >= 90:
            buckets["90分以上"] += 1
        elif v >= 75:
            buckets["75-89分"] += 1
        elif v >= 60:
            buckets["60-74分"] += 1
        else:
            buckets["60分以下"] += 1

    hits = list(db.execute(select(ComplianceHit)).scalars())
    hit_levels = {"P0": 0, "P1": 0, "P2": 0}
    for h in hits:
        hit_levels[h.level] = hit_levels.get(h.level, 0) + 1

    drafts = list(db.execute(select(CommentDraft)).scalars())
    intent_count: dict[str, int] = {}
    for d in drafts:
        intent_count[d.intent] = intent_count.get(d.intent, 0) + 1

    monthly: dict[str, dict] = {}
    for v in db.execute(select(VideoFact)).scalars():
        if not v.published_at:
            continue
        key = v.published_at.strftime("%Y-%m")
        m = monthly.setdefault(key, {"views": 0, "interactions": 0, "count": 0})
        m["views"] += v.views
        m["interactions"] += v.likes + v.comments + v.shares
        m["count"] += 1
    trend = [{"month": k, **monthly[k]} for k in sorted(monthly)]

    sales_rows = list(db.execute(select(SalesFact)).scalars())

    return {
        "funnel": [{"status": s, "count": status_count.get(s, 0)} for s in _FLOW_ORDER]
                  + [{"status": "rejected", "count": status_count.get("rejected", 0)}],
        "score_buckets": buckets,
        "compliance_levels": hit_levels,
        "comment_intents": intent_count,
        "trend": trend,
        "topic_categories": cat_count,
        "totals": {
            "topics": len(topics),
            "videos": db.execute(select(VideoFact)).scalars().all().__len__(),
            "products": db.execute(select(ProductFact)).scalars().all().__len__(),
            "sales_rows": len(sales_rows),
            "gmv": round(sum(r.gmv for r in sales_rows), 2),
        },
    }


# ---------------- 复盘：转化明细 ----------------

def conversion_detail(db: Session, days: int | None = None) -> list[dict]:
    rows = list(db.execute(select(SalesFact)).scalars())
    if days:
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = [r for r in rows if not r.stat_date or r.stat_date >= since]
    videos = {v.external_id: v for v in db.execute(select(VideoFact)).scalars()}
    products = {p.external_id: p for p in db.execute(select(ProductFact)).scalars()}

    agg: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r.video_external_id, r.product_external_id)
        item = agg.setdefault(key, {"sales_count": 0, "gmv": 0.0,
                                    "return_count": 0, "return_amount": 0.0,
                                    "dates": []})
        item["sales_count"] += r.sales_count
        item["gmv"] += r.gmv
        item["return_count"] += r.return_count
        item["return_amount"] += r.return_amount
        if r.stat_date:
            item["dates"].append(r.stat_date)

    out = []
    for (vid, pid), item in agg.items():
        v, p = videos.get(vid), products.get(pid)
        views = v.views if v else 0
        out.append({
            "video_external_id": vid,
            "video_title": v.title if v else "（未找到视频）",
            "product_external_id": pid,
            "product_title": p.title if p else "（未找到商品）",
            "category": p.category if p else "",
            "first_date": min(item["dates"]) if item["dates"] else "",
            "last_date": max(item["dates"]) if item["dates"] else "",
            "sales_count": item["sales_count"],
            "gmv": round(item["gmv"], 2),
            "return_count": item["return_count"],
            "return_amount": round(item["return_amount"], 2),
            "views": views,
            "finish_rate": v.finish_rate if v else None,
            "cvr": round(item["sales_count"] / views, 6) if views else None,
            "commission": round(item["gmv"] * (p.commission_rate if p else 0.0), 2),
        })
    out.sort(key=lambda x: x["gmv"], reverse=True)
    return out


# ---------------- 复盘：报告与权重建议 ----------------

def _weight_suggestions(db: Session, detail: list[dict]) -> list[dict]:
    """对比"高转化选题"与"低转化选题"的各维度平均分，给出权重调整建议。

    只出建议、不自动生效；样本不足时如实说明，不硬凑结论。
    """
    by_video: dict[str, dict] = {}
    for r in detail:
        item = by_video.setdefault(r["video_external_id"], {"sales": 0, "views": 0})
        item["sales"] += r["sales_count"]
        item["views"] += r["views"]

    latest = _latest_scores(db)
    pairs: list[tuple[float, Topic, TopicScore]] = []
    for t in db.execute(select(Topic)).scalars():
        if not t.published_video_external_id:
            continue
        agg = by_video.get(t.published_video_external_id)
        score_row = latest.get(t.id)
        if not agg or score_row is None or not agg["views"]:
            continue
        pairs.append((agg["sales"] / agg["views"], t, score_row))

    suggestions: list[dict] = []
    if len(pairs) < 4:
        have = len(pairs)
        return [{
            "dimension": "样本提示",
            "action": "暂不建议调整权重",
            "evidence": (f"当前有 {have} 条「已发布且已回流销售」的选题可对比，"
                         "累计到 4 条以上再对比权重，结论才站得住。"),
            "level": "muted",
        }]

    pairs.sort(key=lambda x: -x[0])
    half = len(pairs) // 2
    groups = {"高转化组": pairs[:half], "低转化组": pairs[half:]}

    dim_scores: dict[str, dict[str, list[float]]] = {}
    for gname, group in groups.items():
        for _, _, s in group:
            dims = (s.content_detail or []) + (s.product_detail or [])
            for d in dims:
                dim_scores.setdefault(d["name"], {}).setdefault(gname, []).append(d["score"])

    for name, by_group in dim_scores.items():
        top, bottom = by_group.get("高转化组", []), by_group.get("低转化组", [])
        if not top or not bottom:
            continue
        avg_top = sum(top) / len(top)
        avg_bottom = sum(bottom) / len(bottom)
        diff = avg_top - avg_bottom
        if diff >= 12:
            suggestions.append({
                "dimension": _DIM_LABELS.get(name, name),
                "action": "可考虑提高权重",
                "evidence": f"高转化组平均 {avg_top:.0f} 分 vs 低转化组 {avg_bottom:.0f} 分（差 {diff:.0f}），区分度明显。",
                "level": "up",
            })
        elif diff <= -12:
            suggestions.append({
                "dimension": _DIM_LABELS.get(name, name),
                "action": "可考虑降低权重",
                "evidence": f"低转化组平均 {avg_bottom:.0f} 分反高于高转化组 {avg_top:.0f} 分，可能高估了它。",
                "level": "down",
            })
        if len(suggestions) >= 3:
            break

    avg_cvr_top = sum(p[0] for p in pairs[:half]) / half
    avg_cvr_bottom = sum(p[0] for p in pairs[half:]) / len(pairs[half:])
    if avg_cvr_bottom > avg_cvr_top:
        suggestions.insert(0, {
            "dimension": "整体信号",
            "action": "建议复核双引擎权重",
            "evidence": (f"总分较高的一半选题平均转化率 {avg_cvr_top:.2%}，"
                         f"反而低于另一半 {avg_cvr_bottom:.2%}，评分与实际出现背离。"),
            "level": "warn",
        })

    if not suggestions:
        suggestions.append({
            "dimension": "整体信号",
            "action": "权重暂不需要调整",
            "evidence": f"各维度在高低转化组之间区分度不明显（样本 {len(pairs)} 条），继续积累数据。",
            "level": "muted",
        })
    return suggestions


def report(db: Session, days: int = 30) -> dict:
    detail = conversion_detail(db, days=days)
    sales = sum(r["sales_count"] for r in detail)
    gmv = round(sum(r["gmv"] for r in detail), 2)
    returns = sum(r["return_count"] for r in detail)
    return_amount = round(sum(r["return_amount"] for r in detail), 2)
    commission = round(sum(r["commission"] for r in detail), 2)

    by_category: dict[str, dict] = {}
    for r in detail:
        cat = r["category"] or "未分类"
        item = by_category.setdefault(cat, {"sales": 0, "gmv": 0.0, "commission": 0.0})
        item["sales"] += r["sales_count"]
        item["gmv"] += r["gmv"]
        item["commission"] += r["commission"]

    top = detail[:3]
    suggestions = _weight_suggestions(db, detail)

    lines = [f"# 内容运营复盘（近 {days} 天）", "",
             f"生成时间：{datetime.now():%Y-%m-%d %H:%M}", ""]
    if detail:
        lines += [
            "## 核心数字", "",
            f"- 成交：{sales} 单，GMV ¥{gmv:,.0f}，预估佣金 ¥{commission:,.0f}",
            f"- 退货：{returns} 单（¥{return_amount:,.0f}），退货率 {returns / sales:.1%}" if sales else "- 退货：—",
            "",
            "## Top 视频×商品", "",
        ]
        for i, r in enumerate(top, start=1):
            lines.append(f"{i}. 「{r['video_title']}」×「{r['product_title']}」："
                         f"GMV ¥{r['gmv']:,.0f}，转化率 {r['cvr']:.2%}" if r['cvr'] else
                         f"{i}. 「{r['video_title']}」×「{r['product_title']}」：GMV ¥{r['gmv']:,.0f}")
        lines += ["", "## 类目分布", ""]
        for cat, item in sorted(by_category.items(), key=lambda x: -x[1]["gmv"]):
            lines.append(f"- {cat}：{item['sales']} 单 / GMV ¥{item['gmv']:,.0f}")
    else:
        lines.append("本期还没有销售数据。先到「数据导入」上传 sales.csv（视频×商品销售回流），"
                     "或点「下载样例」跑通流程。")

    lines += ["", "## 权重修正建议（人工确认后生效）", ""]
    for s in suggestions:
        lines.append(f"- **{s['dimension']}**：{s['action']} —— {s['evidence']}")
    lines += ["", "> 说明：建议由确定性聚合计算生成，不自动生效；确认后改 app/config.py 的权重。",
              "> 违禁词与资质表为种子版本，以平台官方规则为准。"]

    return {
        "days": days,
        "totals": {"sales": sales, "gmv": gmv, "returns": returns,
                   "return_amount": return_amount, "commission": commission,
                   "return_rate": round(returns / sales, 4) if sales else None},
        "top": top,
        "by_category": [{"category": k, **v} for k, v in
                        sorted(by_category.items(), key=lambda x: -x[1]["gmv"])],
        "suggestions": suggestions,
        "markdown": "\n".join(lines),
    }


# ---------------- 预测校准 ----------------

def calibration(db: Session) -> dict:
    """预测 vs 实际：把评分时的预估完播率与发布后的实际完播率对比。"""
    profile = db.execute(select(AccountProfile)).scalars().first()
    videos = {v.external_id: v for v in db.execute(select(VideoFact)).scalars()}

    rows = []
    for t in db.execute(select(Topic)).scalars():
        if not t.published_video_external_id:
            continue
        v = videos.get(t.published_video_external_id)
        if v is None:
            continue
        predicted, reason = scoring.predicted_finish_rate(t, profile)
        actual = round(v.finish_rate, 4)
        rows.append({
            "topic_id": t.id,
            "topic_title": t.title,
            "video_external_id": t.published_video_external_id,
            "video_title": v.title,
            "hook_strength": t.hook_strength,
            "duration_sec": t.estimated_duration_sec,
            "predicted": predicted,
            "actual": actual,
            "deviation": round(actual - predicted, 4),
            "reason": reason,
        })

    if not rows:
        return {
            "rows": [], "n": 0, "avg_deviation": None, "hit_rate": None,
            "conclusion": "还没有「已发布」的选题可校准：审批通过后在选题卡片上点「标记已发布」，挂到真实视频上即可。",
        }

    deviations = [r["deviation"] for r in rows]
    avg_dev = round(sum(deviations) / len(deviations), 4)
    hit_rate = round(sum(1 for d in deviations if abs(d) <= 0.03) / len(deviations), 4)
    if len(rows) < 3:
        conclusion = f"样本较少（{len(rows)} 条），先看方向：实际完播平均比预估 {'高' if avg_dev >= 0 else '低'} {abs(avg_dev):.1%}。"
    elif avg_dev <= -0.03:
        conclusion = f"实际完播率普遍低于预估（平均低 {abs(avg_dev):.1%}）：钩子强度自评可能偏高，建议调低自评或加强前 3 秒。"
    elif avg_dev >= 0.03:
        conclusion = f"实际完播率普遍高于预估（平均高 {avg_dev:.1%}）：可以更自信一些，或继续加大钩子投入。"
    else:
        conclusion = f"预估与实际基本吻合（平均偏差 {abs(avg_dev):.1%}，命中率 {hit_rate:.0%}），评分引擎的完播预估可用。"

    return {"rows": rows, "n": len(rows), "avg_deviation": avg_dev,
            "hit_rate": hit_rate, "conclusion": conclusion}
