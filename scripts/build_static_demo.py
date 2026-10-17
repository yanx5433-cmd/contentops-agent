"""生成 GitHub Pages 静态演示页 docs/（index/app.js/style.css/vendor + demo 层）。

前置：先运行 scripts/build_demo_snapshot.py 生成 docs/demo-data.js。
"""
from pathlib import Path
import shutil

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "app" / "static"
DOCS = REPO / "docs"

BANNER = """<div class="demo-banner">
  <b>在线演示（静态版）</b> · 评分引擎 / 合规检查 / 知识库检索 / 评论分类都在浏览器内真实运行，数据刷新即重置 ·
  <a href="https://github.com/yanx5433-cmd/contentops-agent" target="_blank">源码与完整后端版</a>
</div>
"""

REPO_LINK = "https://github.com/yanx5433-cmd/contentops-agent/blob/main/data/raw/"


def main() -> None:
    html = (SRC / "index.html").read_text(encoding="utf-8")
    html = html.replace(
        "<title>ContentOps · 带货内容运营工作台</title>",
        "<title>ContentOps · 带货内容运营工作台（在线演示）</title>",
    )
    html = html.replace('href="/style.css"', 'href="./style.css"')
    html = html.replace('<body>', '<body>\n' + BANNER)
    html = html.replace(
        '<script src="/vendor/echarts.min.js"></script>\n<script src="/app.js"></script>',
        '<script src="./demo-data.js"></script>\n'
        '<script src="./vendor/echarts.min.js"></script>\n'
        '<script src="./demo.js"></script>\n'
        '<script src="./app.js"></script>',
    )
    html = html.replace('href="/samples/videos_sample.csv"', f'href="{REPO_LINK}videos_sample.csv"')
    html = html.replace('href="/samples/products_sample.csv"', f'href="{REPO_LINK}products_sample.csv"')
    html = html.replace('href="/samples/sales_sample.csv"', f'href="{REPO_LINK}sales_sample.csv"')
    (DOCS / "index.html").write_text(html, encoding="utf-8")

    shutil.copyfile(SRC / "app.js", DOCS / "app.js")
    css = (SRC / "style.css").read_text(encoding="utf-8")
    css += """

/* ===== 静态演示版横幅 ===== */
.demo-banner {
  background: #1f2430; color: #ffd7e3; font-size: 12.5px; line-height: 1.6;
  padding: 8px 16px; text-align: center;
  font-family: system-ui, -apple-system, "Microsoft YaHei", "PingFang SC", sans-serif;
}
.demo-banner b { color: #ff9db8; }
.demo-banner a { color: #ffb3c8; }
"""
    (DOCS / "style.css").write_text(css, encoding="utf-8")

    vendor = DOCS / "vendor"
    vendor.mkdir(exist_ok=True)
    shutil.copyfile(SRC / "vendor" / "echarts.min.js", vendor / "echarts.min.js")
    print("docs/ 静态演示页生成完成")


if __name__ == "__main__":
    main()
