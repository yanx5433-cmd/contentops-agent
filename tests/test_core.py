"""测试套件：确定性评分、合规分级、导入校验、审批与审计全链路。

评分/合规全部离线可复算，不依赖大模型（对标 dxl 的确定性 Eval 思路）。
"""
from __future__ import annotations

import io
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models import (
    AccountProfile,
    Approval,
    AuditLog,
    ProductFact,
    Topic,
    TopicScore,
    VideoFact,
)
from app.services import compliance, importer, scoring


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = TestSession()

    def override():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = override
    yield TestClient(app), db
    app.dependency_overrides.clear()
    db.close()


def seed(db, with_profile=True):
    if with_profile:
        db.add(AccountProfile(
            name="测试账号",
            audience={"female_ratio": 0.62, "age_18_24": 0.41,
                      "category_pref": {"厨房小工具": 0.38, "办公好物": 0.22, "宿舍收纳": 0.18}},
            price_bands={"0-50": 0.52, "50-150": 0.34, "150+": 0.14},
            baselines={"avg_finish_rate": 0.31, "avg_cvr": 0.023, "avg_view": 150000},
        ))
    now = datetime.now()
    for i in range(5):
        db.add(VideoFact(
            external_id=f"tv{i}", title=f"厨房好物第{i}弹" if i < 3 else f"宿舍收纳合集{i}",
            published_at=now - timedelta(days=10 + i), duration_sec=30,
            views=100000 + i * 5000, finish_rate=0.3, likes=8000, comments=500,
            shares=1000, new_followers=400, imported_batch="seed",
        ))
    db.add(ProductFact(
        external_id="tp1", title="切菜神器", category="厨房小工具",
        price=39.9, commission_rate=0.20, sales_count=5800, return_rate=0.04,
        required_licenses=[], imported_batch="seed",
    ))
    db.add(ProductFact(
        external_id="tp2", title="自热小火锅", category="食品",
        price=39.9, commission_rate=0.12, sales_count=8900, return_rate=0.15,
        required_licenses=[], imported_batch="seed",  # 故意缺资质
    ))
    db.flush()


# ---------------- 评分引擎 ----------------

class TestScoring:
    def test_content_dimensions_and_weights(self, client):
        _, db = client
        seed(db)
        topic = Topic(title="厨房好物第八弹", category="厨房小工具",
                      estimated_duration_sec=30, hook_strength=0.8)
        db.add(topic)
        db.flush()
        profile = db.query(AccountProfile).first()
        videos = list(db.query(VideoFact))
        result = scoring.score_topic(topic, profile, videos, publish_hour=19)
        assert 0 <= result.content_score <= 100
        names = [d.name for d in result.content_detail]
        assert names == ["finish_rate", "topic_heat", "competition", "audience_match", "publish_window"]
        # 每个维度都有可解释依据
        for d in result.content_detail:
            assert d.reason

    def test_product_engine_and_total(self, client):
        _, db = client
        seed(db)
        p = db.query(ProductFact).filter_by(external_id="tp1").one()
        topic = Topic(title="切菜神器测评", category="厨房小工具",
                      hook_strength=0.7, product_id=p.id)
        db.add(topic)
        db.flush()
        profile = db.query(AccountProfile).first()
        result = scoring.score_topic(topic, profile, [])
        assert result.product_score is not None
        # 总分 = 内容*0.6 + 商品*0.4
        expected = round(result.content_score * 0.6 + result.product_score * 0.4, 2)
        assert result.total_score == pytest.approx(expected, abs=0.01)

    def test_no_product_total_is_content_only(self, client):
        _, db = client
        seed(db)
        topic = Topic(title="纯内容不挂车", category="宿舍收纳", hook_strength=0.6)
        db.add(topic)
        db.flush()
        result = scoring.score_topic(topic, None, [])
        assert result.product_score is None
        assert result.total_score == pytest.approx(result.content_score * 0.6, abs=0.01)

    def test_higher_hook_higher_finish_score(self, client):
        _, db = client
        seed(db)
        profile = db.query(AccountProfile).first()
        videos = list(db.query(VideoFact))
        t1 = Topic(title="a", estimated_duration_sec=30, hook_strength=0.2)
        t2 = Topic(title="b", estimated_duration_sec=30, hook_strength=0.9)
        r1 = scoring.score_content(t1, profile, videos)
        r2 = scoring.score_content(t2, profile, videos)
        d1 = {x.name: x.score for x in r1[1]}["finish_rate"]
        d2 = {x.name: x.score for x in r2[1]}["finish_rate"]
        assert d2 > d1

    def test_longer_video_lower_finish_score(self, client):
        _, db = client
        seed(db)
        profile = db.query(AccountProfile).first()
        t1 = Topic(title="短", estimated_duration_sec=20, hook_strength=0.5)
        t2 = Topic(title="长", estimated_duration_sec=90, hook_strength=0.5)
        r1 = {x.name: x.score for x in scoring.score_content(t1, profile, [])[1]}
        r2 = {x.name: x.score for x in scoring.score_content(t2, profile, [])[1]}
        assert r1["finish_rate"] > r2["finish_rate"]

    def test_weakness_reported(self, client):
        _, db = client
        seed(db)
        p = db.query(ProductFact).filter_by(external_id="tp1").one()
        topic = Topic(title="x", category="大家电", hook_strength=0.1,
                      product_id=p.id, estimated_duration_sec=200)
        db.add(topic)
        db.flush()
        profile = db.query(AccountProfile).first()
        result = scoring.score_topic(topic, profile, [], publish_hour=3)
        assert result.weaknesses, "低分选题应输出短板提示"


# ---------------- 合规 ----------------

class TestCompliance:
    def test_p0_banned_word_blocks(self, client):
        _, db = client
        topic = Topic(title="全网最低价的神器", outline="说它是最好的")
        db.add(topic)
        db.flush()
        result = compliance.review_topic(db, topic)
        assert result.blocked
        assert topic.status == "rejected"

    def test_p1_requires_manual_review_not_block(self, client):
        _, db = client
        topic = Topic(title="这款减肥神器真的绝", outline="日常开箱")
        db.add(topic)
        db.flush()
        result = compliance.review_topic(db, topic)
        assert not result.blocked
        assert result.need_manual_review
        assert "P1" in result.levels

    def test_p2_only_hint(self, client):
        _, db = client
        topic = Topic(title="宿舍必备收纳盒", outline="闭眼入")
        db.add(topic)
        db.flush()
        result = compliance.review_topic(db, topic)
        assert not result.blocked and not result.need_manual_review

    def test_license_missing_blocks(self, client):
        _, db = client
        seed(db)
        p = db.query(ProductFact).filter_by(external_id="tp2").one()  # 食品缺资质
        topic = Topic(title="自热火锅试吃", product_id=p.id)
        db.add(topic)
        db.flush()
        result = compliance.review_topic(db, topic)
        assert result.blocked
        kinds = [h.kind for h in result.hits]
        assert "license_missing" in kinds

    def test_clean_topic_passes(self, client):
        _, db = client
        topic = Topic(title="好用的切菜板开箱", outline="实测展示")
        db.add(topic)
        db.flush()
        result = compliance.review_topic(db, topic)
        assert not result.blocked and not result.hits


# ---------------- 导入 ----------------

class TestImporter:
    def test_video_import_and_dedup(self, client):
        _, db = client
        csv_text = ("external_id,title,views,finish_rate,likes,comments,shares,new_followers\n"
                    "v1,第一个视频,10000,0.3,500,50,80,20\n"
                    "v1,重复视频,10000,0.3,500,50,80,20\n")
        r1 = importer.import_videos(db, csv_text.encode(), "b1")
        # 首次导入：1 条新建，同批第 2 次出现计为 duplicate
        assert r1["created"] == 1 and r1["skipped"] == 0 and r1["duplicate"] == 1
        r2 = importer.import_videos(db, csv_text.encode(), "b2")
        # 二次导入：库里已有，两条都计 skipped；事实快照只增不改
        assert r2["created"] == 0 and r2["skipped"] == 2
        assert db.query(VideoFact).count() == 1

    def test_video_import_rejects_bad_rows(self, client):
        _, db = client
        csv_text = ("external_id,title,views,finish_rate\n"
                   "v1,正常,1000,0.3\n"
                   ",空ID,1000,0.3\n"
                   "v2,完播率越界,1000,1.5\n")
        with pytest.raises(importer.ImportRejected) as e:
            importer.import_videos(db, csv_text.encode(), "bad")
        assert len(e.value.errors) == 2
        assert db.query(VideoFact).count() == 0  # 整批拒绝，无半批导入

    def test_video_import_missing_column(self, client):
        _, db = client
        csv_text = "external_id,title\nv1,缺列\n"
        with pytest.raises(importer.ImportRejected):
            importer.import_videos(db, csv_text.encode(), "bad")

    def test_product_import_with_bom(self, client):
        _, db = client
        csv_bytes = ("external_id,title,category,price,commission_rate,sales_count,return_rate,required_licenses\n"
                     "bp1,密封罐,厨房小工具,35.0,0.16,6400,0.05,食品经营许可证|其他\n").encode("utf-8-sig")
        r = importer.import_products(db, csv_bytes, "b1")
        assert r["created"] == 1
        p = db.query(ProductFact).filter_by(external_id="bp1").one()
        assert p.required_licenses == ["食品经营许可证", "其他"]

    def test_import_writes_audit(self, client):
        _, db = client
        csv_text = "external_id,title,views,finish_rate\nv1,xx,1000,0.3\n"
        importer.import_videos(db, csv_text.encode(), "b1")
        logs = db.query(AuditLog).filter_by(action="import").all()
        assert len(logs) == 1 and logs[0].detail["created"] == 1


# ---------------- API 全链路 ----------------

class TestAPI:
    def test_full_pipeline(self, client):
        c, db = client
        seed(db)
        # 1. 建选题（挂厨房小工具商品）
        r = c.post("/api/topics", json={
            "title": "厨房好物第八弹：切菜神器实测", "outline": "30秒钩子+实测+价格对比",
            "category": "厨房小工具", "estimated_duration_sec": 30,
            "hook_strength": 0.8, "product_external_id": "tp1",
        })
        assert r.status_code == 201
        tid = r.json()["id"]
        # 2. 评分
        r = c.post(f"/api/topics/{tid}/score?publish_hour=19")
        assert r.status_code == 200
        body = r.json()
        assert body["total_score"] > 0 and body["product_score"] > 0
        assert len(body["detail"]["content"]) == 5 and len(body["detail"]["product"]) == 4
        # 3. 合规
        r = c.post(f"/api/topics/{tid}/review")
        assert r.status_code == 200 and not r.json()["blocked"]
        # 4. 审批
        r = c.post(f"/api/topics/{tid}/approve", json={"decision": "approved"})
        assert r.status_code == 200
        # 5. 状态与留痕
        r = c.get(f"/api/topics/{tid}")
        assert r.json()["status"] == "approved"
        assert r.json()["latest_score"]["total_score"] == body["total_score"]
        actions = [a.action for a in db.query(AuditLog).all()]
        assert "score" in actions and "compliance_review" in actions and "approved" in actions

    def test_p0_topic_cannot_be_approved(self, client):
        c, db = client
        seed(db)
        r = c.post("/api/topics", json={"title": "全网最低的神器", "hook_strength": 0.5})
        tid = r.json()["id"]
        c.post(f"/api/topics/{tid}/score")
        r = c.post(f"/api/topics/{tid}/review")
        assert r.json()["blocked"]
        r = c.post(f"/api/topics/{tid}/approve", json={"decision": "approved"})
        assert r.status_code == 409  # 被拦截的选题不可审批通过

    def test_unscored_topic_cannot_approve(self, client):
        c, _ = client
        r = c.post("/api/topics", json={"title": "未评分选题"})
        tid = r.json()["id"]
        r = c.post(f"/api/topics/{tid}/approve", json={"decision": "approved"})
        assert r.status_code == 409

    def test_score_versioning(self, client):
        c, db = client
        seed(db)
        tid = c.post("/api/topics", json={"title": "版本测试"}).json()["id"]
        v1 = c.post(f"/api/topics/{tid}/score").json()["version"]
        v2 = c.post(f"/api/topics/{tid}/score?publish_hour=19").json()["version"]
        assert v2 == v1 + 1
        assert db.query(TopicScore).filter_by(topic_id=tid).count() == 2

    def test_api_import_endpoint(self, client):
        c, _ = client
        csv_bytes = open("data/raw/videos_sample.csv", "rb").read()
        r = c.post("/api/import/videos", files={"file": ("videos.csv", csv_bytes, "text/csv")})
        assert r.status_code == 200
        assert r.json()["created"] > 0
        r = c.post("/api/import/products", files={"file": ("products.csv", open("data/raw/products_sample.csv", "rb").read(), "text/csv")})
        assert r.status_code == 200

    def test_audit_endpoint(self, client):
        c, _ = client
        r = c.get("/api/audit?limit=10")
        assert r.status_code == 200 and isinstance(r.json(), list)

    def test_health(self, client):
        c, _ = client
        assert c.get("/health").json()["status"] == "ok"
