"""评论运营助手：意图四分类 + 话术库草稿。

设计（对标 pinduoduo-customer-agent 的场景化思路，但只出草稿不发送）：
- 意图分类：优先关键词规则（确定性），LLM 可用时做辅助复核；
- 草稿从话术库模板生成，禁止编造价格/库存/活动信息——
  话术只做引导（主页橱窗/私信/客服），具体商品信息以平台页面为准；
- 所有草稿落库带 intent/source 字段，人工可改可弃。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.services import llm

INTENTS = ("purchase", "product_qa", "complaint", "cooperation", "other")
INTENT_LABELS = {
    "purchase": "购买意向", "product_qa": "商品咨询",
    "complaint": "质疑差评", "cooperation": "合作洽谈", "other": "其他",
}

# 关键词规则（先匹配先得，顺序即优先级）
_RULES: list[tuple[str, list[str]]] = [
    ("cooperation", ["合作", "商务", "寄拍", "推广", "星图", "置换", "合作方", "mcn", "投放"]),
    ("complaint", ["太差", "垃圾", "骗子", "退货", "退款", "假货", "被骗", "踩雷", "翻车",
                   "不值", "色差", "缩水", "质量差", "失望",
                   "起球了", "掉色了", "破了"]),
    ("product_qa", ["尺码", "多大", "多少斤", "身高", "体重", "什么码", "穿多大", "几码",
                    "有货", "材质", "会不会", "掉毛", "薄不薄", "厚不厚", "洗",
                    "显黑", "显白", "显瘦", "孕", "码数", "偏大", "偏小", "面料", "成分"]),
    ("purchase", ["怎么买", "在哪买", "想要", "种草", "链接", "同款", "多少钱",
                  "价格", "上车", "冲了", "已拍", "好看", "爱了", "安排", "下单", "橱窗"]),
]


@dataclass
class CommentDraftResult:
    intent: str
    intent_label: str
    confidence: str                 # high(规则命中) / medium(默认/LLM辅助)
    draft: str
    source: str                     # template / llm
    rule_matched: str | None = None
    notes: list[str] = field(default_factory=list)


def classify_intent(text: str) -> tuple[str, str | None]:
    """返回 (intent, 命中词)。未命中返回 ("other", None)。"""
    for intent, words in _RULES:
        for w in words:
            if w in text:
                return intent, w
    return "other", None


# 话术模板：{comment}为原评论。只做引导，不承诺任何具体价格/库存/活动。
_TEMPLATES: dict[str, str] = {
    "purchase": "谢谢宝子的喜欢呀～同款已经放在主页橱窗了，点击头像就能看到，喜欢的话可以直接拍，有任何问题随时问我 💕",
    "product_qa": "宝子的问题我看到啦～具体的尺码和材质信息都在商品详情页里写得比较全，可以点进橱窗看一下；如果还有拿不准的，评论区或私信我都可以，我帮你参考～",
    "complaint": "非常抱歉给你带来了不好的体验 😢 麻烦私信我一下订单信息，我帮你核实处理；如果是尺码/色差问题，我们对照详情页确认后该退换退换，一定给你一个满意的处理方案。",
    "cooperation": "感谢品牌方的认可～商务合作可以私信我说明品牌、品类的预算与档期，我们后台对接会尽快回复，感谢支持！",
    "other": "谢谢宝子的留言呀～有想看的内容或者想问的都可以评论区告诉我，我会认真看的～",
}


def generate_draft(comment: str) -> CommentDraftResult:
    intent, matched = classify_intent(comment)

    if intent == "other" and llm.llm_available():
        # 规则未命中时让 LLM 辅助分类（仅分类，不生成内容）
        system = ("把抖音女装带货账号视频下的一条粉丝评论分类为以下之一，只输出单词："
                  "purchase（购买意向）/ product_qa（商品咨询）/ complaint（质疑差评）/ "
                  "cooperation（合作洽谈）/ other（其他）")
        result = llm.chat(f"评论：{comment}", system=system)
        if result and result.text.strip().lower() in INTENTS:
            intent = result.text.strip().lower()
            return CommentDraftResult(
                intent=intent, intent_label=INTENT_LABELS[intent],
                confidence="medium", draft=_TEMPLATES[intent],
                source=result.source, rule_matched=None,
                notes=["LLM 辅助分类（规则未命中）"],
            )

    return CommentDraftResult(
        intent=intent, intent_label=INTENT_LABELS[intent],
        confidence="high" if matched else "medium",
        draft=_TEMPLATES[intent], source="template",
        rule_matched=matched,
    )
