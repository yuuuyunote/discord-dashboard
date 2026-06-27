"""
bot_only.py
Wispbyte用 Discord Bot のみ起動スクリプト
起動時に既存メンバーを自動インポート（初回のみ）
"""

import asyncio
import os
from datetime import datetime, timezone

import discord
from dotenv import load_dotenv

import database
from bot.events import setup_events
from bot.commands import setup_commands

load_dotenv(dotenv_path="/home/container/.env")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID      = int(os.getenv("GUILD_ID", "0"))

intents = discord.Intents.default()
intents.members         = True
intents.message_content = True
intents.presences       = True
intents.moderation      = True

bot = discord.Client(intents=intents)
setup_events(bot)
tree = setup_commands(bot)


async def _import_existing_members(guild: discord.Guild) -> None:
    """既存メンバーを一括インポート（DBに未登録のメンバーのみ）"""
    print("[Bot] 既存メンバーのインポートを開始...")
    count = 0
    skip  = 0

    async for member in guild.fetch_members(limit=None):
        if member.bot:
            skip += 1
            continue

        # 既にDBにいる場合はスキップ
        existing = database.get_user(str(member.id))
        if existing:
            skip += 1
            continue

        try:
            database.upsert_user(
                user_id=str(member.id),
                username=str(member),
                account_created=member.created_at.isoformat(),
                joined_at=member.joined_at.isoformat() if member.joined_at else None,
            )
            count += 1
        except Exception as e:
            print(f"[Bot] インポートエラー: {member} → {e}")

    print(f"[Bot] インポート完了: 新規登録 {count} 人 / スキップ {skip} 件")


async def _sync_commands() -> None:
    await bot.wait_until_ready()
    try:
        guild  = discord.Object(id=GUILD_ID)
        tree.copy_global_to(guild=guild)
        synced = await tree.sync(guild=guild)
        print(f"[Bot] スラッシュコマンド同期完了: {len(synced)}件")
    except Exception as e:
        print(f"[Bot] スラッシュコマンド同期エラー: {e}")


async def _on_ready_import() -> None:
    """起動時に既存メンバーをインポート"""
    await bot.wait_until_ready()
    guild = bot.get_guild(GUILD_ID)
    if guild:
        await _import_existing_members(guild)


async def main() -> None:
    database.init_db()
    print("[Bot] DB初期化完了")

    async with bot:
        bot.loop.create_task(_sync_commands())
        bot.loop.create_task(_on_ready_import())
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
