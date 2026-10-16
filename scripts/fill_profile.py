"""填写账号画像：粉丝画像、价格带、类目偏好、转化基线。

数据来源标注：
- [截图实录] 2026-10-09 抖音 App 数据中心 → 粉丝分析 → 粉丝画像 页面
- [估算]     后台没有现成数字，按账号定位/粉丝结构估，拿到后台真实数据后覆盖
- [自动]     运行时从已导入的 video_fact 计算

用法：
  1. 改手填区数字（每项注释了来源与去哪核对）
  2. 运行: .venv/Scripts/python scripts/fill_profile.py
  3. 重新给选题评分，audience_match / price_fit 即用真实数据
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.database import SessionLocal
from app.models import AccountProfile, VideoFact

# ======================= 手填区 =======================

PROFILE_NAME = "奶油草莓（女装穿搭带货号）"

# --- 粉丝画像 [截图实录 2026-10-09]：性别 女77% 男23% ---
FEMALE_RATIO = 0.77
# 年龄柱状图估读（<23 人数最多，柱高约55%）
AGE_DIST = {"<23": 0.55, "24-30": 0.31, "31-40": 0.08, "41-50": 0.03, ">50": 0.03}
# 城市分布（省份，柱状图估读）
CITY_DIST = {"广东": 0.13, "江苏": 0.08, "浙江": 0.07,
             "四川": 0.06, "河南": 0.06, "山东": 0.06, "湖北": 0.04}
CITIES_TOP3 = ["广东", "江苏", "浙江"]
# 设备与活跃度 [截图实录]
DEVICE_DIST = {"苹果": 0.53, "华为": 0.10, "小米": 0.07,
               "VIVO": 0.07, "OPPO": 0.06, "其他": 0.04, "荣耀": 0.04}
ACTIVITY_DIST = {"重度": 0.69, "低活": 0.23, "轻度": 0.04, "中度": 0.04}

# --- 类目偏好 [估算 → 待核对橱窗实际类目] ---
# 账号定位「分享漂亮衣服，同款在橱窗」。导入真实商品 CSV 后按橱窗类目改这里。
CATEGORY_PREF = {"女装": 0.45, "鞋包": 0.20, "饰品": 0.15, "美妆": 0.10}

# --- 粉丝消费力价格带 [估算 → 待核对] ---
# 截图里没有消费水平分布。核对位置：电商作者后台(diantoushi.com) → 人群分析 → 消费水平。
# 依据：<23 岁粉丝过半 + 重度粉丝 69%，先按学生党偏低客单结构估。
PRICE_BANDS = {"0-50": 0.50, "50-150": 0.35, "150+": 0.15}

# --- 平均转化率 [占位 → 待核对] ---
# 核对位置：我 → 电商带货 → 带货数据(近30天) → 成交单数 ÷ 商品点击数。
AVG_CVR = 0.020

# --- 完播率基线 [手动] ---
# 诊断页近7日完播率 2.4% 是停更期冷数据，不代表正常水平；先手动 0.25。
# 导入真实作品 CSV 后把下面两行改成 None，脚本自动从历史作品计算。
MANUAL_AVG_FINISH_RATE = 0.25
MANUAL_AVG_VIEW = 0     # 历史平均播放未知，先 0；导入真实 CSV 后改 None 自动算

# ===================== 写入逻辑（勿改） =====================

def compute_baselines(db) -> tuple[dict, str]:
    if MANUAL_AVG_FINISH_RATE is None or MANUAL_AVG_VIEW is None:
        videos = list(db.execute(select(VideoFact)).scalars())
        if videos:
            avg_finish = sum(v.finish_rate for v in videos) / len(videos)
            avg_view = sum(v.views for v in videos) / len(videos)
            return ({"avg_finish_rate": round(avg_finish, 4),
                     "avg_view": round(avg_view, 1), "avg_cvr": AVG_CVR},
                    f"自动计算自 {len(videos)} 条已导入视频")
        print("[提示] 库里没有视频数据，完播率回退默认 0.25")
        return {"avg_finish_rate": 0.25, "avg_view": 0, "avg_cvr": AVG_CVR}, "默认值（无视频数据）"
    return ({"avg_finish_rate": MANUAL_AVG_FINISH_RATE,
             "avg_view": MANUAL_AVG_VIEW, "avg_cvr": AVG_CVR},
            "手动指定（导入真实作品 CSV 后可改 None 自动算）")


def main() -> None:
    db = SessionLocal()
    try:
        baselines, src = compute_baselines(db)
        profile = db.execute(select(AccountProfile)).scalars().first()
        if profile is None:
            profile = AccountProfile(name=PROFILE_NAME)
            db.add(profile)
        profile.name = PROFILE_NAME
        profile.audience = {
            "female_ratio": FEMALE_RATIO,
            "age_dist": AGE_DIST,
            "city_dist": CITY_DIST,
            "cities_top3": CITIES_TOP3,
            "device_dist": DEVICE_DIST,
            "activity_dist": ACTIVITY_DIST,
            "category_pref": CATEGORY_PREF,
        }
        profile.price_bands = PRICE_BANDS
        profile.baselines = baselines
        db.commit()
        print(f"[完成] 画像已写入：{PROFILE_NAME}")
        for k, v in profile.audience.items():
            print(f"  {k:<14} = {v}")
        print(f"  price_bands   = {profile.price_bands}")
        print(f"  baselines     = {profile.baselines}")
        print(f"  基线来源       : {src}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
