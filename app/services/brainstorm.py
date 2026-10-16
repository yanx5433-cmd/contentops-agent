"""AI 选题风暴：生成候选选题 → 合规预筛 → 批量评分排序。

生成：LLM 可用时由模型产出（JSON 数组），失败或未配置时用模式模板确定性拼装；
筛选：候选先过三级合规（P0 直接丢弃，P1/P2 标注），再走双引擎评分排序；
输出：只产出候选与分数，不自动入库——用户点「加入选题」才落库（人工在环上）。
"""
from __future__ import annotations

import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountProfile, Topic, VideoFact
from app.services import compliance, llm, scoring

_SCENES = ["小个子", "梨形身材", "学生党", "通勤", "微胖"]
_PRICE_WORDS = ["50元内", "百元内", "100-200元"]
_DEFAULT_ITEMS = {
    "女装": ["秋冬连衣裙", "加绒打底裤", "针织开衫", "高腰半身裙", "毛呢外套"],
    "鞋包": ["短靴", "通勤托特包", "帆布包"],
    "饰品": ["简约耳饰", "叠戴项链", "发饰"],
    "美妆": ["素颜口红", "秋冬面霜", "持妆粉底"],
    "食品": ["办公室零食", "低卡代餐"],
}
_PATTERNS = [
    "{scene}{item}实测：三套直接抄",
    "{item}避坑指南：先看这三点",
    "{price}{item}合集：{scene}别错过",
    "{item}对比测评：贵在哪里",
    "{scene}怎么选{item}：四个技巧",
    "换季衣橱：{item}该留该扔",
    "{item}三色实拍：哪件最惊喜",
]


def _llm_titles(category: str, keywords: list[str], count: int) -> list[str] | None:
    if not llm.llm_available():
        return None
    kw = "、".join(keywords) if keywords else "（无，按类目自行发挥）"
    system = ("你是女装穿搭带货账号的选题策划。按要求输出选题标题列表，"
              "只输出 JSON 字符串数组，不要任何解释；标题口语化、20 字以内，"
              "避免极限词、功效承诺和绝对化表述。")
    prompt = (f"账号风格：年轻女性向带货短视频，粉丝以学生党和年轻通勤族为主。"
              f"类目：{category}。关键词参考：{kw}。请生成 {count} 个角度各异的选题标题。")
    result = llm.chat(prompt, system=system)
    if not result:
        return None
    try:
        m = re.search(r"\[[\s\S]*\]", result.text)
        data = json.loads(m.group(0)) if m else []
        titles = [str(x).strip() for x in data if str(x).strip()][:count]
        return titles or None
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None


def _template_titles(category: str, keywords: list[str], count: int) -> list[str]:
    items = keywords or _DEFAULT_ITEMS.get(category, _DEFAULT_ITEMS["女装"])
    titles: list[str] = []
    i = 0
    while len(titles) < count and i < len(_PATTERNS) * len(items) * 3:
        pat = _PATTERNS[i % len(_PATTERNS)]
        item = items[(i // len(_PATTERNS)) % len(items)]
        scene = _SCENES[(i * 3) % len(_SCENES)]
        price = _PRICE_WORDS[i % len(_PRICE_WORDS)]
        t = pat.format(scene=scene, item=item, price=price)
        if t not in titles and len(t) <= 30:
            titles.append(t)
        i += 1
    return titles[:count]


def generate(db: Session, category: str, keywords: list[str], count: int = 10,
             publish_hour: int | None = 19) -> dict:
    category = (category or "女装").strip()
    titles = _llm_titles(category, keywords, count)
    source = "llm"
    if not titles:
        titles = _template_titles(category, keywords, count)
        source = "template"

    profile = db.execute(select(AccountProfile)).scalars().first()
    videos = list(db.execute(select(VideoFact)).scalars())

    candidates = []
    for title in titles:
        spans = compliance.check_text_spans(title)
        if any(s["level"] == "P0" for s in spans):
            continue  # P0 命中直接丢弃，不给用户"看看就好"的机会
        topic = Topic(title=title, category=category, estimated_duration_sec=30,
                      hook_strength=0.65, product_id=None)
        result = scoring.score_topic(topic, profile, videos, publish_hour)
        candidates.append({
            "title": title,
            "content_score": result.content_score,   # 未挂车，只展示内容分
            "weaknesses": result.weaknesses,
            "compliance_flags": [f'{s["level"]}:{s["word"]}' for s in spans],
        })
    candidates.sort(key=lambda c: c["content_score"], reverse=True)
    return {"source": source, "category": category, "count": len(candidates),
            "candidates": candidates}
