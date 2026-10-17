/* =====================================================================
 * ContentOps 静态在线演示 · 浏览器内引擎
 * ---------------------------------------------------------------------
 * 这不是"截图轮播"：评分引擎、合规检查、知识库 BM25 检索、评论意图
 * 分类、AI 选题风暴全部是用 JS 移植自后端的真实逻辑，在浏览器里现算；
 * 数据来自 docs/demo-data.js（导出时点的真实后端快照），刷新即重置。
 * 完整后端版（FastAPI + SQLite）：https://github.com/yanx5433-cmd/contentops-agent
 * ===================================================================== */
(() => {
  "use strict";
  const DATA = window.DEMO_DATA;
  const realFetch = window.fetch.bind(window);

  /* ---------------- 通用工具 ---------------- */
  const clamp = (v, lo = 0, hi = 100) => Math.max(lo, Math.min(hi, v));
  const round1 = (v) => Math.round(v * 10) / 10;
  const round2 = (v) => Math.round(v * 100) / 100;
  const pct0 = (v) => Math.round(v * 100) + "%";
  const pct1 = (v) => (v * 100).toFixed(1) + "%";
  const nowIso = () => {
    const d = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  };
  const byProductId = (pid) => Object.values(DATA.products).find((p) => p.id === pid) || null;
  const byProductExternal = (ext) => DATA.products[ext] || null;

  /* ---------------- 双引擎评分（移植自 app/services/scoring.py） ---------------- */
  const CONTENT_WEIGHTS = { finish_rate: 0.30, topic_heat: 0.20, competition: 0.15, audience_match: 0.20, publish_window: 0.15 };
  const PRODUCT_WEIGHTS = { commission: 0.30, conversion_history: 0.25, price_fit: 0.25, return_risk: 0.20 };
  const WEAK = 60;

  function weighted(dims, weights) {
    let total = 0, wsum = 0;
    for (const d of dims) { total += weights[d.name] * d.score; wsum += weights[d.name]; }
    return wsum ? round2(total / wsum) : 0;
  }

  function finishRateDim(topic, baselines) {
    const base = baselines.avg_finish_rate ?? 0.25;
    const dur = Math.max(topic.estimated_duration_sec || 1, 1);
    const durFactor = dur <= 30 ? 1 + ((30 - dur) / 30) * 0.10 : Math.max(0.55, 1 - ((dur - 30) / 10) * 0.08);
    const hookFactor = 0.7 + 0.6 * (topic.hook_strength ?? 0.5);
    const est = base * durFactor * hookFactor;
    const score = clamp((est / 0.5) * 100);
    return {
      name: "finish_rate", score: round1(score),
      reason: `基线${pct0(base)}×时长因子${durFactor.toFixed(2)}×钩子因子${hookFactor.toFixed(2)}≈预估完播${pct0(Math.min(est, 1) * 1)}`,
    };
  }

  function topicHeatDim() {
    const interactions = DATA.videos.map((v) => (v.likes || 0) + (v.comments || 0) + (v.shares || 0)).sort((a, b) => a - b);
    if (!interactions.length) return { name: "topic_heat", score: 50, reason: "无同类目历史数据，给中性分 50" };
    const median = interactions[Math.floor(interactions.length / 2)];
    const overall = interactions[Math.floor(interactions.length / 2)] || 1;
    const ratio = median / overall;
    return { name: "topic_heat", score: round1(clamp(ratio * 60)), reason: `同类目互动中位数为全账号的${ratio.toFixed(2)}倍` };
  }

  function competitionDim(topic) {
    const key = topic.category || (topic.title || "").slice(0, 6);
    const dup = key ? DATA.videos.filter((v) => (v.title || "").includes(key)).length : 0;
    const score = clamp(80 - dup * 15);
    const label = dup === 0 ? "蓝海" : dup <= 2 ? "轻度同质" : "饱和";
    return { name: "competition", score: round1(score), reason: `近90天同关键词视频 ${dup} 条（${label}）` };
  }

  function audienceDim(topic) {
    const pref = (DATA.profile.category_pref || {})[topic.category];
    const ratio = pref == null ? 0.20 : pref;
    return { name: "audience_match", score: round1(clamp(ratio * 250)), reason: `粉丝对[${topic.category || "未分类"}]偏好占比 ${Math.round(ratio * 100)}%` };
  }

  function publishWindowDim(hour) {
    if (hour == null) return { name: "publish_window", score: 70, reason: "未指定发布时段，按默认 70" };
    let factor = 0.75, name = "冷门时段";
    if (hour >= 7 && hour < 9) { factor = 0.80; name = "早高峰"; }
    else if (hour >= 12 && hour < 14) { factor = 0.90; name = "午间"; }
    else if (hour >= 18 && hour < 20) { factor = 1.00; name = "晚黄金档"; }
    else if (hour >= 20 && hour < 23) { factor = 0.95; name = "晚间"; }
    return { name: "publish_window", score: round1(factor * 100), reason: `计划发布于${name}` };
  }

  function scoreContent(topic, hour) {
    const baselines = DATA.profile.baselines || {};
    const dims = [finishRateDim(topic, baselines), topicHeatDim(), competitionDim(topic), audienceDim(topic), publishWindowDim(hour)];
    return { score: weighted(dims, CONTENT_WEIGHTS), dims };
  }

  function commissionDim(p) {
    return { name: "commission", score: round1(clamp(((p.commission_rate - 0.05) / 0.20) * 100)), reason: `佣金 ${pct1(p.commission_rate)}` };
  }

  function conversionHistoryDim(p) {
    const baseCvr = (DATA.profile.baselines || {}).avg_cvr ?? 0.02;
    const score = clamp((p.sales_count / 3000) * 60 + 20);
    return { name: "conversion_history", score: round1(score), reason: `商品累计销量 ${p.sales_count} 单；账号基线转化 ${pct1(baseCvr)}` };
  }

  function priceFitDim(p) {
    const bands = DATA.profile.price_bands || {};
    if (!Object.keys(bands).length) return { name: "price_fit", score: 50, reason: "未配置粉丝价格带，给中性分 50" };
    const band = p.price <= 50 ? "0-50" : p.price <= 150 ? "50-150" : "150+";
    const share = bands[band] == null ? 0.10 : bands[band];
    return { name: "price_fit", score: round1(clamp((share / 0.50) * 100)), reason: `价格 ¥${Math.round(p.price)} 落在[${band}]带，该带粉丝占比 ${Math.round(share * 100)}%` };
  }

  function returnRiskDim(p) {
    const score = clamp(((0.15 - p.return_rate) / 0.10) * 100);
    const grade = p.return_rate < 0.05 ? "优秀" : p.return_rate < 0.10 ? "正常" : "高危";
    return { name: "return_risk", score: round1(score), reason: `退货率 ${pct1(p.return_rate)}（${grade}）` };
  }

  function scoreProduct(p) {
    const dims = [commissionDim(p), conversionHistoryDim(p), priceFitDim(p), returnRiskDim(p)];
    return { score: weighted(dims, PRODUCT_WEIGHTS), dims };
  }

  function scoreTopicFull(topic, hour) {
    const content = scoreContent(topic, hour);
    const product = topic.product_id ? byProductId(topic.product_id) : null;
    const productScore = product ? scoreProduct(product) : null;
    const total = productScore
      ? round2(content.score * 0.6 + productScore.score * 0.4)
      : round2(content.score * 0.6);
    const weaknesses = [];
    for (const d of content.dims.concat(productScore ? productScore.dims : [])) {
      if (d.score < WEAK) weaknesses.push(`${d.name}=${d.score}（${d.reason}）`);
    }
    return { contentScore: content.score, contentDims: content.dims, productScore: productScore ? productScore.score : null, productDims: productScore ? productScore.dims : [], total: total, weaknesses };
  }

  /* ---------------- 合规检查（移植自 app/services/compliance.py） ---------------- */
  const LEVEL_ACTION = { P0: "block", P1: "manual_review", P2: "rewrite_hint" };

  function checkTextSpans(text) {
    const spans = [];
    for (const level of ["P0", "P1", "P2"]) {
      for (const word of DATA.banned_words[level]) {
        let start = text.indexOf(word);
        while (start >= 0) {
          const end = start + word.length;
          spans.push({
            level, word, start, end,
            context: text.slice(Math.max(0, start - 10), Math.min(text.length, end + 10)),
            action: LEVEL_ACTION[level],
            hint: DATA.rewrite_hints[word] || "",
          });
          start = text.indexOf(word, end);
        }
      }
    }
    spans.sort((a, b) => a.start - b.start || a.end - b.end);
    return spans;
  }

  function reviewCompliance(topic) {
    const hits = [];
    for (const s of checkTextSpans(`${topic.title}\n${topic.outline || ""}`)) {
      hits.push({ level: s.level, kind: "banned_word", matched: s.word, action: s.action });
    }
    const product = topic.product_id ? byProductId(topic.product_id) : null;
    if (product) {
      for (const [catPrefix, licenses] of Object.entries(DATA.category_licenses)) {
        if (product.category && product.category.startsWith(catPrefix)) {
          const have = new Set(product.required_licenses || []);
          for (const lic of licenses) {
            if (!have.has(lic)) hits.push({ level: "P0", kind: "license_missing", matched: lic, action: "block" });
          }
          break;
        }
      }
    }
    return { blocked: hits.some((h) => h.level === "P0"), need_manual_review: hits.some((h) => h.level === "P1"), hits };
  }

  /* ---------------- 评论助手（移植自 comment_assistant.py） ---------------- */
  function classifyIntent(text) {
    for (const [intent, words] of DATA.comment_rules) {
      for (const w of words) if (text.includes(w)) return [intent, w];
    }
    return ["other", null];
  }

  function makeDraft(text) {
    const [intent, matched] = classifyIntent(text);
    return { intent, label: DATA.intent_labels[intent], confidence: matched ? "high" : "medium", draft: DATA.comment_templates[intent], matched };
  }

  /* ---------------- 知识库 BM25（移植自 rag.py） ---------------- */
  function splitChunks(docName, content) {
    const lines = content.replace(/\r\n/g, "\n").split("\n");
    if (lines.length && lines[lines.length - 1] === "") lines.pop();
    const sections = [];
    let curTitle = "", buf = [];
    lines.forEach((line, i) => {
      const m = line.match(/^(#{1,4})\s+(.+)$/);
      if (m) {
        if (buf.length) sections.push([curTitle || "（无标题）", buf]);
        curTitle = m[2].trim();
        buf = [[i + 1, line]];
      } else {
        buf.push([i + 1, line]);
      }
    });
    if (buf.length) sections.push([curTitle || "（无标题）", buf]);
    const chunks = [];
    for (const [title, sec] of sections) {
      for (let j = 0; j < sec.length; j += 30) {
        const piece = sec.slice(j, j + 30);
        const text = piece.map((p) => p[1]).join("\n").trim();
        if (text) chunks.push({ doc: docName, title, text, lines: [piece[0][0], piece[piece.length - 1][0]] });
      }
    }
    return chunks;
  }

  function tokenize(text) {
    const norm = text.replace(/[^\w\u4e00-\u9fff]+/g, " ");
    const tokens = [];
    for (const part of norm.split(/\s+/).filter(Boolean)) {
      if (/^[A-Za-z0-9_]+$/.test(part)) tokens.push(part.toLowerCase());
      else for (let i = 0; i < part.length - 1; i++) tokens.push(part.slice(i, i + 2));
    }
    return tokens;
  }

  class BM25 {
    constructor(chunks) {
      this.chunks = chunks;
      this.docTokens = chunks.map((c) => tokenize(c.text));
      this.avgdl = this.docTokens.reduce((s, t) => s + t.length, 0) / (this.docTokens.length || 1);
      this.df = {};
      for (const tokens of this.docTokens) for (const tok of new Set(tokens)) this.df[tok] = (this.df[tok] || 0) + 1;
      this.N = chunks.length;
    }
    idf(tok) { return Math.log(1 + (this.N - (this.df[tok] || 0) + 0.5) / ((this.df[tok] || 0) + 0.5)); }
    search(query, topK = 5, minScore = 0.05) {
      const qTokens = tokenize(query);
      const k1 = 1.5, b = 0.75;
      const scored = this.docTokens.map((tokens, idx) => {
        const tf = {};
        for (const t of tokens) tf[t] = (tf[t] || 0) + 1;
        let s = 0;
        for (const qt of new Set(qTokens)) {
          if (tf[qt]) s += this.idf(qt) * tf[qt] * (k1 + 1) / (tf[qt] + k1 * (1 - b + b * tokens.length / this.avgdl));
        }
        return [this.chunks[idx], s];
      });
      return scored.sort((a, b2) => b2[1] - a[1]).slice(0, topK).filter((x) => x[1] > minScore)
        .map(([c, s]) => ({ chunk: c, score: s }));
    }
  }

  const KB = new BM25(DATA.knowledge.flatMap((k) => splitChunks(k.doc, k.content)));

  function queryKB(query, topK = 5) {
    const hits = KB.search(query, topK);
    if (!hits.length) return { answer: "知识库中没有相关内容。", source: "empty", hits: [], llm_used: false };
    const citations = hits.map((h) => ({
      doc: h.chunk.doc, title: h.chunk.title, lines: h.chunk.lines,
      score: Math.round(h.score * 1000) / 1000, excerpt: h.chunk.text.slice(0, 200),
    }));
    const joined = hits.map((h) => `[${h.chunk.doc}#${h.chunk.title}] ${h.chunk.text}`).join("\n\n");
    return { answer: `（静态演示版未接入 LLM，返回知识库原文片段）\n\n${joined}`, source: "template", hits: citations, llm_used: false };
  }

  /* ---------------- AI 选题风暴（移植自 brainstorm.py） ---------------- */
  function brainstorm(category, keywords, count) {
    const b = DATA.brainstorm;
    const items = (keywords && keywords.length) ? keywords : (b.default_items[category] || b.default_items["女装"]);
    const titles = [];
    let i = 0;
    while (titles.length < count && i < 200) {
      const pat = b.patterns[i % b.patterns.length];
      const item = items[Math.floor(i / b.patterns.length) % items.length];
      const scene = b.scenes[(i * 3) % b.scenes.length];
      const price = b.prices[i % b.prices.length];
      const t = pat.replace("{scene}", scene).replace("{item}", item).replace("{price}", price);
      if (!titles.includes(t) && t.length <= 30) titles.push(t);
      i++;
    }
    const candidates = [];
    for (const title of titles) {
      const spans = checkTextSpans(title);
      if (spans.some((s) => s.level === "P0")) continue;
      const topic = { title, category, estimated_duration_sec: 30, hook_strength: 0.65, product_id: null };
      const full = scoreTopicFull(topic, 19);
      candidates.push({
        title,
        content_score: full.contentScore,
        weaknesses: full.weaknesses,
        compliance_flags: spans.map((s) => `${s.level}:${s.word}`),
      });
    }
    candidates.sort((a2, b2) => b2.content_score - a2.content_score);
    return { source: "template", category, count: candidates.length, candidates };
  }

  /* ---------------- 内存状态 ---------------- */
  const state = {
    topics: [],      // 完整对象（含 latest_score / compliance_hits）
    comments: [],
    audit: [],
    nextTopicId: 0,
    nextCommentId: 0,
    nextAuditId: 0,
  };

  function initState() {
    const briefMap = {};
    for (const t of DATA.topics) briefMap[t.id] = t;
    state.topics = DATA.topics.map((detail) => {
      const facts = (DATA.topic_facts || {})[detail.id] || {};
      return {
        ...facts,
        id: detail.id,
        title: detail.title,
        status: detail.status,
        category: detail.category,
        product_id: detail.product_id,
        published_video_external_id: detail.published_video_external_id || "",
        latest_score: detail.latest_score || null,
        compliance_hits: detail.compliance_hits || [],
        created_at: (briefMap[detail.id] || {}).created_at || facts.created_at || nowIso(),
      };
    });
    state.comments = DATA.comments.map((c) => ({ ...c }));
    state.audit = DATA.audit.map((a) => ({ ...a }));
    state.nextTopicId = Math.max(0, ...state.topics.map((t) => t.id)) + 1;
    state.nextCommentId = Math.max(0, ...state.comments.map((c) => c.id)) + 1;
    state.nextAuditId = Math.max(0, ...state.audit.map((a) => a.id)) + 1;
  }

  function addAudit(action, entityType, entityId, detail) {
    state.audit.unshift({
      id: state.nextAuditId++, action, entity_type: entityType,
      entity_id: String(entityId), detail: detail || {}, created_at: nowIso(),
    });
    if (state.audit.length > 60) state.audit.pop();
  }

  const findTopic = (id) => state.topics.find((t) => t.id === Number(id));

  function topicBrief(t) {
    return {
      id: t.id, title: t.title, category: t.category, status: t.status,
      product_id: t.product_id,
      content_score: t.latest_score ? t.latest_score.content_score : null,
      product_score: t.latest_score ? t.latest_score.product_score : null,
      total_score: t.latest_score ? t.latest_score.total_score : null,
      created_at: t.created_at,
    };
  }

  function topicDetail(t) {
    return {
      id: t.id, title: t.title, status: t.status, category: t.category,
      product_id: t.product_id,
      published_video_external_id: t.published_video_external_id || "",
      latest_score: t.latest_score, compliance_hits: t.compliance_hits || [],
    };
  }

  function overview() {
    const counts = {};
    for (const t of state.topics) counts[t.status] = (counts[t.status] || 0) + 1;
    counts.approved_total = (counts.approved || 0) + (counts.published || 0);
    return {
      videos: DATA.videos.length,
      products: Object.keys(DATA.products).length,
      comment_drafts: state.comments.length,
      topics_by_status: counts,
    };
  }

  function dashboard() {
    const d = JSON.parse(JSON.stringify(DATA.dashboard));
    const order = ["draft", "scored", "reviewed", "approved", "published"];
    const counts = {};
    for (const t of state.topics) counts[t.status] = (counts[t.status] || 0) + 1;
    d.funnel = order.map((s) => ({ status: s, count: counts[s] || 0 }))
      .concat([{ status: "rejected", count: counts.rejected || 0 }]);
    const buckets = { "90分以上": 0, "75-89分": 0, "60-74分": 0, "60分以下": 0 };
    for (const t of state.topics) {
      const v = t.latest_score ? t.latest_score.total_score : null;
      if (v == null) continue;
      if (v >= 90) buckets["90分以上"]++;
      else if (v >= 75) buckets["75-89分"]++;
      else if (v >= 60) buckets["60-74分"]++;
      else buckets["60分以下"]++;
    }
    d.score_buckets = buckets;
    const intents = {};
    for (const c of state.comments) intents[c.intent] = (intents[c.intent] || 0) + 1;
    d.comment_intents = intents;
    d.totals.topics = state.topics.length;
    return d;
  }

  /* ---------------- API 路由 ---------------- */
  function jsonResponse(data, status = 200) {
    return new Response(JSON.stringify(data), {
      status, headers: { "Content-Type": "application/json; charset=utf-8" },
    });
  }
  const err = (msg, status = 400) => jsonResponse({ detail: msg }, status);

  async function handleApi(method, pathname, query, payload) {
    const p = pathname.replace(/\/+$/, "");

    if (method === "GET" && p === "/api/overview") return jsonResponse(overview());
    if (method === "GET" && p === "/api/audit") return jsonResponse(state.audit);
    if (method === "GET" && p === "/api/topics") {
      return jsonResponse([...state.topics].sort((a, b) => b.id - a.id).map(topicBrief));
    }
    if (method === "GET" && p === "/api/dashboard") return jsonResponse(dashboard());
    if (method === "GET" && p === "/api/review/calibration") return jsonResponse(DATA.calibration);
    if (method === "GET" && p === "/api/review/report") return jsonResponse(DATA.report);
    if (method === "GET" && p === "/api/review/conversion") return jsonResponse(DATA.conversion);
    if (method === "GET" && p === "/api/settings/llm") {
      return jsonResponse({ configured: false, available: false, model: "deepseek-chat",
        base_url: "https://api.openai.com/v1", api_key_masked: "", daily_cap: 200, used_today: 0 });
    }
    if (method === "PUT" && p === "/api/settings/llm") {
      return err("静态演示版不支持在浏览器保存 API Key；完整版部署后可在「知识库·问答」中配置", 400);
    }
    if (method === "POST" && p === "/api/settings/llm/test") {
      return jsonResponse({ ok: false, message: "静态演示版未接入模型；完整版配置 Key 后可测试" });
    }

    let m = p.match(/^\/api\/topics\/(\d+)$/);
    if (m && method === "GET") {
      const t = findTopic(m[1]);
      return t ? jsonResponse(topicDetail(t)) : err("选题不存在", 404);
    }

    if (method === "POST" && p === "/api/topics") {
      const product = payload.product_external_id ? byProductExternal(payload.product_external_id) : null;
      if (payload.product_external_id && !product) return err(`商品 ${payload.product_external_id} 不存在，请先导入`, 404);
      const topic = {
        id: state.nextTopicId++,
        title: payload.title,
        outline: payload.outline || "",
        category: payload.category || "",
        estimated_duration_sec: payload.estimated_duration_sec || 30,
        hook_strength: payload.hook_strength == null ? 0.5 : payload.hook_strength,
        product_id: product ? product.id : null,
        status: "draft",
        published_video_external_id: "",
        latest_score: null,
        compliance_hits: [],
        created_at: nowIso(),
      };
      state.topics.push(topic);
      return jsonResponse({ id: topic.id, status: topic.status }, 201);
    }

    m = p.match(/^\/api\/topics\/(\d+)\/score$/);
    if (m && method === "POST") {
      const t = findTopic(m[1]);
      if (!t) return err("选题不存在", 404);
      const hour = query.get("publish_hour");
      const full = scoreTopicFull(t, hour == null ? null : parseInt(hour, 10));
      const version = t.latest_score ? t.latest_score.version + 1 : 1;
      t.latest_score = {
        version,
        content_score: full.contentScore,
        product_score: full.productScore,
        total_score: full.total,
        weaknesses: full.weaknesses,
      };
      if (t.status === "draft") t.status = "scored";
      addAudit("score", "topic", t.id, { version, total: full.total });
      return jsonResponse({
        topic_id: t.id, version,
        content_score: full.contentScore, product_score: full.productScore,
        total_score: full.total, weaknesses: full.weaknesses,
        detail: {
          content: full.contentDims.map((d) => ({ name: d.name, score: d.score, reason: d.reason })),
          product: full.productDims.map((d) => ({ name: d.name, score: d.score, reason: d.reason })),
        },
      });
    }

    m = p.match(/^\/api\/topics\/(\d+)\/review$/);
    if (m && method === "POST") {
      const t = findTopic(m[1]);
      if (!t) return err("选题不存在", 404);
      const result = reviewCompliance(t);
      t.compliance_hits = (t.compliance_hits || []).concat(result.hits);
      if (result.blocked) t.status = "rejected";
      else if (t.status === "scored") t.status = "reviewed";
      addAudit("compliance_review", "topic", t.id, {
        blocked: result.blocked,
        levels: [...new Set(result.hits.map((h) => h.level))].sort(),
        hits: result.hits.length,
      });
      return jsonResponse({
        topic_id: t.id, blocked: result.blocked,
        need_manual_review: result.need_manual_review,
        hits: result.hits, topic_status: t.status,
      });
    }

    m = p.match(/^\/api\/topics\/(\d+)\/approve$/);
    if (m && method === "POST") {
      const t = findTopic(m[1]);
      if (!t) return err("选题不存在", 404);
      if (!["scored", "reviewed"].includes(t.status)) return err(`当前状态 ${t.status} 不可审批（需先评分）`, 409);
      t.status = payload.decision;
      addAudit(payload.decision, "topic", t.id, { reviewer: payload.reviewer || "站长", note: payload.note || "" });
      return jsonResponse({ topic_id: t.id, status: t.status });
    }

    m = p.match(/^\/api\/topics\/(\d+)\/publish$/);
    if (m && method === "POST") {
      const t = findTopic(m[1]);
      if (!t) return err("选题不存在", 404);
      if (!["approved", "published"].includes(t.status)) return err(`当前状态 ${t.status} 不可标记发布（需先审批通过）`, 409);
      if (t.published_video_external_id === payload.video_external_id) return err("该选题已挂接此视频", 409);
      const video = DATA.videos.find((v) => v.external_id === payload.video_external_id);
      if (!video) return err(`视频 ${payload.video_external_id} 不存在，请先导入视频数据`, 404);
      t.published_video_external_id = payload.video_external_id;
      t.status = "published";
      addAudit("publish", "topic", t.id, { video_external_id: payload.video_external_id });
      return jsonResponse({ topic_id: t.id, status: t.status, video_external_id: t.published_video_external_id });
    }

    if (method === "POST" && p === "/api/comments/draft") {
      const d = makeDraft(payload.comment_text);
      const row = {
        id: state.nextCommentId++,
        video_external_id: payload.video_external_id || "",
        comment_text: payload.comment_text,
        intent: d.intent, intent_label: d.label,
        confidence: d.confidence, draft: d.draft, source: "template",
        status: "draft", final_text: null, created_at: nowIso(),
      };
      state.comments.unshift(row);
      addAudit("comment_draft", "comment", row.id, { intent: d.intent, confidence: d.confidence, source: "template" });
      return jsonResponse({
        id: row.id, intent: row.intent, intent_label: row.intent_label,
        confidence: row.confidence, draft: row.draft, source: row.source,
        status: row.status, notes: [],
      }, 201);
    }
    if (method === "GET" && p === "/api/comments/drafts") {
      return jsonResponse([...state.comments].sort((a, b) => b.id - a.id));
    }
    m = p.match(/^\/api\/comments\/drafts\/(\d+)\/finalize$/);
    if (m && method === "POST") {
      const c = state.comments.find((x) => x.id === Number(m[1]));
      if (!c) return err("草稿不存在", 404);
      c.final_text = payload.final_text;
      c.status = payload.status;
      addAudit("comment_finalize", "comment", c.id, { status: payload.status, edited: payload.final_text !== c.draft });
      return jsonResponse({ id: c.id, status: c.status, edited: payload.final_text !== c.draft });
    }

    if (method === "POST" && p === "/api/kb/query") {
      return jsonResponse(queryKB(payload.query, payload.top_k || 5));
    }
    if (method === "POST" && p === "/api/kb/reload") {
      return jsonResponse({ chunks: KB.chunks.length });
    }

    if (method === "POST" && p === "/api/compliance/check-text") {
      const spans = checkTextSpans(payload.text);
      const counts = { P0: 0, P1: 0, P2: 0 };
      for (const s of spans) counts[s.level]++;
      const verdict = counts.P0 ? "block" : counts.P1 ? "manual_review" : counts.P2 ? "rewrite_hint" : "clean";
      return jsonResponse({ hits: spans, counts, total: spans.length, verdict });
    }

    if (method === "POST" && p === "/api/brainstorm") {
      return jsonResponse(brainstorm(payload.category || "女装", payload.keywords || [], payload.count || 10));
    }

    if (method === "POST" && (p === "/api/import/videos" || p === "/api/import/products" || p === "/api/import/sales")) {
      return err("静态演示版不支持上传文件；完整版部署后可导入 CSV", 400);
    }

    return err(`静态演示版未实现该接口：${method} ${p}`, 404);
  }

  /* ---------------- fetch 拦截 ---------------- */
  window.fetch = async function (input, init) {
    const rawUrl = typeof input === "string" ? input : (input && input.url) || "";
    let pathname = rawUrl;
    let query = new URLSearchParams();
    try {
      const u = new URL(rawUrl, location.href);
      pathname = u.pathname;
      query = u.searchParams;
    } catch (e) { /* 保持原样 */ }

    if (pathname === "/health") {
      return jsonResponse({ status: "ok", service: "contentops-agent（静态演示版）" });
    }
    if (!pathname.startsWith("/api")) {
      return realFetch(input, init);
    }
    const method = ((init && init.method) || "GET").toUpperCase();
    let payload = null;
    if (init && init.body && typeof init.body === "string") {
      try { payload = JSON.parse(init.body); } catch (e) { payload = {}; }
    } else if (init && init.body instanceof FormData) {
      payload = {};
    }
    try {
      return await handleApi(method, pathname, query, payload || {});
    } catch (e) {
      return err("演示版内部错误：" + e.message, 500);
    }
  };

  initState();
})();
