"""爆款拆解 RAG：知识库加载、分块、BM25 检索、来源标注。

语料来源（data/knowledge/*.md，人工维护）：
- 自己账号的爆款视频拆解（钩子/节奏/转化点结构）
- 垂类头部账号的公开爆款结构
- 平台规则与带货话术规范

检索为纯标准库 BM25 实现：女装垂类语料规模小（几百块以内），
无需向量库依赖；每个结果带来源文件与行号，符合"可解释可追溯"。
LLM 可用时对检索结果做组织生成，不可用时直接返回片段。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

from app import config

KB_DIR = config.DATA_DIR / "knowledge"


@dataclass
class Chunk:
    doc: str          # 来源文件名
    title: str        # 文档内小节标题（# 行）
    text: str
    lines: tuple[int, int]  # 该块在原文件的行号范围（1-based，含头尾）


@dataclass
class RetrievalHit:
    chunk: Chunk
    score: float


def _iter_docs() -> list[tuple[str, str]]:
    if not KB_DIR.exists():
        return []
    out = []
    for f in sorted(KB_DIR.glob("*.md")):
        out.append((f.name, f.read_text(encoding="utf-8")))
    return out


def _split_chunks(doc_name: str, content: str) -> list[Chunk]:
    """按 # 至 #### 标题分块；无标题的整文件一块。行号 1-based。"""
    lines = content.splitlines()
    sections: list[tuple[str, list[tuple[int, str]]]] = []
    current_title, buf = "", []
    for i, line in enumerate(lines, start=1):
        m = re.match(r"^(#{1,4})\s+(.+)$", line)
        if m:
            if buf:
                sections.append((current_title or "（无标题）", buf))
            current_title, buf = f"{m.group(2).strip()}", [(i, line)]
        else:
            buf.append((i, line))
    if buf:
        sections.append((current_title or "（无标题）", buf))

    chunks = []
    for title, sec in sections:
        # 过长的节按行数二次切分（约 30 行一块）
        for j in range(0, len(sec), 30):
            piece = sec[j:j + 30]
            text = "\n".join(l for _, l in piece).strip()
            if text:
                chunks.append(Chunk(doc=doc_name, title=title, text=text,
                                    lines=(piece[0][0], piece[-1][0])))
    return chunks


def _tokenize(text: str) -> list[str]:
    """中文按 2-gram + 英文/数字按词，过滤空白与标点。"""
    text = re.sub(r"[^\w\u4e00-\u9fff]+", " ", text)
    tokens: list[str] = []
    for part in text.split():
        if re.fullmatch(r"[A-Za-z0-9_]+", part):
            tokens.append(part.lower())
        else:
            tokens.extend(part[i:i + 2] for i in range(len(part) - 1))
    return tokens


class BM25Index:
    def __init__(self, chunks: list[Chunk]):
        self.chunks = chunks
        self.doc_tokens = [_tokenize(c.text) for c in chunks]
        self.avgdl = sum(len(t) for t in self.doc_tokens) / len(self.doc_tokens) if self.doc_tokens else 1
        self.df: dict[str, int] = {}
        for tokens in self.doc_tokens:
            for tok in set(tokens):
                self.df[tok] = self.df.get(tok, 0) + 1
        self.N = len(chunks)

    def _idf(self, tok: str) -> float:
        return math.log(1 + (self.N - self.df.get(tok, 0) + 0.5) / (self.df.get(tok, 0) + 0.5))

    def search(self, query: str, top_k: int = 5, min_score: float = 0.05) -> list[RetrievalHit]:
        q_tokens = _tokenize(query)
        scores: list[float] = []
        k1, b = 1.5, 0.75
        for tokens in self.doc_tokens:
            tf: dict[str, int] = {}
            for t in tokens:
                tf[t] = tf.get(t, 0) + 1
            s = 0.0
            for qt in set(q_tokens):
                if qt in tf:
                    s += self._idf(qt) * tf[qt] * (k1 + 1) / (tf[qt] + k1 * (1 - b + b * len(tokens) / self.avgdl))
            scores.append(s)
        ranked = sorted(zip(self.chunks, scores), key=lambda x: x[1], reverse=True)
        return [RetrievalHit(chunk=c, score=s) for c, s in ranked[:top_k] if s > min_score]


_index: BM25Index | None = None


def get_index() -> BM25Index:
    global _index
    if _index is None:
        chunks: list[Chunk] = []
        for name, content in _iter_docs():
            chunks.extend(_split_chunks(name, content))
        _index = BM25Index(chunks)
    return _index


def reload_index() -> int:
    """知识库文件更新后重载，返回块数。"""
    global _index
    chunks: list[Chunk] = []
    for name, content in _iter_docs():
        chunks.extend(_split_chunks(name, content))
    _index = BM25Index(chunks)
    return len(chunks)


def query_kb(query: str, top_k: int = 5) -> dict:
    """检索 + 组织答案。LLM 可用则组织生成，否则返回命中的原文片段（带来源）。"""
    idx = get_index()
    hits = idx.search(query, top_k=top_k)
    if not hits:
        return {"answer": "知识库中没有相关内容。", "source": "empty",
                "hits": [], "llm_used": False}

    citations = [
        {"doc": h.chunk.doc, "title": h.chunk.title,
         "lines": list(h.chunk.lines), "score": round(h.score, 3),
         "excerpt": h.chunk.text[:200]}
        for h in hits
    ]
    joined = "\n\n".join(f"[{h.chunk.doc}#{h.chunk.title}] {h.chunk.text}" for h in hits)

    from app.services import llm
    if llm.llm_available():
        system = ("你是女装穿搭带货账号的内容运营助手。基于下面检索到的爆款拆解知识回答问题，"
                  "只使用给出的材料，不编造；材料不足就明说。回答末尾列出引用来源。")
        result = llm.chat(f"问题：{query}\n\n检索材料：\n{joined}", system=system)
        if result:
            return {"answer": result.text, "source": result.source,
                    "hits": citations, "llm_used": True}

    # 模板降级：直接给命中片段，人工自行阅读
    return {"answer": f"（未配置 LLM，返回知识库原文片段）\n\n{joined}",
            "source": "template", "hits": citations, "llm_used": False}
