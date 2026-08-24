"""
bot/commands/__init__.py
スラッシュコマンド（Application Commands）のセットアップ。

discord.Client は commands.Bot と違い app_commands.CommandTree を持たないため、
ここで自分でツリーを組み立てる。全コマンドともユーザーIDのみで対象を指定する
（discord.Userの選択肢は廃止済み）。

起動時sync:
- GUILD_ID が設定されていれば、そのサーバー限定でコピー・sync（即時反映、開発中向け）
- 未設定ならグローバルsync（全サーバー反映まで最大1時間程度）
- discord.Client は commands.Bot と違い add_listener を持たないため、
  bot/events.py 側で既に設定された on_ready を保持したまま、後ろに
  コマンドsyncを繋いだ新しい on_ready で再代入する（上書きではなく連結）
"""

import os
from typing import Optional

import discord
from discord import app_commands

from bot.commands.check import handle_check
from bot.commands.report import handle_report

GUILD_ID = os.getenv("GUILD_ID")
MAINTAINER_CHANNEL_ID = os.getenv("MAINTAINER_CHANNEL_ID")


def setup_commands(bot: discord.Client) -> app_commands.CommandTree:
    tree = app_commands.CommandTree(bot)

    @tree.command(name="check", description="ユーザーIDが通報リストに載っているか確認する")
    @app_commands.describe(user_id="確認するユーザーID")
    async def check(interaction: discord.Interaction, user_id: str) -> None:
        await handle_check(interaction, user_id)

    @tree.command(name="report", description="悪質なユーザーを通報する")
    @app_commands.describe(
        user_id="通報するユーザーID",
        evidence_image="証拠画像（必須）",
        note="補足（任意）",
    )
    async def report(
        interaction: discord.Interaction,
        user_id: str,
        evidence_image: discord.Attachment,
        note: Optional[str] = None,
    ) -> None:
        if not MAINTAINER_CHANNEL_ID:
            await interaction.response.send_message(
                "MAINTAINER_CHANNEL_ID が設定されていないため /report は利用できません。",
                ephemeral=True,
            )
            return
        channel = interaction.client.get_channel(int(MAINTAINER_CHANNEL_ID))
        if channel is None:
            channel = await interaction.client.fetch_channel(int(MAINTAINER_CHANNEL_ID))
        await handle_report(interaction, user_id, evidence_image, note, channel)

    async def _sync_commands() -> None:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
            print(f"[commands] synced {len(synced)} command(s) to guild {GUILD_ID}")
        else:
            synced = await tree.sync()
            print(f"[commands] synced {len(synced)} command(s) globally")

    # discord.Client（commands.Bot と違い add_listener を持たない）で
    # bot/events.py 側の on_ready を上書きしないよう、既存のハンドラを
    # 保持しつつ後ろに繋いだ新しい on_ready を再代入する。
    # Client.dispatch() は毎回 getattr(self, "on_ready") を見に行くだけなので、
    # デコレータ経由でなく直接代入しても同じように呼び出される。
    original_on_ready = getattr(bot, "on_ready", None)

    async def _on_ready() -> None:
        if original_on_ready is not None:
            await original_on_ready()
        await _sync_commands()

    bot.on_ready = _on_ready

    return tree
