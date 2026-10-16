"""全局配置：路径、评分权重、工具权限开关。

设计原则（对标 dxl-commerce-agent）：
- 只读能力默认开启；涉及写操作/花费/外发的工具默认关闭，用环境变量逐个开。
- 评分权重集中在配置层，复盘模块输出的权重修正建议不直接生效，须人工确认后改这里。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOADS_DIR = BASE_DIR / "uploads"
DB_PATH = Path(os.environ.get("CONTENTOPS_DB", DATA_DIR / "contentops.db"))
DB_URL = os.environ.get("CONTENTOPS_DB_URL", f"sqlite:///{DB_PATH}")

# ============ 选题双引擎权重 ============
# 内容分（0-100）
CONTENT_WEIGHTS = {
    "finish_rate": 0.30,        # 预估完播率（脚本时长×钩子强度）
    "topic_heat": 0.20,         # 垂类热度（近30天同类选题发布量与互动中位数）
    "competition": 0.15,        # 竞争饱和度（反向：同类内容越多分越低）
    "audience_match": 0.20,     # 粉丝画像匹配
    "publish_window": 0.15,     # 发布时段权重
}
# 商品分（0-100）
PRODUCT_WEIGHTS = {
    "commission": 0.30,         # 佣金比例
    "conversion_history": 0.25, # 同类目历史转化
    "price_fit": 0.25,          # 价格带匹配粉丝消费力
    "return_risk": 0.20,        # 退货风险（反向）
}
# 合成：总分 = 内容分*0.6 + 商品分*0.4*车位系数（无挂车选题车位系数=0）
CONTENT_SHARE = 0.6
PRODUCT_SHARE = 0.4
CART_MULTIPLIER = 1.0

# 评分解释阈值：低于该分输出短板提示
WEAKNESS_THRESHOLD = 60

# ============ 合规过滤 ============
# P0=拦截（确定性违规），P1=高危（人工必审），P2=提示（低风险建议改写）
COMPLIANCE_LEVELS = ("P0", "P1", "P2")

# ============ LLM 可选接入（OpenAI 兼容，不配则模板降级） ============
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")

# ============ 工具权限开关（默认最小权限） ============
# 写操作与批量工具默认关闭；置 1 开启后仍走人工审批流。
ENABLE_APPROVAL_API = os.environ.get("CONTENTOPS_ENABLE_APPROVAL_API", "1") == "1"   # 审批本身默认开
ENABLE_COMMENT_DRAFT_TOOL = os.environ.get("CONTENTOPS_ENABLE_COMMENT_DRAFT_TOOL", "1") == "1"  # 评论草稿只读+落库
ENABLE_AUTO_PUBLISH = os.environ.get("CONTENTOPS_ENABLE_AUTO_PUBLISH", "0") == "1"  # 自动发布：默认禁止
ENABLE_WEIGHT_WRITEBACK = os.environ.get("CONTENTOPS_ENABLE_WEIGHT_WRITEBACK", "0") == "1"  # 权重自动回写：默认禁止
