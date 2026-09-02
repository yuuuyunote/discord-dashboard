"""
bot_only.py
Wispbyte用 Discord Bot のみ起動スクリプト
起動時に既存メンバーをusersテーブルのみに登録（join_logsには追加しない）
"""

import asyncio
import logging
import os
import glob
from typing import Optional

import discord

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _load_env():
    found = glob.glob("/home/**/.env", recursive=True) + glob.glob(".env")
    logger.info(f"見つかった.envファイル: {found}")
    paths = ["/home/container/.env", "/home/user/.env", ".env", "/app/.env"]
    for path in paths:
        exists = os.path.exists(path)
        logger.debug(f"パス確認: {path} → {'存在する' if exists else '存在しない'}")
        if exists:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    os.environ[key] = val
            logger.info(f".envを読み込みました: {path}")
            return
    logger.warning(".envファイルが見つかりませんでした")


_load_env()

import database
from bot.events import setup_events
from bot.commands import setup_commands

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
GUILD_ID_STR  = os.environ.get("GUILD_ID", "0")
GUILD_ID      = int(GUILD_ID_STR) if GUILD_ID_STR.strip().isdigit() else 0

logger.info(f"GUILD_ID={GUILD_ID}")
logger.debug(f"TOKEN先頭6文字={DISCORD_TOKEN[:6] if DISCORD_TOKEN else 'なし'}")

intents = discord.Intents.default()
intents.members         = True
intents.message_content = True
intents.presences       = True
intents.moderation      = True

bot = discord.Client(intents=intents)
setup_events(bot)
tree = setup_commands(bot)


async def _import_existing_members(guild: discord.Guild) -> None:
    """
    既存メンバーを users テーブルのみに登録する。
    join_logs には追加しない（重複防止）。

    以前は1人ずつ database.get_user() / upsert_user() を同期呼び出ししていた。
    psycopg2は同期・ブロッキングで呼び出しごとに新規接続を張るため、669人分を
    直列に処理するとその間ずっとイベントループが止まり、Discordへのハートビート
    応答が途切れて「オフライン」と判定される原因になっていた。
    メンバー情報はメモリ上に集めるだけにし、DB書き込みは1回のバルクINSERTに
    まとめ、それも asyncio.to_thread() で別スレッドに逃がしている。
    """
    logger.info("既存メンバーのインポートを開始...")

    rows: list[tuple[str, str, str, Optional[str]]] = []
    skip = 0

    async for member in guild.fetch_members(limit=None):
        if member.bot:
            skip += 1
            continue
        rows.append((
            str(member.id),
            str(member),
            member.created_at.isoformat(),
            member.joined_at.isoformat() if member.joined_at else None,
        ))

    inserted = await asyncio.to_thread(database.insert_new_users_bulk, rows)
    skip += len(rows) - inserted

    logger.info(f"インポート完了: 新規登録 {inserted} 人 / スキップ {skip} 件")


async def _sync_commands() -> None:
    await bot.wait_until_ready()
    try:
        guild  = discord.Object(id=GUILD_ID)
        tree.copy_global_to(guild=guild)
        synced = await tree.sync(guild=guild)
        logger.info(f"スラッシュコマンド同期完了: {len(synced)}件")
    except Exception as e:
        logger.error(f"スラッシュコマンド同期エラー: {e}", exc_info=True)


async def _on_ready_tasks() -> None:
    await bot.wait_until_ready()
    guild = bot.get_guild(GUILD_ID)
    if guild:
        logger.info(f"ギルド取得成功: {guild.name}")
        await _import_existing_members(guild)
    else:
        logger.error(f"ギルド取得失敗: GUILD_ID={GUILD_ID}")


async def main() -> None:
    database.init_db()
    logger.info("DB初期化完了")

    async with bot:
        bot.loop.create_task(_sync_commands())
        bot.loop.create_task(_on_ready_tasks())
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
