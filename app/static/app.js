/* ContentOps 工作台前端：纯原生 JS，无框架依赖 */
"use strict";
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

const HOOKS = ["悬念反转", "痛点直击", "效果对比", "测评实验", "剧情植入", "口播种草"];
const STATUS_MAP = {
  draft: ["草稿", "badge-gray"],
  scored: ["已评分", "badge-blue"],
  reviewed: ["已审查", "badge-cyan"],
  approved: ["已通过", "badge-green"],
  published: ["已发布", "badge-purple"],
  rejected: ["已拦截", "badge-red"],
};
const STATUS_LABEL = Object.fromEntries(Object.entries(STATUS_MAP).map(([k, v]) => [k, v[0]]));
const DIM_LABELS = {
  finish_rate: "预估完播率",
  topic_heat: "垂类热度",
  competition: "竞争饱和度",
  audience_match: "粉丝画像匹配",
  publish_window: "发布时段",
  commission: "佣金比例",
  conversion_history: "同类目转化",
  price_fit: "价格带匹配",
  return_risk: "退货风险",
};
const ACTION_LABELS = {
  import: "导入数据", score: "选题评分", compliance_review: "合规审查",
  approved: "审批通过", rejected: "驳回", kb_reload: "知识库重载",
  comment_draft: "评论草稿", comment_finalize: "评论定稿",
  publish: "标记发布", brainstorm: "选题风暴", llm_config: "AI配置",
};

let currentTopicId = null;

/* ---------- 基础工具 ---------- */
function toast(msg, ms = 2600) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.remove("show"), ms);
}

async function api(path, opts = {}) {
  const resp = await fetch(path, opts);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const detail = data.detail;
    const msg = typeof detail === "string" ? detail : (detail ? JSON.stringify(detail) : `HTTP ${resp.status}`);
    throw new Error(msg);
  }
  return data;
}
const postJSON = (path, body) => api(path, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function scoreClass(score) { return score >= 75 ? "green" : score >= 60 ? "blue" : "red"; }
function scoreColor(score) { return score >= 75 ? "#22a06b" : score >= 60 ? "#3b82f6" : "#e5484d"; }

/* ---------- 页签切换 ---------- */
$$("#tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$("#tabs button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    $$(".view").forEach((v) => v.classList.remove("active"));
    $(`#view-${btn.dataset.view}`).classList.add("active");
    if (btn.dataset.view === "overview") loadOverview();
    if (btn.dataset.view === "dashboard") loadDashboard();
    if (btn.dataset.view === "topics") loadTopicList();
    if (btn.dataset.view === "review") loadReview();
    if (btn.dataset.view === "comments") loadDraftList();
    if (btn.dataset.view === "kb") loadLlmStatus();
  });
});

/* ---------- 健康检查 ---------- */
async function checkHealth() {
  try {
    await api("/health");
    $("#api-dot").className = "dot ok";
    $("#api-text").textContent = "系统运行中";
  } catch {
    $("#api-dot").className = "dot bad";
    $("#api-text").textContent = "服务未启动（运行 uvicorn 命令后刷新）";
  }
}

/* ---------- 总览 ---------- */
async function loadOverview() {
  try {
    const o = await api("/api/overview");
    const st = o.topics_by_status || {};
    const cards = [
      [o.videos, "已导入视频"],
      [o.products, "已导入商品"],
      [st.scored || 0, "待审批选题"],
      [st.approved_total ?? st.approved ?? 0, "已通过选题"],
      [st.rejected || 0, "合规拦截选题"],
      [o.comment_drafts || 0, "评论草稿"],
    ];
    $("#overview-cards").innerHTML = cards.map(([n, l]) =>
      `<div class="stat"><div class="num">${n}</div><div class="lbl">${l}</div></div>`).join("");
    const logs = await api("/api/audit?limit=8");
    $("#overview-audit").innerHTML = logs.length ? `<table><tr><th>时间</th><th>动作</th><th>对象</th><th>详情</th></tr>` +
      logs.map((l) => `<tr><td>${esc((l.created_at || "").replace("T", " ").slice(5, 16))}</td>
        <td>${esc(ACTION_LABELS[l.action] || l.action)}</td>
        <td>${esc(l.entity_type)}${l.entity_id ? "#" + esc(l.entity_id) : ""}</td>
        <td class="hint">${esc(JSON.stringify(l.detail))}</td></tr>`).join("") + `</table>`
      : "还没有操作记录";
  } catch (e) { $("#overview-audit").textContent = "加载失败：" + e.message; }
}

/* ---------- 选题工作台 ---------- */
function initHookSelect() {
  $("#t-hook").innerHTML = HOOKS.map((h) => `<option>${h}</option>`).join("");
}
$("#t-strength")?.addEventListener("input", (e) => { $("#t-strength-val").textContent = e.target.value; });

$("#t-create").addEventListener("click", async () => {
  const title = $("#t-title").value.trim();
  if (!title) return toast("先填选题标题");
  const payload = {
    title,
    outline: $("#t-outline").value.trim(),
    category: $("#t-category").value.trim() || "女装",
    estimated_duration_sec: parseInt($("#t-duration").value || "30", 10),
    hook_strength: parseInt($("#t-strength").value, 10) / 100,
    product_external_id: $("#t-product").value.trim() || null,
  };
  try {
    const created = await postJSON("/api/topics", payload);
    currentTopicId = created.id;
    toast(`选题 #${created.id} 已创建，正在评分…`);
    await doScore();
    loadTopicList();
  } catch (e) { toast("创建失败：" + e.message); }
});

async function doScore() {
  if (!currentTopicId) return;
  const hour = parseInt($("#t-hour").value || "19", 10);
  try {
    const s = await postJSON(`/api/topics/${currentTopicId}/score?publish_hour=${hour}`, {});
    renderScore(s);
    $("#t-actions").style.display = "flex";
  } catch (e) { toast("评分失败：" + e.message); }
}

function renderScore(s) {
  const dims = (s.detail?.content || []).concat(s.detail?.product || []);
  const productLine = s.product_score == null
    ? `<span class="score-sub">未挂车（商品分不启用）</span>`
    : `<span class="score-sub">商品分 <b style="color:${scoreColor(s.product_score)}">${s.product_score}</b></span>`;
  $("#t-result").innerHTML = `
    <div class="score-head">
      <div><div class="score-total">${s.total_score}</div><div class="score-sub">总分（内容×60% + 商品×40%）</div></div>
      <div class="score-sub">内容分 <b style="color:${scoreColor(s.content_score)}">${s.content_score}</b></div>
      ${productLine}
      <div class="score-sub">第 ${s.version} 版</div>
    </div>
    ${dims.map((d) => `
      <div class="dim">
        <div class="top"><span>${esc(DIM_LABELS[d.name] || d.name)}</span><b>${d.score}</b></div>
        <div class="bar"><i style="width:${Math.min(100, d.score)}%;background:${scoreColor(d.score)}"></i></div>
        <div class="why">${esc(d.reason)}</div>
      </div>`).join("")}
    ${s.weaknesses?.length ? `<div class="weak-box"><b>短板提示（低于60分）：</b><br>${s.weaknesses.map(esc).join("<br>")}</div>` : ""}
  `;
}

$("#t-rescore").addEventListener("click", doScore);

$("#t-review").addEventListener("click", async () => {
  if (!currentTopicId) return;
  try {
    const r = await postJSON(`/api/topics/${currentTopicId}/review`, {});
    const badge = r.blocked ? `<span class="badge badge-red">已拦截，不可审批</span>`
      : (r.need_manual_review ? `<span class="badge badge-amber">P1 命中，请人工确认后再通过</span>`
        : `<span class="badge badge-green">合规通过</span>`);
    const hits = (r.hits || []).map((h) =>
      `<div class="hit"><div class="meta">${h.level} · ${h.kind === "license_missing" ? "资质缺失" : "违禁词"} · 动作 ${h.action}</div>命中：<b>${esc(h.matched)}</b></div>`).join("");
    $("#t-result").insertAdjacentHTML("beforeend",
      `<hr style="border:none;border-top:1px solid #e8ebf2;margin:14px 0"><div>${badge}</div>${hits || '<div class="hint" style="margin-top:6px">没有命中任何风险词和资质问题。</div>'}`);
    toast(r.blocked ? "合规拦截：选题已标记 rejected" : "合规审查完成");
    loadTopicList();
  } catch (e) { toast("审查失败：" + e.message); }
});

async function decide(decision) {
  if (!currentTopicId) return;
  try {
    await postJSON(`/api/topics/${currentTopicId}/approve`, { decision, reviewer: "站长", note: "" });
    toast(decision === "approved" ? "已审批通过 ✅" : "已驳回");
    if (decision === "approved") $("#t-publish-row").style.display = "flex";
    loadTopicList();
  } catch (e) { toast("操作失败：" + e.message); }
}
$("#t-approve").addEventListener("click", () => decide("approved"));
$("#t-reject").addEventListener("click", () => decide("rejected"));

async function loadTopicList() {
  try {
    const list = await api("/api/topics?limit=50");
    if (!list.length) { $("#t-list").textContent = "还没有选题，先在上面建一个。"; return; }
    $("#t-list").innerHTML = list.map((t) => {
      const [label, cls] = STATUS_MAP[t.status] || [t.status, "badge-gray"];
      const score = t.total_score == null ? "—" : t.total_score;
      return `<div class="list-item" data-id="${t.id}">
        <div><div class="lt">#${t.id} ${esc(t.title)}</div>
        <div class="lm">${esc(t.category || "")} · 评分 ${score} · ${esc((t.created_at || "").slice(0, 10))}</div></div>
        <span class="badge ${cls}">${label}</span>
      </div>`;
    }).join("");
    $$("#t-list .list-item").forEach((el) => el.addEventListener("click", () => loadTopicDetail(parseInt(el.dataset.id, 10))));
  } catch (e) { $("#t-list").textContent = "加载失败：" + e.message; }
}

async function loadTopicDetail(id) {
  try {
    const t = await api(`/api/topics/${id}`);
    currentTopicId = id;
    $("#t-title").value = t.title || "";
    $("#t-category").value = t.category || "";
    $("#t-actions").style.display = "flex";
    // 发布挂接：仅审批通过后的选题可见
    const pubRow = $("#t-publish-row");
    if (t.status === "approved" || t.status === "published") {
      pubRow.style.display = "flex";
      $("#t-publish-video").value = t.published_video_external_id || "";
      const pubBtn = $("#t-publish");
      pubBtn.textContent = t.published_video_external_id ? "已挂接 ✓" : "挂接视频";
      pubBtn.disabled = !!t.published_video_external_id;
    } else {
      pubRow.style.display = "none";
    }
    const s = t.latest_score;
    $("#t-result").innerHTML = s ? `
      <div class="score-head">
        <div><div class="score-total">${s.total_score}</div><div class="score-sub">总分（第 ${s.version} 版）</div></div>
        <div class="score-sub">内容分 <b style="color:${scoreColor(s.content_score)}">${s.content_score}</b></div>
        ${s.product_score == null ? '<span class="score-sub">未挂车</span>' : `<div class="score-sub">商品分 <b style="color:${scoreColor(s.product_score)}">${s.product_score}</b></div>`}
      </div>
      ${s.weaknesses?.length ? `<div class="weak-box">短板：${s.weaknesses.map(esc).join("<br>")}</div>` : ""}
      ${(t.compliance_hits || []).length ? t.compliance_hits.map((h) => `<div class="hit"><div class="meta">${h.level} · ${h.kind}</div>命中：<b>${esc(h.matched)}</b></div>`).join("") : '<div class="hint">无合规命中记录（可点“合规审查”执行检查）</div>'}
    ` : "该选题还没有评分，点“重新评分”试试。";
    toast(`已载入选题 #${id}，可重新评分或审查`);
  } catch (e) { toast("载入失败：" + e.message); }
}

/* ---------- 评论助手 ---------- */
$("#c-gen").addEventListener("click", async () => {
  const text = $("#c-input").value.trim();
  if (!text) return toast("先把评论粘进来");
  try {
    const r = await postJSON("/api/comments/draft", { comment_text: text });
    $("#c-result").innerHTML = `
      <div class="draft-box">
        <div style="margin-bottom:8px">
          <span class="badge badge-blue">${esc(r.intent_label)}</span>
          <span class="badge badge-gray">置信 ${r.confidence}</span>
          <span class="badge badge-gray">${r.source === "llm" ? "LLM" : "话术库"}</span>
        </div>
        <div id="c-draft-text">${esc(r.draft)}</div>
        <div class="row" style="margin:10px 0 0">
          <button class="btn" id="c-copy">复制草稿</button>
          <button class="btn" id="c-sent" data-id="${r.id}">标记已发送</button>
        </div>
        ${r.notes?.length ? `<div class="hint">${r.notes.map(esc).join("；")}</div>` : ""}
      </div>`;
    $("#c-copy").addEventListener("click", async () => {
      await navigator.clipboard.writeText(r.draft);
      toast("草稿已复制，记得自己改一下口吻再发");
    });
    $("#c-sent").addEventListener("click", async () => {
      await postJSON(`/api/comments/drafts/${r.id}/finalize`, { final_text: r.draft, status: "sent" });
      toast("已标记为发送");
      loadDraftList();
    });
    loadDraftList();
  } catch (e) { toast("生成失败：" + e.message); }
});

async function loadDraftList() {
  try {
    const list = await api("/api/comments/drafts?limit=10");
    if (!list.length) { $("#c-list").textContent = "还没有草稿。上面粘一条评论试试。"; return; }
    $("#c-list").innerHTML = list.map((d) => `
      <div class="list-item" style="cursor:default">
        <div><div class="lt">"${esc((d.comment_text || "").slice(0, 40))}"</div>
        <div class="lm">${esc(d.intent_label)} · ${esc((d.created_at || "").replace("T", " ").slice(5, 16))}</div></div>
        <span class="badge ${d.status === "sent" ? "badge-green" : "badge-gray"}">${d.status === "sent" ? "已发送" : "草稿"}</span>
      </div>`).join("");
  } catch { /* 静默 */ }
}

/* ---------- 知识库 ---------- */
$("#k-ask").addEventListener("click", async () => {
  const q = $("#k-q").value.trim();
  if (!q) return toast("先输入问题");
  try {
    const r = await postJSON("/api/kb/query", { query: q, top_k: 4 });
    $("#k-result").innerHTML = `
      <div class="answer-block" style="margin-top:12px">${esc(r.answer)}</div>
      ${(r.hits || []).map((h) => `
        <div class="hit"><div class="meta">${esc(h.doc)} · ${esc(h.title)} · 行 ${h.lines?.[0]}-${h.lines?.[1]} · 相关度 ${h.score}</div>
        ${esc(h.excerpt)}…</div>`).join("")}`;
  } catch (e) { toast("查询失败：" + e.message); }
});

$("#k-reload").addEventListener("click", async () => {
  try {
    const r = await postJSON("/api/kb/reload", {});
    toast(`知识库已重载：${r.chunks} 个知识块`);
  } catch (e) { toast("重载失败：" + e.message); }
});

/* ---------- 数据导入 ---------- */
async function upload(kind) {
  const input = kind === "video" ? $("#i-video") : $("#i-product");
  const resEl = kind === "video" ? $("#i-video-res") : $("#i-product-res");
  const file = input.files[0];
  if (!file) return toast("先选一个 CSV 文件");
  const fd = new FormData();
  fd.append("file", file);
  resEl.textContent = "上传中…";
  try {
    const r = await api(`/api/import/${kind === "video" ? "videos" : "products"}`, { method: "POST", body: fd });
    resEl.innerHTML = `✅ 导入成功：新增 <b>${r.created}</b> 条，跳过 ${r.skipped} 条${r.duplicate ? `，同批重复 ${r.duplicate} 条` : ""}（批次 ${esc(r.batch)}）`;
    toast("导入完成");
    loadOverview();
  } catch (e) {
    resEl.innerHTML = `❌ 导入失败：${esc(e.message)}`;
  }
}
$("#i-video-btn").addEventListener("click", () => upload("video"));
$("#i-product-btn").addEventListener("click", () => upload("product"));

/* ---------- 文案体检 ---------- */
let medicTimer = null;
const VERDICT_MAP = {
  block: ["不可发布：命中 P0 违禁词", "badge-red"],
  manual_review: ["发布前必须人工确认（P1 命中）", "badge-amber"],
  rewrite_hint: ["建议改写后发布（P2 提示）", "badge-gray"],
  clean: ["未命中风险词，可以继续发布", "badge-green"],
};
const LEVEL_BADGE = { P0: "badge-red", P1: "badge-amber", P2: "badge-gray" };

async function doMedic() {
  const text = $("#m-input").value;
  const verdictEl = $("#m-verdict");
  if (!text.trim()) {
    verdictEl.className = "badge badge-gray";
    verdictEl.textContent = "等待输入…";
    $("#m-count").textContent = "";
    $("#m-preview").textContent = "输入后这里显示高亮结果";
    $("#m-hits").innerHTML = "";
    return;
  }
  try {
    const data = await postJSON("/api/compliance/check-text", { text });
    const [label, cls] = VERDICT_MAP[data.verdict] || VERDICT_MAP.clean;
    verdictEl.className = "badge " + cls;
    verdictEl.textContent = label;
    $("#m-count").textContent = data.total
      ? `共命中 ${data.total} 处（P0 ${data.counts.P0} / P1 ${data.counts.P1} / P2 ${data.counts.P2}）`
      : "";
    let html = "", cursor = 0;
    for (const h of data.hits) {
      if (h.start < cursor) continue; // 与上一处重叠时跳过（已被覆盖）
      html += esc(text.slice(cursor, h.start));
      html += `<mark class="lv-${h.level.toLowerCase()}" title="${esc(h.level)}：${esc(h.word)}">${esc(text.slice(h.start, h.end))}</mark>`;
      cursor = h.end;
    }
    html += esc(text.slice(cursor));
    $("#m-preview").innerHTML = html;
    $("#m-hits").innerHTML = data.hits.map((h) => `
      <div class="hit">
        <div class="meta"><span class="badge ${LEVEL_BADGE[h.level]}">${esc(h.level)}</span>
          命中：<b>${esc(h.word)}</b> · 动作 ${esc(h.action)}</div>
        ${h.hint ? `<div class="hint">改写建议：${esc(h.hint)}</div>` : ""}
        <div class="hint">上下文：…${esc(h.context)}…</div>
      </div>`).join("");
  } catch (e) { /* 输入过程静默失败，避免干扰打字 */ }
}
$("#m-input").addEventListener("input", () => {
  clearTimeout(medicTimer);
  medicTimer = setTimeout(doMedic, 250);
});

/* ---------- AI 选题灵感 ---------- */
$("#b-generate").addEventListener("click", async () => {
  const category = $("#b-category").value.trim() || "女装";
  const keywords = $("#b-keywords").value.split(/[，,、\s]+/).map((s) => s.trim()).filter(Boolean);
  const count = parseInt($("#b-count").value || "10", 10);
  $("#b-list").innerHTML = '<div class="hint">生成中…</div>';
  try {
    const r = await postJSON("/api/brainstorm", { category, keywords, count });
    $("#b-note").textContent = (r.source === "llm" ? "模型生成" : "模板生成（未配置 LLM，确定性降级）")
      + " · P0 命中候选已自动丢弃 · 同参数下分数一致，差异体现在标题与合规预筛";
    $("#b-list").innerHTML = r.candidates.length ? r.candidates.map((c, i) => `
      <div class="list-item" style="cursor:default">
        <div style="flex:1">
          <div class="lt">${i + 1}. ${esc(c.title)}</div>
          <div class="lm">内容分 ${c.content_score}${c.compliance_flags.length
            ? " · ⚠️ " + esc(c.compliance_flags.join("、")) : " · 合规预筛通过"}</div>
        </div>
        <button class="btn" data-title="${esc(c.title)}">加入选题</button>
      </div>`).join("") : '<div class="hint">没有通过合规预筛的候选，换个关键词再试。</div>';
    $$("#b-list button").forEach((btn) => btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        const created = await postJSON("/api/topics", {
          title: btn.dataset.title, category, estimated_duration_sec: 30, hook_strength: 0.65,
        });
        await postJSON(`/api/topics/${created.id}/score?publish_hour=19`, {});
        toast(`已加入选题 #${created.id} 并完成评分`);
        loadTopicList();
      } catch (e) { toast("加入失败：" + e.message); btn.disabled = false; }
    }));
  } catch (e) {
    $("#b-list").innerHTML = `<div class="hint">生成失败：${esc(e.message)}</div>`;
  }
});

/* ---------- 发布挂接 ---------- */
$("#t-publish").addEventListener("click", async () => {
  if (!currentTopicId) return;
  const vid = $("#t-publish-video").value.trim();
  if (!vid) return toast("先填已发布视频的 external_id");
  try {
    await postJSON(`/api/topics/${currentTopicId}/publish`, { video_external_id: vid });
    toast("已挂接发布 ✓ 数据复盘里可看校准");
    await loadTopicDetail(currentTopicId);
    loadTopicList();
  } catch (e) { toast("挂接失败：" + e.message); }
});

/* ---------- 数据看板 ---------- */
const charts = {};
const PINK = "#ff4d79";
const PALETTE = ["#ff4d79", "#3b82f6", "#22a06b", "#f59e0b", "#8b5cf6", "#0d9488", "#e5484d", "#98a0ae"];

function initChart(id) {
  const el = document.getElementById(id);
  if (!el || typeof echarts === "undefined") return null;
  if (charts[id]) charts[id].dispose();
  charts[id] = echarts.init(el);
  return charts[id];
}
const axisStyle = {
  xAxis: { axisLine: { lineStyle: { color: "#dfe3ec" } }, axisLabel: { color: "#7a8294", fontSize: 11 } },
  yAxis: { splitLine: { lineStyle: { color: "#eef0f5" } }, axisLabel: { color: "#7a8294", fontSize: 11 } },
};

async function loadDashboard() {
  let d;
  try { d = await api("/api/dashboard"); }
  catch (e) { $("#db-hint").textContent = "加载失败：" + e.message; return; }
  $("#db-cards").innerHTML = [
    [d.totals.topics, "选题总数"],
    [d.totals.videos, "视频数据"],
    [d.totals.products, "商品数据"],
    [d.totals.sales_rows, "销售回流"],
    ["¥" + Math.round(d.totals.gmv).toLocaleString(), "累计 GMV"],
  ].map(([n, l]) => `<div class="stat"><div class="num">${n}</div><div class="lbl">${l}</div></div>`).join("");
  $("#db-hint").textContent = typeof echarts === "undefined" ? "⚠️ 图表库加载失败（静态文件缺失）" : "";

  const c1 = initChart("chart-funnel");
  c1 && c1.setOption({
    ...axisStyle, grid: { left: 66, right: 40, top: 16, bottom: 24 },
    xAxis: { ...axisStyle.xAxis, type: "value" },
    yAxis: { ...axisStyle.yAxis, type: "category", inverse: true, data: d.funnel.map((f) => STATUS_LABEL[f.status] || f.status) },
    series: [{ type: "bar", barWidth: 16, data: d.funnel.map((f) => f.count),
      itemStyle: { color: PINK, borderRadius: [0, 6, 6, 0] },
      label: { show: true, position: "right", color: "#5b6472", fontSize: 11 } }],
  });

  const c2 = initChart("chart-score");
  c2 && c2.setOption({
    ...axisStyle, grid: { left: 40, right: 20, top: 24, bottom: 24 },
    xAxis: { ...axisStyle.xAxis, type: "category", data: Object.keys(d.score_buckets) },
    yAxis: { ...axisStyle.yAxis, type: "value", minInterval: 1 },
    series: [{ type: "bar", barWidth: 42, data: Object.values(d.score_buckets),
      itemStyle: { color: "#3b82f6", borderRadius: [6, 6, 0, 0] },
      label: { show: true, position: "top", color: "#5b6472", fontSize: 11 } }],
  });

  const c3 = initChart("chart-compliance");
  c3 && c3.setOption({
    color: ["#e5484d", "#f59e0b", "#98a0ae"],
    tooltip: { trigger: "item" },
    legend: { bottom: 0, textStyle: { color: "#7a8294", fontSize: 11 } },
    series: [{ type: "pie", radius: ["42%", "68%"], center: ["50%", "44%"],
      label: { fontSize: 11, color: "#5b6472" },
      data: Object.entries(d.compliance_levels).map(([k, v]) => ({ name: k, value: v })) }],
  });

  const c4 = initChart("chart-trend");
  c4 && c4.setOption({
    ...axisStyle, tooltip: { trigger: "axis" },
    legend: { bottom: 0, textStyle: { color: "#7a8294", fontSize: 11 } },
    grid: { left: 56, right: 24, top: 24, bottom: 48 },
    xAxis: { ...axisStyle.xAxis, type: "category", data: d.trend.map((t) => t.month) },
    yAxis: { ...axisStyle.yAxis, type: "value" },
    series: [
      { name: "播放量", type: "line", smooth: true, data: d.trend.map((t) => t.views),
        lineStyle: { color: PINK }, itemStyle: { color: PINK },
        areaStyle: { color: "rgba(255,77,121,.08)" } },
      { name: "互动量（赞评转）", type: "line", smooth: true, data: d.trend.map((t) => t.interactions),
        lineStyle: { color: "#3b82f6" }, itemStyle: { color: "#3b82f6" } },
    ],
  });

  const c5 = initChart("chart-category");
  c5 && c5.setOption({
    color: PALETTE, tooltip: { trigger: "item" },
    legend: { bottom: 0, textStyle: { color: "#7a8294", fontSize: 11 } },
    series: [{ type: "pie", radius: ["42%", "68%"], center: ["50%", "44%"],
      label: { fontSize: 11, color: "#5b6472" },
      data: Object.entries(d.topic_categories).map(([k, v]) => ({ name: k, value: v })) }],
  });

  const c6 = initChart("chart-intents");
  const INTENT_LABELS = { purchase: "购买意向", product_qa: "商品咨询", complaint: "质疑差评", cooperation: "合作洽谈", other: "其他" };
  c6 && c6.setOption({
    color: PALETTE, tooltip: { trigger: "item" },
    legend: { bottom: 0, textStyle: { color: "#7a8294", fontSize: 11 } },
    series: [{ type: "pie", radius: ["42%", "68%"], center: ["50%", "44%"],
      label: { fontSize: 11, color: "#5b6472" },
      data: Object.entries(d.comment_intents).map(([k, v]) => ({ name: INTENT_LABELS[k] || k, value: v })) }],
  });
}

/* ---------- 数据复盘 ---------- */
function fmtPct(v, digits = 1) { return v == null ? "—" : (v * 100).toFixed(digits) + "%"; }

async function loadReview() {
  // 预测校准
  try {
    const cal = await api("/api/review/calibration");
    if (!cal.rows.length) {
      $("#rv-calibration").textContent = cal.conclusion;
    } else {
      $("#rv-calibration").innerHTML = `
        <div class="hit" style="margin-bottom:8px"><b>结论：</b>${esc(cal.conclusion)}
          <span class="hint">样本 ${cal.n} 条 · 平均偏差 ${(cal.avg_deviation * 100).toFixed(1)} 个百分点 · 命中率 ${(cal.hit_rate * 100).toFixed(0)}%</span></div>
        <table><tr><th>选题</th><th>预估完播</th><th>实际完播</th><th>偏差</th><th>钩子强度</th><th>时长</th></tr>
        ${cal.rows.map((r) => `<tr>
          <td>${esc(r.topic_title)}</td><td>${fmtPct(r.predicted)}</td><td>${fmtPct(r.actual)}</td>
          <td style="color:${r.deviation >= 0 ? "#22a06b" : "#e5484d"}">${r.deviation >= 0 ? "+" : ""}${(r.deviation * 100).toFixed(1)}pp</td>
          <td>${r.hook_strength}</td><td>${r.duration_sec}s</td></tr>`).join("")}
        </table>`;
    }
  } catch (e) { $("#rv-calibration").textContent = "加载失败：" + e.message; }

  // 报告与建议（近 90 天口径：演示数据跨度 9-10 月，30 天窗口样本不够对比）
  try {
    const rep = await api("/api/review/report?days=90");
    window._reportMd = rep.markdown;
    $("#rv-suggestions").innerHTML = rep.suggestions.map((s) => `
      <div class="hit"><div class="meta">${esc(s.dimension)} · ${esc(s.action)}</div>${esc(s.evidence)}</div>`).join("");
    $("#rv-markdown").textContent = rep.markdown;
  } catch (e) { $("#rv-suggestions").innerHTML = esc("加载失败：" + e.message); }

  // 转化明细
  try {
    const conv = await api("/api/review/conversion?days=90");
    $("#rv-detail").innerHTML = conv.rows.length ? `<table>
      <tr><th>视频</th><th>商品</th><th>GMV</th><th>销量</th><th>转化率</th><th>退货</th><th>预估佣金</th></tr>
      ${conv.rows.map((r) => `<tr>
        <td>${esc(r.video_title)}</td><td>${esc(r.product_title)}</td>
        <td>¥${r.gmv.toLocaleString()}</td><td>${r.sales_count}</td>
        <td>${fmtPct(r.cvr, 2)}</td>
        <td>${r.return_count}${r.sales_count ? `（${Math.round(r.return_count / r.sales_count * 100)}%）` : ""}</td>
        <td>¥${r.commission.toLocaleString()}</td></tr>`).join("")}
      </table>` : "还没有销售回流数据：到「数据导入」上传 sales.csv（或点“下载样例”跑通流程）。";
  } catch (e) { $("#rv-detail").textContent = "加载失败：" + e.message; }
}
$("#rv-reload").addEventListener("click", loadReview);
$("#rv-export").addEventListener("click", () => {
  if (!window._reportMd) return toast("先刷新报告");
  const blob = new Blob([window._reportMd], { type: "text/markdown;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `内容复盘_${new Date().toISOString().slice(0, 10)}.md`;
  a.click();
  URL.revokeObjectURL(a.href);
});

/* ---------- AI（LLM）配置 ---------- */
async function loadLlmStatus() {
  try {
    const s = await api("/api/settings/llm");
    const el = $("#llm-status");
    el.className = "badge " + (s.available ? "badge-green" : s.configured ? "badge-amber" : "badge-gray");
    el.textContent = s.available
      ? `已启用 · ${s.model} · 今日 ${s.used_today}/${s.daily_cap}`
      : s.configured ? "已配置（超限或待生效）" : "未配置（模板/原文降级运行）";
    if (s.configured) $("#llm-key").placeholder = `已保存 ${s.api_key_masked}（重新填写可覆盖）`;
    $("#llm-model").value = s.model || "";
    $("#llm-base").value = s.base_url || "";
    $("#llm-cap").value = s.daily_cap;
  } catch (e) { /* 静默 */ }
}
$("#llm-save").addEventListener("click", async () => {
  const payload = {
    base_url: $("#llm-base").value.trim(),
    model: $("#llm-model").value.trim(),
    daily_cap: parseInt($("#llm-cap").value || "200", 10),
  };
  const key = $("#llm-key").value.trim();
  if (key) payload.api_key = key;
  try {
    await api("/api/settings/llm", {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    $("#llm-key").value = "";
    toast("AI 配置已保存");
    loadLlmStatus();
  } catch (e) { toast("保存失败：" + e.message); }
});
$("#llm-test").addEventListener("click", async () => {
  try {
    const r = await postJSON("/api/settings/llm/test", {});
    toast(r.ok ? r.message : "测试失败：" + r.message);
    loadLlmStatus();
  } catch (e) { toast("测试失败：" + e.message); }
});

/* ---------- 销售数据上传 ---------- */
$("#i-sales-btn").addEventListener("click", async () => {
  const file = $("#i-sales").files[0];
  const resEl = $("#i-sales-res");
  if (!file) return toast("先选一个 CSV 文件");
  const fd = new FormData();
  fd.append("file", file);
  resEl.textContent = "上传中…";
  try {
    const r = await api("/api/import/sales", { method: "POST", body: fd });
    resEl.innerHTML = `✅ 导入成功：新增 <b>${r.created}</b> 条，跳过 ${r.skipped} 条` +
      `${r.duplicate ? `，同批重复 ${r.duplicate} 条` : ""}（批次 ${esc(r.batch)}）` +
      (r.unmatched && r.unmatched.length
        ? `<br>⚠️ 有 ${r.unmatched.length} 个编号在视频/商品表里没找到：${esc(r.unmatched.join("、"))}`
        : "");
    toast("销售数据导入完成");
  } catch (e) {
    resEl.innerHTML = `❌ 导入失败：${esc(e.message)}`;
  }
});

/* ---------- 初始化 ---------- */
initHookSelect();
checkHealth();
loadOverview();
loadTopicList();
loadDraftList();
loadLlmStatus();
window.addEventListener("resize", () => {
  Object.values(charts).forEach((c) => { try { c.resize(); } catch (e) { /* 忽略 */ } });
});
