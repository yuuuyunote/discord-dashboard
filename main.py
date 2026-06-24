"""
main.py
Discord Bot（discord.py）と FastAPI（uvicorn）を
同一の asyncio イベントループで並行稼働させるメインスクリプト
"""

import asyncio
import os

import discord
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import database
from bot.events import setup_events
from routers import auth, api

# ─── 環境変数読み込み ──────────────────────────────────────
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
HOST          = os.getenv("HOST", "0.0.0.0")
PORT          = int(os.getenv("PORT", "8000"))


# ─── Discord Bot セットアップ ──────────────────────────────

intents = discord.Intents.default()
intents.members         = True   # SERVER MEMBERS INTENT
intents.message_content = True   # MESSAGE CONTENT INTENT
intents.presences       = True   # PRESENCE INTENT
intents.moderation      = True   # BAN / キック検知

bot = discord.Client(intents=intents)
setup_events(bot)  # bot/events.py のイベントを登録


# ─── FastAPI セットアップ ──────────────────────────────────

app = FastAPI(title="Community Dashboard")

# 静的ファイル（CSS）
app.mount("/static", StaticFiles(directory="static"), name="static")

# Jinja2 テンプレート
templates = Jinja2Templates(directory="templates")

# ルーター登録
app.include_router(auth.router)
app.include_router(api.router)

# テンプレートオブジェクトをルーターから参照できるよう共有
auth.templates = templates
api.templates  = templates
api.bot        = bot


# ─── 起動エントリーポイント ────────────────────────────────

async def main() -> None:
    # DB初期化（テーブルが無ければ作成）
    database.init_db()

    # uvicorn の設定
    config = uvicorn.Config(
        app=app,
        host=HOST,
        port=PORT,
        log_level="info",
    )
    server = uvicorn.Server(config)

    # Bot と Web サーバーを同一ループで並行起動
    await asyncio.gather(
        bot.start(DISCORD_TOKEN),
        server.serve(),
    )


if __name__ == "__main__":
    asyncio.run(main())
