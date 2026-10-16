"""第 2 周模块测试：RAG 知识库 + 评论助手 + LLM 降级。

全部离线运行（不配 LLM_API_KEY，LLM 走模板降级路径）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.services import comment_assistant, rag


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
def fresh_rag_index(monkeypatch):
    """每个测试用真实种子知识库重建索引（data/knowledge/）。"""
    monkeypatch.setattr(rag, "_index", None)
    yield
    monkeypatch.setattr(rag, "_index", None)


# ---------------- RAG ----------------

class TestRAG:
    def test_kb_chunks_built(self):
        idx = rag.get_index()
        assert len(idx.chunks) >= 5, "种子知识库应至少切出 5 块"
        # 每块都带来源与行号
        for c in idx.chunks:
            assert c.doc.endswith(".md") and c.lines[0] >= 1 and c.lines[1] >= c.lines[0]

    def test_query_returns_citations(self):
        result = rag.query_kb("小个子女生怎么选穿搭选题")
        assert result["hits"], "应命中选题模式库/爆款结构文档"
        for h in result["hits"]:
            assert h["doc"] and h["lines"] and h["score"] > 0

    def test_query_no_hit(self):
        result = rag.query_kb("量子力学考研重点")
        assert result["source"] == "empty"
        assert result["hits"] == []

    def test_no_llm_falls_back_to_excerpts(self):
        # 测试环境无 LLM_API_KEY，应走模板降级返回原文片段
        result = rag.query_kb("极限词有哪些")
        assert result["llm_used"] is False
        assert result["source"] == "template"
        assert "极限词" in result["answer"] or "最好" in result["answer"]

    def test_reload_counts_chunks(self):
        n = rag.reload_index()
        assert n >= 5

    def test_kb_query_api(self, client):
        c, _ = client
        r = c.post("/api/kb/query", json={"query": "退货差评怎么回复", "top_k": 3})
        assert r.status_code == 200
        body = r.json()
        assert body["hits"] and body["hits"][0]["doc"] == "带货合规要点.md"

    def test_kb_reload_api_writes_audit(self, client):
        c, db = client
        r = c.post("/api/kb/reload")
        assert r.status_code == 200 and r.json()["chunks"] >= 5
        from app.models import AuditLog
        assert db.query(AuditLog).filter_by(action="kb_reload").count() == 1


# ---------------- 评论助手 ----------------

class TestCommentAssistant:
    @pytest.mark.parametrize("comment,intent", [
        ("求同款链接！！爱了爱了", "purchase"),
        ("这件多少钱呀 怎么买", "purchase"),
        ("160 100斤穿什么码 会起球吗", "product_qa"),
        ("会不会显黑 显瘦吗", "product_qa"),
        ("太差了 洗一次就缩水 差评", "complaint"),
        ("色差也太大了 要求退款", "complaint"),
        ("博主您好 我们品牌想找您做寄拍合作", "cooperation"),
        ("星图报价多少", "cooperation"),
        ("哈哈哈哈笑死我了", "other"),
    ])
    def test_intent_classification(self, comment, intent):
        got, matched = comment_assistant.classify_intent(comment)
        assert got == intent, f"'{comment}' 应为 {intent}，实得 {got}（命中 {matched}）"

    def test_draft_contains_no_price_promises(self):
        """话术不得承诺具体价格/库存/补偿（防止评论区留下承诺证据）。"""
        for comment in ("求链接", "160穿什么码", "质量太差了要退款", "品牌方合作"):
            result = comment_assistant.generate_draft(comment)
            for banned in ("元", "包邮", "现货", "打折", "半价", "补偿"):
                assert banned not in result.draft, f"{result.intent} 话术出现违规承诺词: {banned}"

    def test_complaint_draft_guides_private_channel(self):
        result = comment_assistant.generate_draft("起球了 太失望 要退货")
        assert result.intent == "complaint"
        assert "私信" in result.draft, "差评草稿必须引导到私信处理"

    def test_draft_api_persists_and_audits(self, client):
        c, db = client
        r = c.post("/api/comments/draft", json={
            "comment_text": "160 95斤穿什么码呀",
            "video_external_id": "V20260930",
        })
        assert r.status_code == 201
        body = r.json()
        assert body["intent"] == "product_qa"
        assert body["intent_label"] == "商品咨询"
        assert body["status"] == "draft"
        from app.models import AuditLog, CommentDraft
        assert db.query(CommentDraft).count() == 1
        assert db.query(AuditLog).filter_by(action="comment_draft").count() == 1

    def test_draft_finalize_flow(self, client):
        c, db = client
        draft_id = c.post("/api/comments/draft", json={"comment_text": "求链接"}).json()["id"]
        r = c.post(f"/api/comments/drafts/{draft_id}/finalize", json={
            "final_text": "谢谢宝子～橱窗可以看到同款哦", "status": "sent",
        })
        assert r.status_code == 200 and r.json()["status"] == "sent"
        from app.models import CommentDraft
        row = db.get(CommentDraft, draft_id)
        assert row.final_text == "谢谢宝子～橱窗可以看到同款哦"

    def test_draft_list_filter(self, client):
        c, _ = client
        c.post("/api/comments/draft", json={"comment_text": "求链接"})
        c.post("/api/comments/draft", json={"comment_text": "哈哈哈"})
        r = c.get("/api/comments/drafts?limit=10")
        assert len(r.json()) == 2
        r = c.get("/api/comments/drafts?status=sent")
        assert r.json() == []

    def test_finalize_404(self, client):
        c, _ = client
        r = c.post("/api/comments/drafts/999/finalize", json={"final_text": "x", "status": "sent"})
        assert r.status_code == 404
