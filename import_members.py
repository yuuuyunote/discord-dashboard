"""
import_members.py
Discordサーバーの既存メンバーを全員DBに一括登録するスクリプト
ローカルPCで1回だけ実行する

実行方法：
  python import_members.py
"""

import asyncio
import os
from datetime import timezone

import discord
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID      = int(os.getenv("GUILD_ID", "0"))

# DB接続（database.pyと同じ）
import psycopg2
import psycopg2.extras

DATABASE_URL = os.getenv("DATABASE_URL", "")


def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def upsert_user(user_id: str, username: str, account_created: str, joined_at: str = None):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (user_id, username, account_created, joined_at, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT(user_id) DO UPDATE SET
                    username   = EXCLUDED.username,
                    updated_at = EXCLUDED.updated_at
            """, (user_id, username, account_created, joined_at, now))
        conn.commit()
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────
# メインBot処理
# ─────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.members = True

bot = discord.Client(intents=intents)


@bot.event
async def on_ready():
    print(f"[Import] ログイン成功: {bot.user}")

    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        print(f"[Import] エラー: GUILD_ID={GUILD_ID} のサーバーが見つかりません")
        await bot.close()
        return

    print(f"[Import] サーバー: {guild.name} | メンバー数: {guild.member_count}")
    print("[Import] メンバーの取得を開始します...")

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
                print(f"[Import] 進捗: {count} 人登録済み...")

        except Exception as e:
            print(f"[Import] エラー: {member} → {e}")
            error += 1

    print(f"\n[Import] 完了！")
    print(f"  登録: {count} 人")
    print(f"  スキップ（Bot）: {skip} 件")
    print(f"  エラー: {error} 件")

    await bot.close()


asyncio.run(bot.start(DISCORD_TOKEN))
