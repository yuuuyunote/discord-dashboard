"""
import_members.py
Discordサーバーの既存メンバーを全員DBに一括登録するスクリプト
ローカルPCで1回だけ実行する

実行方法：
  python import_members.py
"""

import asyncio
import logging
import os

import discord
from dotenv import load_dotenv

from database import upsert_user

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID      = int(os.getenv("GUILD_ID", "0"))


# ─────────────────────────────────────────────────────────
# メインBot処理
# ─────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.members = True

bot = discord.Client(intents=intents)


@bot.event
async def on_ready():
    logger.info(f"ログイン成功: {bot.user}")

    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        logger.error(f"GUILD_ID={GUILD_ID} のサーバーが見つかりません")
        await bot.close()
        return

    logger.info(f"サーバー: {guild.name} | メンバー数: {guild.member_count}")
    logger.info("メンバーの取得を開始します...")

    count     = 0
    skip      = 0
    error     = 0

    async for member in guild.fetch_members(limit=None):
        if member.bot:
            skip += 1
            continue

        try:
            user_id         = str(member.id)
            username        = str(member)
            account_created = member.created_at.isoformat()
            joined_at       = member.joined_at.isoformat() if member.joined_at else None

            upsert_user(user_id, username, account_created, joined_at)
            count += 1

            if count % 100 == 0:
                logger.info(f"進捗: {count} 人登録済み...")

        except Exception as e:
            logger.error(f"エラー: {member} → {e}", exc_info=True)
            error += 1

    logger.info("完了！")
    logger.info(f"  登録: {count} 人")
    logger.info(f"  スキップ（Bot）: {skip} 件")
    logger.info(f"  エラー: {error} 件")

    await bot.close()


asyncio.run(bot.start(DISCORD_TOKEN))
