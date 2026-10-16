"""第 3 周模块测试：文案体检 / 选题风暴 / 看板 / 复盘 / 校准 / 发布挂接 / LLM 配置。

全部离线运行：LLM 未配置走模板降级；预算测试用 monkeypatch 的假 HTTP 接口。
"""
from __future__ import annotations

import io
from datetime import datetime, timedelta

import httpx
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
    SalesFact,
    Topic,
    TopicScore,
    VideoFact,
)
from app.services import brainstorm, compliance, llm, scoring


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


@pytest.fixture(autouse=True)
def clean_llm(monkeypatch):
    """每个测试用干净的 LLM 运行时状态（默认未配置；计数不落盘）。"""
    monkeypatch.setattr(llm, "_runtime", {"api_key": "", "base_url": "https://api.example/v1",
                                          "model": "test-model", "daily_cap": 200})
    monkeypatch.setattr(llm, "_counter", {"date": "", "used": 0})
    monkeypatch.setattr(llm, "_session_factory", None)
    yield


def seed(db):
    """最小演示基座：画像 + 3 条视频 + 2 个商品。"""
    db.add(AccountProfile(
        name="测试账号",
        audience={"female_ratio": 0.7, "category_pref": {"女装": 0.45}},
        price_bands={"0-50": 0.5, "50-150": 0.35, "150+": 0.15},
        baselines={"avg_finish_rate": 0.25, "avg_cvr": 0.02},
    ))
    now = datetime.now()
    for i, (ext, title, views, fr) in enumerate([
        ("tv1", "三件打底裤对比测评", 143000, 0.31),
        ("tv2", "小个子显高穿搭公式", 167000, 0.34),
        ("tv3", "换季衣橱整理清单", 89000, 0.26),
    ]):
        db.add(VideoFact(external_id=ext, title=title, published_at=now - timedelta(days=5 + i),
                         duration_sec=30, views=views, finish_rate=fr, likes=7000, comments=600,
                         shares=1300, new_followers=400, imported_batch="seed"))
    db.add(ProductFact(external_id="tp1", title="加绒打底裤", category="女装", price=59.9,
                       commission_rate=0.22, sales_count=8600, return_rate=0.06,
                       required_licenses=[], imported_batch="seed"))
    db.add(ProductFact(external_id="tp2", title="无资质保健食品", category="保健食品", price=69.0,
                       commission_rate=0.28, sales_count=5800, return_rate=0.22,
                       required_licenses=[], imported_batch="seed"))
    db.flush()


def make_published_topic(db, title="打底裤实测", duration=25, hook=0.70, hour=19,
                         video="tv1", status="published"):
    """已通过（+可选已发布）的选题，含评分快照。"""
    topic = Topic(title=title, category="女装", estimated_duration_sec=duration, hook_strength=hook)
    db.add(topic)
    db.flush()
    profile = db.get(AccountProfile, 1)
    videos = list(db.query(VideoFact).all())
    result = scoring.score_topic(topic, profile, videos, hour)
    db.add(TopicScore(
        topic_id=topic.id, version=1,
        content_score=result.content_score,
        content_detail=[{"name": d.name, "score": d.score, "reason": d.reason} for d in result.content_detail],
        product_score=None, product_detail=None,
        total_score=result.total_score, weaknesses=result.weaknesses,
    ))
    if status == "published":
        topic.status = "published"
        topic.published_video_external_id = video
    else:
        topic.status = status
    db.add(Approval(topic_id=topic.id, decision="approved", reviewer="tester"))
    db.flush()
    return topic


# ---------------- 文案体检 ----------------

class TestMedic:
    def test_spans_positions_and_hints(self):
        spans = compliance.check_text_spans("这个全网最低价真的最好")
        words = [s["word"] for s in spans]
        assert "全网最低" in words and "最好" in words
        for s in spans:
            assert s["start"] < s["end"]
            assert s["hint"], f"{s['word']} 应有改写建议"
            assert s["level"] == "P0"

    def test_spans_multiple_occurrences(self):
        spans = compliance.check_text_spans("最好最好")
        assert len([s for s in spans if s["word"] == "最好"]) == 2
        positions = sorted(s["start"] for s in spans if s["word"] == "最好")
        assert positions == [0, 2]

    def test_api_verdict_block_and_clean(self, client):
        c, _ = client
        r = c.post("/api/compliance/check-text", json={"text": "全网最低，闭眼入"})
        assert r.status_code == 200
        data = r.json()
        assert data["verdict"] == "block"
        assert data["counts"]["P0"] >= 1 and data["counts"]["P2"] >= 1

        r2 = c.post("/api/compliance/check-text", json={"text": "秋冬连衣裙三套实测，评论区报身高"})
        assert r2.json()["verdict"] == "clean"
        assert r2.json()["hits"] == []

    def test_api_requires_text(self, client):
        c, _ = client
        assert c.post("/api/compliance/check-text", json={"text": ""}).status_code == 422


# ---------------- AI 选题风暴 ----------------

class TestBrainstorm:
    def test_template_mode_offline(self, client):
        c, db = client
        seed(db)
        r = c.post("/api/brainstorm", json={"category": "女装",
                                            "keywords": ["秋冬连衣裙"], "count": 8})
        assert r.status_code == 200
        data = r.json()
        assert data["source"] == "template"
        assert len(data["candidates"]) >= 3
        scores = [x["content_score"] for x in data["candidates"]]
        assert scores == sorted(scores, reverse=True)
        assert all("秋冬连衣裙" in x["title"] for x in data["candidates"])

    def test_p0_candidates_filtered(self, client, monkeypatch):
        c, db = client
        seed(db)
        monkeypatch.setattr(brainstorm, "_llm_titles", lambda *a, **k: None)
        monkeypatch.setattr(brainstorm, "_template_titles",
                            lambda *a, **k: ["全网最低价的秋冬连衣裙", "小个子秋冬连衣裙实测"])
        r = c.post("/api/brainstorm", json={"category": "女装", "count": 5})
        titles = [x["title"] for x in r.json()["candidates"]]
        assert "全网最低价的秋冬连衣裙" not in titles
        assert "小个子秋冬连衣裙实测" in titles

    def test_candidates_carry_scores(self, client):
        c, db = client
        seed(db)
        r = c.post("/api/brainstorm", json={"category": "女装", "count": 3})
        for cand in r.json()["candidates"]:
            assert 0 <= cand["content_score"] <= 100
            assert isinstance(cand["weaknesses"], list)


# ---------------- 看板 ----------------

class TestDashboard:
    def test_dashboard_keys(self, client):
        c, db = client
        seed(db)
        r = c.get("/api/dashboard")
        assert r.status_code == 200
        data = r.json()
        for key in ("funnel", "score_buckets", "compliance_levels", "trend",
                    "topic_categories", "totals"):
            assert key in data
        assert data["totals"]["videos"] == 3

    def test_funnel_counts(self, client):
        c, db = client
        seed(db)
        make_published_topic(db, title="a")
        db.add(Topic(title="b", category="女装", status="scored"))
        db.flush()
        data = c.get("/api/dashboard").json()
        funnel = {f["status"]: f["count"] for f in data["funnel"]}
        assert funnel["published"] == 1
        assert funnel["scored"] == 1


# ---------------- 销售导入 ----------------

class TestSalesImport:
    CSV_OK = ("video_external_id,product_external_id,stat_date,sales_count,gmv,return_count,return_amount\n"
              "tv1,tp1,2026-09-05,96,5750.4,5,299.5\n"
              "tv9,tp1,2026-09-06,10,599.0,0,0\n").encode("utf-8-sig")

    def test_import_valid_and_unmatched(self, client):
        c, db = client
        seed(db)
        r = c.post("/api/import/sales", files={"file": ("sales.csv", self.CSV_OK, "text/csv")})
        assert r.status_code == 200
        data = r.json()
        assert data["created"] == 2
        assert data["unmatched"] == ["tv9"]  # 视频不存在只提示，不整批拒绝
        assert db.query(SalesFact).count() == 2

    def test_import_rejects_bad_rows(self, client):
        c, db = client
        seed(db)
        bad = ("video_external_id,product_external_id,sales_count,return_count\n"
               "tv1,tp1,10,20\n").encode("utf-8-sig")  # 退货数大于销量
        r = c.post("/api/import/sales", files={"file": ("sales.csv", bad, "text/csv")})
        assert r.status_code == 422
        assert db.query(SalesFact).count() == 0  # 整批拒绝

    def test_import_dedupe(self, client):
        c, db = client
        seed(db)
        c.post("/api/import/sales", files={"file": ("sales.csv", self.CSV_OK, "text/csv")})
        r2 = c.post("/api/import/sales", files={"file": ("sales.csv", self.CSV_OK, "text/csv")})
        assert r2.json()["created"] == 0
        assert r2.json()["skipped"] == 2
        assert db.query(SalesFact).count() == 2


# ---------------- 发布挂接 ----------------

class TestPublish:
    def _approved_topic(self, db):
        topic = Topic(title="待发布选题", category="女装", status="approved")
        db.add(topic)
        db.flush()
        return topic

    def test_publish_ok(self, client):
        c, db = client
        seed(db)
        topic = self._approved_topic(db)
        r = c.post(f"/api/topics/{topic.id}/publish", json={"video_external_id": "tv1"})
        assert r.status_code == 200
        assert r.json()["status"] == "published"
        assert db.get(Topic, topic.id).published_video_external_id == "tv1"
        assert db.query(AuditLog).filter_by(action="publish").count() == 1

    def test_publish_unknown_video_404(self, client):
        c, db = client
        seed(db)
        topic = self._approved_topic(db)
        r = c.post(f"/api/topics/{topic.id}/publish", json={"video_external_id": "nope"})
        assert r.status_code == 404

    def test_publish_requires_approved(self, client):
        c, db = client
        seed(db)
        topic = Topic(title="未审批", category="女装", status="scored")
        db.add(topic)
        db.flush()
        r = c.post(f"/api/topics/{topic.id}/publish", json={"video_external_id": "tv1"})
        assert r.status_code == 409

    def test_republish_same_video_409(self, client):
        c, db = client
        seed(db)
        topic = self._approved_topic(db)
        c.post(f"/api/topics/{topic.id}/publish", json={"video_external_id": "tv1"})
        r = c.post(f"/api/topics/{topic.id}/publish", json={"video_external_id": "tv1"})
        assert r.status_code == 409


# ---------------- 预测校准 ----------------

class TestCalibration:
    def test_calibration_math(self, client):
        c, db = client
        seed(db)
        make_published_topic(db, duration=25, hook=0.70, video="tv1")
        data = c.get("/api/review/calibration").json()
        assert data["n"] == 1
        row = data["rows"][0]
        # 25s + 钩子0.70 + 基线25%：0.25 × 1.0167 × 1.12 ≈ 0.2847
        assert abs(row["predicted"] - 0.2847) < 0.001
        assert abs(row["actual"] - 0.31) < 1e-6
        assert abs(row["deviation"] - (0.31 - 0.2847)) < 0.001

    def test_calibration_empty_guides_user(self, client):
        c, db = client
        seed(db)
        data = c.get("/api/review/calibration").json()
        assert data["n"] == 0
        assert "标记已发布" in data["conclusion"]


# ---------------- 复盘报告 ----------------

class TestReview:
    def _sales(self, db):
        for i, (sales, gmv, rc, ra) in enumerate([(96, 5750.4, 5, 299.5), (142, 8505.8, 8, 479.2)]):
            db.add(SalesFact(video_external_id="tv1", product_external_id="tp1",
                             stat_date=f"2026-09-{5 + i:02d}", sales_count=sales, gmv=gmv,
                             return_count=rc, return_amount=ra, imported_batch="seed"))
        db.flush()

    def test_conversion_cvr(self, client):
        c, db = client
        seed(db)
        self._sales(db)
        rows = c.get("/api/review/conversion?days=365").json()["rows"]
        assert len(rows) == 1
        row = rows[0]
        assert row["sales_count"] == 238
        assert abs(row["gmv"] - 14256.2) < 0.01
        assert abs(row["cvr"] - 238 / 143000) < 1e-6
        assert abs(row["commission"] - 14256.2 * 0.22) < 0.01

    def test_report_totals_and_markdown(self, client):
        c, db = client
        seed(db)
        self._sales(db)
        rep = c.get("/api/review/report?days=365").json()
        assert rep["totals"]["sales"] == 238
        assert rep["totals"]["returns"] == 13
        assert "权重修正建议" in rep["markdown"]
        assert rep["suggestions"], "报告应始终带一条建议或样本提示"

    def test_small_sample_muted(self, client):
        c, db = client
        seed(db)
        self._sales(db)
        make_published_topic(db)  # 仅 1 条可对比（<4）
        rep = c.get("/api/review/report?days=365").json()
        assert any("样本" in s["dimension"] or "样本" in s["evidence"] for s in rep["suggestions"])


# ---------------- LLM 配置 ----------------

class TestLLMSettings:
    def test_settings_mask(self, client):
        c, _ = client
        r = c.put("/api/settings/llm", json={"api_key": "sk-abcdefgh12345678",
                                             "model": "deepseek-chat", "daily_cap": 50})
        assert r.status_code == 200
        assert r.json()["configured"] is True
        assert r.json()["daily_cap"] == 50
        got = c.get("/api/settings/llm").json()
        assert got["api_key_masked"] != "sk-abcdefgh12345678"
        assert "…" in got["api_key_masked"]

    def test_settings_audit_has_no_key(self, client):
        c, db = client
        c.put("/api/settings/llm", json={"api_key": "sk-secret-1234567890"})
        logs = db.query(AuditLog).filter_by(action="llm_config").all()
        assert len(logs) == 1
        assert "llm_api_key" in logs[0].detail["keys"]
        assert "sk-secret" not in str(logs[0].detail)

    def test_test_endpoint_unconfigured_400(self, client):
        c, _ = client
        assert c.post("/api/settings/llm/test").status_code == 400


# ---------------- LLM 预算 ----------------

class TestLLMBudget:
    def _fake_post(self, content="ok"):
        def fake_post(*args, **kwargs):
            return httpx.Response(
                200, json={"choices": [{"message": {"content": content}}]},
                request=httpx.Request("POST", "https://api.example/v1/chat/completions"),
            )
        return fake_post

    def test_budget_cap_blocks(self, monkeypatch):
        monkeypatch.setattr(llm, "_runtime", {"api_key": "sk-x", "base_url": "https://api.example/v1",
                                              "model": "m", "daily_cap": 2})
        monkeypatch.setattr(llm.httpx, "post", self._fake_post())
        assert llm.llm_available() is True
        assert llm.chat("1") is not None
        assert llm.chat("2") is not None
        assert llm.llm_available() is False
        assert llm.chat("3") is None  # 超限自动停用

    def test_zero_cap_disables(self, monkeypatch):
        monkeypatch.setattr(llm, "_runtime", {"api_key": "sk-x", "base_url": "https://api.example/v1",
                                              "model": "m", "daily_cap": 0})
        assert llm.llm_available() is False
        assert llm.chat("hi") is None

    def test_status_reports_usage(self, monkeypatch):
        monkeypatch.setattr(llm, "_runtime", {"api_key": "sk-1234567890abcd",
                                              "base_url": "https://api.example/v1",
                                              "model": "m", "daily_cap": 10})
        monkeypatch.setattr(llm.httpx, "post", self._fake_post())
        llm.chat("hi")
        st = llm.status()
        assert st["used_today"] == 1
        assert st["available"] is True
        assert st["api_key_masked"].startswith("sk-1")
