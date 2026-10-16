"""合规过滤：内容侧违禁词三级分级 + 商品侧资质检查。

对标 cn-ecommerce-ops 的 P0/P1/P2 分级与 dxl 的"写操作默认关"哲学：
P0 确定性拦截（不进审批），P1 强制人工审批，P2 仅提示。
词库为种子版本，可由数据文件扩充；不构成法律意见，发布前以平台官方规则为准。
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import ComplianceHit, ProductFact, Topic

# 内容侧违禁词库（种子版，带货内容高频风险）
BANNED_WORDS: dict[str, list[str]] = {
    "P0": [
        "最好", "第一", "顶级", "国家级", "世界级", "最高级", "最佳",
        "治愈", "根治", "百分百有效", "包治百病", "无效退款",
        "全网最低", "史上最低", "秒杀全网",
        "国家级认证", "免检产品",
    ],
    "P1": [
        "美白", "祛斑", "减肥", "瘦身", "丰胸", "抗衰老",
        "绝对", "永久", "彻底根除", "无任何副作用",
        "限量秒杀", "仅剩最后", "错过再无",
    ],
    "P2": [
        "神器", "爆款", "必备", "人手一件", "闭眼入", "无脑冲",
        "专供", "特供",
    ],
}

# 商品类目 -> 需要的经营资质（种子版）
CATEGORY_LICENSES: dict[str, list[str]] = {
    "食品": ["食品经营许可证"],
    "保健食品": ["食品经营许可证（保健食品）"],
    "化妆品": ["化妆品经营备案"],
    "医疗器械": ["医疗器械经营许可证"],
}

# 改写建议（种子版）：命中词 -> 可替换/处理建议。文案体检用。
REWRITE_HINTS: dict[str, str] = {
    # P0
    "最好": "改为「很合适 / 亲测好用」等主观体验表述",
    "第一": "改为「同类里很出色的」，避免排名承诺",
    "顶级": "去掉等级词，改用具体事实（面料/工艺）",
    "国家级": "删除，等级认定类词不可出现在带货文案",
    "世界级": "删除，改为具体可验证的卖点",
    "最高级": "删除，绝对化等级词",
    "最佳": "改为「很出彩」等主观表述",
    "治愈": "普通商品不可用医疗功效词，改为「穿着很放松」等体验描述",
    "根治": "医疗功效词，普通商品禁用",
    "百分百有效": "删除效果承诺，改为「实测感受因人而异」",
    "包治百病": "医疗功效词，普通商品禁用",
    "无效退款": "删除，售后承诺以平台规则为准",
    "全网最低": "删除价格承诺，改为「到手价 59 元」等事实价格",
    "史上最低": "删除价格承诺",
    "秒杀全网": "删除，避免绝对化价格表述",
    "国家级认证": "删除，资质表述以商品页证照为准",
    "免检产品": "删除，免检类表述不可宣传",
    # P1
    "美白": "改为「提亮肤色」仍需谨慎，建议直接描述使用感受",
    "祛斑": "功效词需资质，建议移到商品详情页由资质页面承载",
    "减肥": "改为「轻食搭配 / 体重管理记录」等中性表述，避免功效承诺",
    "瘦身": "改为「显瘦」等服装版型描述（仅服饰类可用）",
    "丰胸": "删除，功效词",
    "抗衰老": "改为「使用感滋润」等体验描述",
    "绝对": "改为「基本 / 大概率」等软化表述",
    "永久": "改为「长时间保持」等有限表述",
    "彻底根除": "删除，效果承诺",
    "无任何副作用": "删除，安全性承诺需谨慎",
    "限量秒杀": "改为「限时活动（以页面为准）」，避免饥饿营销表述",
    "仅剩最后": "删除，库存紧迫话术风险高",
    "错过再无": "删除，避免误导性紧迫话术",
    # P2
    "神器": "改为「好物 / 小帮手」",
    "爆款": "改为「热门款 / 很多人在用」",
    "必备": "改为「实用推荐」",
    "人手一件": "改为「出镜率很高」",
    "闭眼入": "改为「适合新手参考」",
    "无脑冲": "改为「可以放心入」",
    "专供": "改为「定制款」，专供类表述谨慎",
    "特供": "改为「定制款」，特供类表述谨慎",
}


@dataclass
class ComplianceResult:
    blocked: bool                      # True = P0 拦截，不进审批
    hits: list[ComplianceHit]          # 命中记录（调用方落库）
    levels: set[str]

    @property
    def need_manual_review(self) -> bool:
        return "P1" in self.levels


def check_text(text: str) -> list[tuple[str, str, str]]:
    """返回 [(level, word, context)]，context 为命中词前后各 10 字。"""
    found: list[tuple[str, str, str]] = []
    for level in ("P0", "P1", "P2"):
        for word in BANNED_WORDS[level]:
            idx = text.find(word)
            if idx >= 0:
                start, end = max(0, idx - 10), min(len(text), idx + len(word) + 10)
                found.append((level, word, text[start:end]))
    return found


def check_text_spans(text: str) -> list[dict]:
    """文案体检用：返回全部命中（含出现位置与改写建议），按位置排序。

    与 check_text 的区别：同一词的所有出现都返回（不只是第一次），
    且带 start/end 供前端高亮、带 hint 供改写参考。
    """
    action_of = {"P0": "block", "P1": "manual_review", "P2": "rewrite_hint"}
    spans: list[dict] = []
    for level in ("P0", "P1", "P2"):
        for word in BANNED_WORDS[level]:
            start = text.find(word)
            while start >= 0:
                end = start + len(word)
                ctx_lo, ctx_hi = max(0, start - 10), min(len(text), end + 10)
                spans.append({
                    "level": level,
                    "word": word,
                    "start": start,
                    "end": end,
                    "context": text[ctx_lo:ctx_hi],
                    "action": action_of[level],
                    "hint": REWRITE_HINTS.get(word, ""),
                })
                start = text.find(word, end)
    spans.sort(key=lambda s: (s["start"], s["end"]))
    return spans


def check_licenses(product: ProductFact | None) -> list[tuple[str, str]]:
    """商品资质检查：返回 [(缺失资质, 类目)]。"""
    if product is None:
        return []
    for cat_prefix, licenses in CATEGORY_LICENSES.items():
        if product.category and product.category.startswith(cat_prefix):
            have = set(product.required_licenses or [])
            return [(lic, product.category) for lic in licenses if lic not in have]
    return []


def review_topic(db: Session, topic: Topic) -> ComplianceResult:
    """对选题做全量合规检查并落库。P0 命中直接置 rejected。"""
    text = f"{topic.title}\n{topic.outline}"
    hits: list[ComplianceHit] = []
    levels: set[str] = set()
    blocked = False

    for level, word, context in check_text(text):
        action = {"P0": "block", "P1": "manual_review", "P2": "rewrite_hint"}[level]
        levels.add(level)
        if level == "P0":
            blocked = True
        hits.append(ComplianceHit(
            topic_id=topic.id, level=level, kind="banned_word",
            matched=word, context=context, action=action,
        ))

    for missing, cat in check_licenses(topic.product):
        levels.add("P0")
        blocked = True
        hits.append(ComplianceHit(
            topic_id=topic.id, level="P0", kind="license_missing",
            matched=missing, context=f"类目[{cat}]需要资质：{missing}", action="block",
        ))

    for h in hits:
        db.add(h)
    if blocked:
        topic.status = "rejected"
    db.flush()
    return ComplianceResult(blocked=blocked, hits=hits, levels=levels)
