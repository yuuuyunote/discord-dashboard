"""
bot_only.py
Wispbyte用 Discord Bot のみ起動スクリプト
FastAPIは起動しない・Botイベント処理のみ
"""

import asyncio
import os

import discord
from dotenv import load_dotenv

import database
from bot.events import setup_events

# ─── 環境変数読み込み ──────────────────────────────────────
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")

# ─── Discord Bot セットアップ ──────────────────────────────

intents = discord.Intents.default()
intents.members         = True
intents.message_content = True
intents.presences       = True
intents.moderation      = True

bot = discord.Client(intents=intents)
setup_events(bot)


# ─── 起動エントリーポイント ────────────────────────────────

async def main() -> None:
    # DB初期化（テーブルが無ければ作成）
    database.init_db()
    print("[Bot] DB初期化完了")

    # Botのみ起動
    await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
