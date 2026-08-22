"""
bot/commands/__init__.py
スラッシュコマンド（Application Commands）のセットアップ。

discord.Client は commands.Bot と違い app_commands.CommandTree を持たないため、
ここで自分でツリーを組み立てる。/check /report を追加する際は、この下に
bot/commands/check.py, bot/commands/report.py を増やして
@tree.command(...) を追加していく想定。

起動時sync:
- GUILD_ID が設定されていれば、そのサーバー限定でコピー・sync（即時反映、開発中向け）
- 未設定ならグローバルsync（全サーバー反映まで最大1時間程度）
- bot/events.py に既存の on_ready があっても、add_listener は同一イベントに
  複数のリスナーを追加できるため上書きしない
"""

import os

import discord
from discord import app_commands

from bot.commands.idlookup import handle_idlookup

GUILD_ID = os.getenv("GUILD_ID")


def setup_commands(bot: discord.Client) -> app_commands.CommandTree:
    tree = app_commands.CommandTree(bot)

    @tree.command(name="idlookup", description="メッセージリンクから投稿者のユーザーID・ユーザー名を調べる")
    @app_commands.describe(
        message_link="対象メッセージのリンク（メッセージを右クリック/長押し→「メッセージリンクをコピー」）"
    )
    async def idlookup(interaction: discord.Interaction, message_link: str) -> None:
        await handle_idlookup(interaction, message_link)

    async def _sync_on_ready() -> None:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
            print(f"[commands] synced {len(synced)} command(s) to guild {GUILD_ID}")
        else:
            synced = await tree.sync()
            print(f"[commands] synced {len(synced)} command(s) globally")

    bot.add_listener(_sync_on_ready, "on_ready")

    return tree
