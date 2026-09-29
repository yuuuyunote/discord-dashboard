"""
bot_only.py
Wispbyte / Pterodactyl 用 Discord Bot 起動スクリプト
残す機能：DM返信の受信、フォーラムスレッド キープアライブ、初回発言ロール、/report、匿名アイデア投稿パネル
"""

import asyncio
import logging
import os
import glob

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
from bot.suggestions import setup_suggestions

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
GUILD_ID_STR  = os.environ.get("GUILD_ID", "0")
GUILD_ID      = int(GUILD_ID_STR) if GUILD_ID_STR.strip().isdigit() else 0

# members: 初回発言ロール付与で guild.get_member を使うため必要。
# メッセージ内容/プレゼンス/監査ログの特権Intentは不要（DMの本文はIntentなしで取得できる）。
intents = discord.Intents.default()
intents.members = True

bot = discord.Client(intents=intents)
setup_events(bot)
# on_ready / on_message を連結する実装のため、setup_events の後に呼ぶこと
tree = setup_commands(bot)
setup_suggestions(bot, tree)


async def main() -> None:
    database.init_db()
    logger.info("DB初期化完了")

    async with bot:
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
