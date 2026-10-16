"""ContentOps Agent 应用入口。"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.database import Base, SessionLocal, engine
from app.routers import api
from app.services import llm


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 开发模式直接建表；生产用 alembic upgrade head（见 alembic/）
    Base.metadata.create_all(bind=engine)
    # 加载界面配置的 LLM 参数与今日调用计数，并允许调用计数落盘
    db = SessionLocal()
    try:
        llm.load_from_db(db)
    finally:
        db.close()
    llm.set_session_factory(SessionLocal)
    yield


app = FastAPI(title="ContentOps Agent", version="0.1.0",
              description="带货账号内容运营智能体：选题双引擎评分 + 合规过滤 + 人工审批 + 审计",
              lifespan=lifespan)

app.include_router(api.router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "contentops-agent"}


# 工作台前端（纯静态，无框架依赖）。必须最后挂载：
# mount("/") 会匹配所有未命中先注册路由的请求，/api、/docs、/health 均优先。
app.mount("/", StaticFiles(directory=str(Path(__file__).resolve().parent / "static"), html=True), name="static")
