"""LLM 可选接入层：OpenAI 兼容接口，未配置 Key 时降级为确定性模板。

设计原则（延续项目"确定性优先"）：
- 模型只做草稿生成与意图辅助，输出必须带 fallback 标记；
- 所有 LLM 输出仅作草稿，人工审批前不产生任何对外动作；
- 不配 Key 系统完全可用（模板降级），测试全部离线跑；
- 每日调用上限：界面可配置，超限自动停用（公开演示防刷）。

配置来源优先级：数据库 app_setting（界面写入） > 环境变量 > 内置默认。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

import httpx

from app import config

DEFAULT_DAILY_CAP = 200

_runtime: dict = {
    "api_key": config.LLM_API_KEY,
    "base_url": config.LLM_BASE_URL,
    "model": config.LLM_MODEL,
    "daily_cap": DEFAULT_DAILY_CAP,
}
_counter: dict = {"date": "", "used": 0}
_session_factory = None  # main 启动时注入；测试环境保持 None（计数不落盘）


@dataclass
class LLMResult:
    text: str
    source: str  # "llm" | "template"


def set_session_factory(factory) -> None:
    global _session_factory
    _session_factory = factory


def load_from_db(db) -> None:
    """启动时调用：把数据库里的配置与今日调用计数加载进内存。"""
    from app.models import AppSetting

    rows = {r.key: r.value for r in db.query(AppSetting).all()}
    if rows.get("llm_api_key"):
        _runtime["api_key"] = rows["llm_api_key"]
    if rows.get("llm_base_url"):
        _runtime["base_url"] = rows["llm_base_url"]
    if rows.get("llm_model"):
        _runtime["model"] = rows["llm_model"]
    if rows.get("llm_daily_cap"):
        try:
            _runtime["daily_cap"] = max(0, int(rows["llm_daily_cap"]))
        except ValueError:
            pass
    today = date.today().isoformat()
    _counter["date"], _counter["used"] = today, 0
    if rows.get("llm_counter_date") == today:
        try:
            _counter["used"] = int(rows.get("llm_counter_used", "0"))
        except ValueError:
            _counter["used"] = 0


def configure(api_key: str | None = None, base_url: str | None = None,
              model: str | None = None, daily_cap: int | None = None) -> None:
    """更新运行时配置（持久化由路由层写 app_setting 完成）。"""
    if api_key is not None:
        _runtime["api_key"] = api_key.strip()
    if base_url is not None:
        _runtime["base_url"] = base_url.strip() or config.LLM_BASE_URL
    if model is not None:
        _runtime["model"] = model.strip() or config.LLM_MODEL
    if daily_cap is not None:
        _runtime["daily_cap"] = max(0, int(daily_cap))


def _roll_counter() -> None:
    today = date.today().isoformat()
    if _counter.get("date") != today:
        _counter["date"], _counter["used"] = today, 0


def llm_available() -> bool:
    _roll_counter()
    return bool(_runtime.get("api_key")) and _counter["used"] < _runtime["daily_cap"]


def _bump_and_persist() -> None:
    _counter["used"] += 1
    if _session_factory is None:
        return
    try:
        from app.models import AppSetting

        db = _session_factory()
        try:
            for key, value in (("llm_counter_date", _counter["date"]),
                               ("llm_counter_used", str(_counter["used"]))):
                row = db.get(AppSetting, key)
                if row is None:
                    db.add(AppSetting(key=key, value=value))
                else:
                    row.value = value
            db.commit()
        finally:
            db.close()
    except Exception:
        pass  # 计数落盘失败不阻断主流程


def status() -> dict:
    """返回给配置界面/接口的状态（Key 打码，不回传明文）。"""
    _roll_counter()
    key = _runtime.get("api_key") or ""
    if len(key) > 8:
        masked = f"{key[:4]}…{key[-4:]}"
    else:
        masked = "已配置" if key else ""
    return {
        "configured": bool(key),
        "available": llm_available(),
        "model": _runtime["model"],
        "base_url": _runtime["base_url"],
        "api_key_masked": masked,
        "daily_cap": _runtime["daily_cap"],
        "used_today": _counter["used"],
    }


def chat(prompt: str, system: str = "", timeout: float = 20.0) -> LLMResult | None:
    """调用 OpenAI 兼容 chat 接口；未配置/超限/失败返回 None（调用方走降级）。"""
    if not llm_available():
        return None
    try:
        resp = httpx.post(
            f"{_runtime['base_url'].rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {_runtime['api_key']}"},
            json={
                "model": _runtime["model"],
                "messages": [m for m in ([{"role": "system", "content": system}] if system else [])
                             + [{"role": "user", "content": prompt}]],
                "temperature": 0.3,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()
        if not text:
            return None
        _bump_and_persist()
        return LLMResult(text=text, source="llm")
    except (httpx.HTTPError, KeyError, json.JSONDecodeError):
        return None
