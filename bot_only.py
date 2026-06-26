"""
bot_only.py
Wispbyte用 Discord Bot のみ起動スクリプト
"""

import asyncio
import os

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


@bot.event
async def on_ready_sync():
    pass


# スラッシュコマンドを同期するためon_readyをラップ
_original_on_ready = bot.event.__self__ if hasattr(bot.event, '__self__') else None


async def _sync_commands():
    await bot.wait_until_ready()
    try:
        guild = discord.Object(id=GUILD_ID)
        tree.copy_global_to(guild=guild)
        synced = await tree.sync(guild=guild)
        print(f"[Bot] スラッシュコマンド同期完了: {len(synced)}件")
    except Exception as e:
        print(f"[Bot] スラッシュコマンド同期エラー: {e}")


async def main() -> None:
    database.init_db()
    print("[Bot] DB初期化完了")

    async with bot:
        bot.loop.create_task(_sync_commands())
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
