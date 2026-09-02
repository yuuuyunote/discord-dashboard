"""
bot/commands/__init__.py
スラッシュコマンド（Application Commands）のセットアップ。

discord.Client は commands.Bot と違い app_commands.CommandTree を持たないため、
ここで自分でツリーを組み立てる。

target_typeでuser/server/botを切り替える（サブコマンド化はせず、単一の
/report, /checkに引数として持たせる設計）。typing.Literalを使うとdiscord.py側で
自動的にドロップダウン選択肢になる。

user-installable app対応（discord.py 2.4+）:
allowed_installs(guilds=True, users=True) — サーバーへの導入・個人アカウントへの
導入の両方を許可する。
allowed_contexts(guilds=True, dms=True, private_channels=True) — サーバー内・DM・
グループDM等の非公式チャンネルの全部で実行可能にする。
/report, /check はどちらもGitHub上のデータ参照とDB書き込みだけで、実行元のサーバーに
依存する処理（ロール付与など）が無いため、両方とも全許可にしている。

起動時sync:
- GUILD_ID が設定されていれば、そのサーバー限定でコピー・sync（即時反映、開発中向け）
  ただし、この経路ではuser-install/DM実行の動作確認はできない
  （ギルド限定コピーはそのギルド内でしか使えないため）。DM・未導入サーバーからの
  動作確認をしたい場合はGUILD_IDを外してグローバルsyncする必要がある
  （反映まで最大1時間程度）。
- 未設定ならグローバルsync（全サーバー・DM含めて反映まで最大1時間程度）
- discord.Client は commands.Bot と違い add_listener を持たないため、
  bot/events.py 側で既に設定された on_ready を保持したまま、後ろに
  コマンドsyncを繋いだ新しい on_ready で再代入する（上書きではなく連結）
"""

import os
from typing import Literal, Optional

import discord
from discord import app_commands

from bot.commands.check import handle_check
from bot.commands.report import handle_report

GUILD_ID = os.getenv("GUILD_ID")
MAINTAINER_CHANNEL_ID = os.getenv("MAINTAINER_CHANNEL_ID")

TargetType = Literal["user", "server", "bot"]


def setup_commands(bot: discord.Client) -> app_commands.CommandTree:
    tree = app_commands.CommandTree(bot)

    @tree.command(name="check", description="ユーザー/サーバー/Botが通報リストに載っているか確認する")
    @app_commands.describe(
        target_type="確認する対象の種類",
        target_id="確認するID（ユーザーID / サーバーID / BotのユーザーID）",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def check(
        interaction: discord.Interaction,
        target_type: TargetType,
        target_id: str,
    ) -> None:
        await handle_check(interaction, target_type, target_id)

    @tree.command(name="report", description="悪質なユーザー/サーバー/Botを通報する")
    @app_commands.describe(
        target_type="通報する対象の種類",
        target_id="通報するID（ユーザーID / サーバーID / BotのユーザーID）",
        evidence_image="証拠画像（必須）",
        note="補足（任意）",
        server_name="対象がサーバーの場合のサーバー名（サーバー通報時は必須。Botは対象サーバーに未参加のため自動取得できません）",
        related_id="サーバーの場合は作成者のユーザーID、Botの場合は開発者のユーザーID（任意・分かる範囲で）",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def report(
        interaction: discord.Interaction,
        target_type: TargetType,
        target_id: str,
        evidence_image: discord.Attachment,
        note: Optional[str] = None,
        server_name: Optional[str] = None,
        related_id: Optional[str] = None,
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
        await handle_report(
            interaction,
            target_type,
            target_id,
            evidence_image,
            note,
            server_name,
            related_id,
            channel,
        )

    async def _sync_commands() -> None:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
            print(f"[commands] synced {len(synced)} command(s) to guild {GUILD_ID}")
        else:
            synced = await tree.sync()
            print(f"[commands] synced {len(synced)} command(s) globally")

    original_on_ready = getattr(bot, "on_ready", None)

    async def _on_ready() -> None:
        if original_on_ready is not None:
            await original_on_ready()
        await _sync_commands()

    bot.on_ready = _on_ready

    return tree
