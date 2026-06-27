"""
main.py
Discord Bot（discord.py）と FastAPI（uvicorn）を
同一の asyncio イベントループで並行稼働させるメインスクリプト
"""

import asyncio
import os
from datetime import datetime, timezone, timedelta

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
intents.members         = True
intents.message_content = True
intents.presences       = True
intents.moderation      = True

bot = discord.Client(intents=intents)
setup_events(bot)


# ─── FastAPI セットアップ ──────────────────────────────────

app = FastAPI(title="Community Dashboard")

app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")

# ─── カスタムフィルター（UTC→JST変換） ────────────────────

JST = timezone(timedelta(hours=9))

def to_jst(value: str, fmt: str = "%Y/%m/%d %H:%M") -> str:
    """ISO形式のUTC文字列をJST表示に変換"""
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(JST).strftime(fmt)
    except Exception:
        return str(value)[:16].replace("T", " ")

templates.env.filters["jst"] = to_jst

# ルーター登録
app.include_router(auth.router)
app.include_router(api.router)

auth.templates = templates
api.templates  = templates
api.bot        = bot


# ─── 起動エントリーポイント ────────────────────────────────

async def main() -> None:
    database.init_db()

    config = uvicorn.Config(
        app=app,
        host=HOST,
        port=PORT,
        log_level="info",
    )
    server = uvicorn.Server(config)

    await asyncio.gather(
        bot.start(DISCORD_TOKEN),
        server.serve(),
    )


if __name__ == "__main__":
    asyncio.run(main())
