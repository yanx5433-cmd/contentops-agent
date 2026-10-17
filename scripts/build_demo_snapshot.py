"""生成 GitHub Pages 静态演示版的数据快照。

从正在运行的后端抓取演示状态，并把评分/合规/检索所需的常量（词库、
改写建议、类目资质、评论规则、知识库原文、画像与视频/商品事实）
一起打包成 docs/demo-data.js，供浏览器内移植版引擎使用。
"""
import json
import sqlite3
from pathlib import Path

import httpx

REPO = Path(r"C:/Users/86134/Desktop/简历/contentops-agent")
BASE = "http://localhost:8100"
OUT = REPO / "docs" / "demo-data.js"


def get(path: str):
    r = httpx.get(BASE + path, timeout=30)
    r.raise_for_status()
    return r.json()


def main() -> None:
    # --- 实况数据（来自真实后端）---
    overview = get("/api/overview")
    audit = get("/api/audit?limit=50")
    topics_brief = get("/api/topics?limit=50")
    topics = [get(f"/api/topics/{t['id']}") for t in topics_brief]
    comments = get("/api/comments/drafts?limit=50")
    dashboard = get("/api/dashboard")
    calibration = get("/api/review/calibration")
    report = get("/api/review/report?days=90")
    conversion = get("/api/review/conversion?days=90")

    # --- 事实数据（来自 SQLite）---
    db = sqlite3.connect(REPO / "data" / "contentops.db")
    db.row_factory = sqlite3.Row
    profile_row = db.execute("select audience, price_bands, baselines from account_profile").fetchone()
    videos = [
        {"external_id": r["external_id"], "title": r["title"], "views": r["views"],
         "likes": r["likes"], "comments": r["comments"], "shares": r["shares"],
         "finish_rate": r["finish_rate"], "published_at": r["published_at"]}
        for r in db.execute("select * from video_fact order by published_at")
    ]
    products = {}
    for r in db.execute("select * from product_fact"):
        products[r["external_id"]] = {
            "id": r["id"], "title": r["title"], "category": r["category"], "price": r["price"],
            "commission_rate": r["commission_rate"], "sales_count": r["sales_count"],
            "return_rate": r["return_rate"],
            "required_licenses": json.loads(r["required_licenses"] or "[]"),
        }
    topic_facts = {}
    for r in db.execute("select * from topic"):
        topic_facts[r["id"]] = {
            "title": r["title"], "outline": r["outline"], "category": r["category"],
            "estimated_duration_sec": r["estimated_duration_sec"],
            "hook_strength": r["hook_strength"], "product_id": r["product_id"],
            "status": r["status"],
            "published_video_external_id": r["published_video_external_id"] or "",
            "created_at": r["created_at"],
        }
    db.close()

    audience = json.loads(profile_row["audience"])
    profile = {
        "category_pref": audience.get("category_pref", {}),
        "price_bands": json.loads(profile_row["price_bands"]),
        "baselines": json.loads(profile_row["baselines"]),
    }

    # --- 规则与常量（来自后端模块）---
    import sys
    sys.path.insert(0, str(REPO))
    from app.services import brainstorm, comment_assistant, compliance

    knowledge = [
        {"doc": f.name, "content": f.read_text(encoding="utf-8")}
        for f in sorted((REPO / "data" / "knowledge").glob("*.md"))
    ]

    data = {
        "generated_at": "2026-10-17",
        "overview": overview,
        "audit": audit,
        "topics": topics,
        "topic_facts": topic_facts,
        "comments": comments,
        "dashboard": dashboard,
        "calibration": calibration,
        "report": report,
        "conversion": conversion,
        "videos": videos,
        "products": products,
        "profile": profile,
        "banned_words": compliance.BANNED_WORDS,
        "rewrite_hints": compliance.REWRITE_HINTS,
        "category_licenses": compliance.CATEGORY_LICENSES,
        "comment_rules": [[intent, words] for intent, words in comment_assistant._RULES],
        "comment_templates": comment_assistant._TEMPLATES,
        "intent_labels": comment_assistant.INTENT_LABELS,
        "brainstorm": {
            "scenes": brainstorm._SCENES,
            "prices": brainstorm._PRICE_WORDS,
            "default_items": brainstorm._DEFAULT_ITEMS,
            "patterns": brainstorm._PATTERNS,
        },
        "knowledge": knowledge,
    }

    js = "window.DEMO_DATA = " + json.dumps(data, ensure_ascii=False, indent=1) + ";\n"
    OUT.write_text(js, encoding="utf-8")
    print(f"written {OUT} ({len(js)/1024:.0f} KB)")
    print(f"topics={len(topics)} videos={len(videos)} products={len(products)} "
          f"comments={len(comments)} kb_docs={len(knowledge)}")


if __name__ == "__main__":
    main()
