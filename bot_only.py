"""
bot_only.py
Wispbyte用 Discord Bot のみ起動スクリプト
"""

import asyncio
import os

import discord

# .envファイルを明示的に読み込む（複数パスを試みる）
def _load_env():
    paths = [
        "/home/container/.env",
        ".env",
        "/app/.env",
    ]
    for path in paths:
        if os.path.exists(path):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = val
            print(f"[Bot] .envを読み込みました: {path}")
            return
    print("[Bot] .envファイルが見つかりませんでした（環境変数を直接使用）")

_load_env()

# 環境変数を読み込んだ後にimport
import database
from bot.events import setup_events
from bot.commands import setup_commands

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
GUILD_ID_STR  = os.environ.get("GUILD_ID", "0")
GUILD_ID      = int(GUILD_ID_STR) if GUILD_ID_STR.isdigit() else 0

print(f"[Bot] GUILD_ID={GUILD_ID}")
print(f"[Bot] TOKEN先頭6文字={DISCORD_TOKEN[:6] if DISCORD_TOKEN else 'なし'}")

intents = discord.Intents.default()
intents.members         = True
intents.message_content = True
intents.presences       = True
intents.moderation      = True

bot = discord.Client(intents=intents)
setup_events(bot)
tree = setup_commands(bot)


async def _import_existing_members(guild: discord.Guild) -> None:
    print("[Bot] 既存メンバーのインポートを開始...")
    count = 0
    skip  = 0

    async for member in guild.fetch_members(limit=None):
        if member.bot:
            skip += 1
            continue

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


async def _on_ready_tasks() -> None:
    await bot.wait_until_ready()
    guild = bot.get_guild(GUILD_ID)
    if guild:
        print(f"[Bot] ギルド取得成功: {guild.name}")
        await _import_existing_members(guild)
    else:
        print(f"[Bot] ギルド取得失敗: GUILD_ID={GUILD_ID}")


async def main() -> None:
    database.init_db()
    print("[Bot] DB初期化完了")

    async with bot:
        bot.loop.create_task(_sync_commands())
        bot.loop.create_task(_on_ready_tasks())
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
