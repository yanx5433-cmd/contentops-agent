"""演示数据种子脚本：把数据库一键恢复到作品集 PDF 里的演示状态。

用途：
- Render 免费档磁盘是临时的，服务每次冷启动都会丢数据——由 render.yaml 的
  startCommand 在每次启动前运行本脚本重建演示状态，保证链接打开永远"有货"；
- 本地想快速恢复演示环境也可以手动运行。

幂等：只要 video_fact 里已有数据就跳过，不重复灌入。
"""
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.database import Base, SessionLocal, engine
from app.models import (
    AccountProfile,
    Approval,
    AuditLog,
    CommentDraft,
    ProductFact,
    Topic,
    TopicScore,
    VideoFact,
)
from app.services import comment_assistant, compliance, importer, scoring

RAW = Path(__file__).resolve().parent.parent / "data" / "raw"


def _batch(prefix: str) -> str:
    return f"{prefix}-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"


def _import_csv(db, path: Path, importer, prefix: str) -> None:
    content = path.read_bytes()
    result = importer(db, content, _batch(prefix))
    print(f"  导入 {path.name}: 新增 {result['created']}，跳过 {result.get('skipped', 0)}")


def _score(db, topic: Topic, hour: int) -> float:
    """复用确定性评分引擎，落一条版本化评分快照 + 审计日志。"""
    profile = db.execute(select(AccountProfile)).scalars().first()
    videos = list(db.execute(select(VideoFact)).scalars())
    result = scoring.score_topic(topic, profile, videos, hour)
    db.add(TopicScore(
        topic_id=topic.id, version=1,
        content_score=result.content_score,
        content_detail=[{"name": d.name, "score": d.score, "reason": d.reason} for d in result.content_detail],
        product_score=result.product_score,
        product_detail=[{"name": d.name, "score": d.score, "reason": d.reason} for d in result.product_detail] if result.product_detail else None,
        total_score=result.total_score,
        weaknesses=result.weaknesses,
    ))
    db.add(AuditLog(action="score", entity_type="topic", entity_id=str(topic.id),
                    detail={"version": 1, "total": result.total_score}))
    if topic.status == "draft":
        topic.status = "scored"
    db.flush()
    return result.total_score


def _review(db, topic: Topic) -> None:
    result = compliance.review_topic(db, topic)
    # 状态流转与 API 层保持一致：未被拦截的 scored 选题进入"已审查"
    if not result.blocked and topic.status == "scored":
        topic.status = "reviewed"
    db.add(AuditLog(action="compliance_review", entity_type="topic", entity_id=str(topic.id),
                    detail={"blocked": result.blocked, "levels": sorted(result.levels), "hits": len(result.hits)}))
    db.flush()


def _approve(db, topic: Topic) -> None:
    topic.status = "approved"
    db.add(Approval(topic_id=topic.id, decision="approved", reviewer="站长", note=""))
    db.add(AuditLog(action="approved", entity_type="topic", entity_id=str(topic.id),
                    detail={"reviewer": "站长", "note": ""}))
    db.flush()


def _publish(db, topic: Topic, video_external_id: str) -> None:
    topic.published_video_external_id = video_external_id
    topic.status = "published"
    db.add(AuditLog(action="publish", entity_type="topic", entity_id=str(topic.id),
                    detail={"video_external_id": video_external_id}))
    db.flush()


def _new_topic(db, title: str, outline: str, category: str, duration: int,
               hook_strength: float, product_external_id: str | None) -> Topic:
    product_id = None
    if product_external_id:
        product = db.execute(select(ProductFact).where(
            ProductFact.external_id == product_external_id)).scalars().first()
        if product is None:
            raise SystemExit(f"挂车商品 {product_external_id} 不存在，请先导入商品 CSV")
        product_id = product.id
    topic = Topic(title=title, outline=outline, category=category,
                  estimated_duration_sec=duration, hook_strength=hook_strength,
                  product_id=product_id)
    db.add(topic)
    db.flush()
    return topic


def _comment(db, text: str, status: str = "draft") -> None:
    result = comment_assistant.generate_draft(text)
    draft = CommentDraft(comment_text=text, intent=result.intent, confidence=result.confidence,
                         draft=result.draft, source=result.source, status=status,
                         final_text=result.draft if status == "sent" else None)
    db.add(draft)
    db.flush()
    db.add(AuditLog(action="comment_draft", entity_type="comment", entity_id=str(draft.id),
                    detail={"intent": result.intent, "source": result.source}))
    if status == "sent":
        db.add(AuditLog(action="comment_finalize", entity_type="comment", entity_id=str(draft.id),
                        detail={"status": "sent", "edited": False}))


def main() -> None:
    Base.metadata.create_all(bind=engine)  # 免费档冷启动时先于 uvicorn 执行，需要自己建表
    db = SessionLocal()
    try:
        if db.execute(select(VideoFact.id).limit(1)).scalars().first() is not None:
            print("数据库已有数据，跳过种子灌入。")
            return

        print("灌入演示数据…")
        _import_csv(db, RAW / "videos_sample.csv", importer.import_videos, "video")
        _import_csv(db, RAW / "products_sample.csv", importer.import_products, "product")
        _import_csv(db, RAW / "videos_womenswear_sample.csv", importer.import_videos, "video")
        _import_csv(db, RAW / "products_womenswear_sample.csv", importer.import_products, "product")
        _import_csv(db, RAW / "videos_extra_demo.csv", importer.import_videos, "video")
        _import_csv(db, RAW / "products_extra_demo.csv", importer.import_products, "product")
        sales = importer.import_sales(db, (RAW / "sales_sample.csv").read_bytes(),
                                      _batch("sales"))
        print(f"  导入 sales_sample.csv: 新增 {sales['created']}（销售回流）")

        if db.execute(select(AccountProfile)).scalars().first() is None:
            db.add(AccountProfile(
                name="奶油草莓（女装穿搭带货号）",
                audience={
                    "female_ratio": 0.77,
                    "age_dist": {"<23": 0.55, "24-30": 0.31, "31-40": 0.08, "41-50": 0.03, ">50": 0.03},
                    "city_dist": {"广东": 0.13, "江苏": 0.08, "浙江": 0.07, "四川": 0.06, "河南": 0.06, "山东": 0.06, "湖北": 0.04},
                    "cities_top3": ["广东", "江苏", "浙江"],
                    "device_dist": {"苹果": 0.53, "华为": 0.1, "小米": 0.07, "VIVO": 0.07, "OPPO": 0.06, "其他": 0.04, "荣耀": 0.04},
                    "activity_dist": {"重度": 0.69, "低活": 0.23, "轻度": 0.04, "中度": 0.04},
                    "category_pref": {"女装": 0.45, "鞋包": 0.2, "饰品": 0.15, "美妆": 0.1},
                },
                price_bands={"0-50": 0.5, "50-150": 0.35, "150+": 0.15},
                baselines={"avg_finish_rate": 0.25, "avg_view": 0, "avg_cvr": 0.02},
            ))
            db.flush()
            print("  账号画像：奶油草莓（女装穿搭带货号）")

        # 选题 1：好评通过（双引擎 + 合规通过 + 人工审批 + 发布挂接）
        t1 = _new_topic(db, "秋冬打底裤实测：59元加绒款穿三周起不起球",
                        "钩子：三周连续穿着实测，正面回答起球、掉档问题；三色上身实拍 + 尺码建议；结尾提醒到手价 59 元并引导进橱窗。",
                        "女装", 25, 0.70, "P101")
        total = _score(db, t1, 19)
        _review(db, t1)
        _approve(db, t1)
        _publish(db, t1, "V20260903")
        print(f"  选题 #{t1.id} {t1.title[:12]}… 总分 {total} → 已通过并挂接发布")

        # 选题 2：待审批（已评分）
        t2 = _new_topic(db, "感恩季｜百元内针织开衫三色叠穿",
                        "钩子：衣柜里最出片的一件；三色上身 + 通勤/约会双场景；评论区引导留言身高体重做尺码建议。",
                        "女装", 30, 0.70, "P103")
        total = _score(db, t2, 20)
        print(f"  选题 #{t2.id} {t2.title[:12]}… 总分 {total} → 待审批")

        # 选题 3：合规拦截（P1 违禁词 + P0 资质缺失，挂车无资质的保健食品）
        t3 = _new_topic(db, "减肥咖啡实测：喝出好身材", "钩子：30天瘦身变化对比；成分讲解；评论区收集反馈。",
                        "保健食品", 30, 0.60, "P016")
        total = _score(db, t3, 20)
        _review(db, t3)
        print(f"  选题 #{t3.id} {t3.title[:12]}… 总分 {total} → 合规拦截（{t3.status}）")

        # 选题 4：已审查（合规通过，等待人工终审）
        t4 = _new_topic(db, "自热小火锅试吃：深夜加班救星", "钩子：加班场景代入；试吃实拍；价格提醒。",
                        "食品", 30, 0.60, "P010")
        total = _score(db, t4, 21)
        _review(db, t4)
        print(f"  选题 #{t4.id} {t4.title[:12]}… 总分 {total} → 已审查")

        # 选题 5-7：已发布（用于「数据复盘」的预测校准与权重建议对比）
        for title, outline, duration, hook, hour, vid in [
            ("秋冬连衣裙实测：159小个子也能穿", "钩子：身高体重明细 + 三件上身对比；评论区收集反常识提问。", 30, 0.45, 19, "V20260901"),
            ("通勤连衣裙一件三穿实测", "钩子：一件连衣裙三种场景；结尾提醒换季清仓价。", 30, 0.40, 22, "VW20260921"),
            ("光腿神器实测：深肤色和浅肤色对比", "钩子：两只腿同框对比；结尾引导评论区报身高。", 30, 1.00, 19, "VW20260924"),
        ]:
            t = _new_topic(db, title, outline, "女装", duration, hook, None)
            total = _score(db, t, hour)
            _approve(db, t)
            _publish(db, t, vid)
            print(f"  选题 #{t.id} {t.title[:12]}… 总分 {total} → 已发布（挂接 {vid}）")

        for text, status in [
            ("这个裙子多少钱呀 求链接", "sent"),
            ("160 95斤穿什么码呀 会起球吗", "draft"),
            ("洗了一次就缩水 太失望了要退货", "draft"),
        ]:
            _comment(db, text, status)
        print("  评论草稿 3 条（1 条已发送，2 条待处理）")

        db.commit()
        print("演示数据就绪。")
    finally:
        db.close()


if __name__ == "__main__":
    main()
