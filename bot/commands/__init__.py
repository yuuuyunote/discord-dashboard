"""
bot/commands/__init__.py
スラッシュコマンド（Application Commands）のセットアップ。

discord.Client は commands.Bot と違い app_commands.CommandTree を持たないため、
ここで自分でツリーを組み立てる。

target_typeでuser/server/botを切り替える（サブコマンド化はせず、単一の
/reportに引数として持たせる設計）。typing.Literalを使うとdiscord.py側で
自動的にドロップダウン選択肢になる。

user-installable app対応は廃止済み。/report はギルド内でのみ実行可能
（allowed_installs/allowed_contexts は付与しない = discord.py既定のギルド専用挙動）。

このBotはGuide Base +サーバー（GUILD_ID）でしか使わない前提のため、コマンドは
常にそのサーバー限定で登録する。グローバル同期は行わない
（全サーバーへの反映は不要な上、反映まで最大1時間かかり開発時の確認に不向き）。
GUILD_IDは events.py 等がメンバー追跡に使っているものと同じ環境変数を流用する。

過去に「開発中はCOMMAND_SYNC_GUILD_IDでギルド限定、本番は未設定でグローバル」と
切り替えていた時期があり、その名残でグローバル側に古いコマンドが登録されたまま
残っていた（同名コマンドの重複表示の原因になっていた）。ギルド限定オンリーに
統一した今回、起動のたびにグローバル側を明示的にクリアして、その残骸も掃除する。

discord.Client は commands.Bot と違い add_listener を持たないため、
bot/events.py 側で既に設定された on_ready を保持したまま、後ろに
コマンドsyncを繋いだ新しい on_ready で再代入する（上書きではなく連結）
"""

import logging
import os
from typing import Literal, Optional

import discord
from discord import app_commands

from bot.commands.report import handle_report

logger = logging.getLogger(__name__)

MAINTAINER_CHANNEL_ID = os.getenv("MAINTAINER_CHANNEL_ID")

# コマンドを登録する唯一のサーバー（Guide Base +）。
# アプリ全体のメンバー追跡用 GUILD_ID と同じ値を流用する —— 詳しくは上のdocstring参照。
GUILD_ID_STR = os.getenv("GUILD_ID", "0")
GUILD_ID = int(GUILD_ID_STR) if GUILD_ID_STR.strip().isdigit() else 0

TargetType = Literal["user", "server", "bot"]


def setup_commands(bot: discord.Client) -> app_commands.CommandTree:
    tree = app_commands.CommandTree(bot)

    @tree.command(name="report", description="悪質なユーザー/サーバー/Botを通報する")
    @app_commands.describe(
        target_type="通報する対象の種類",
        target_id="通報するID（ユーザーID / サーバーID・招待リンク / BotのユーザーID）",
        evidence_image="証拠画像（必須）",
        note="補足（任意）",
        related_id="サーバーの場合は作成者のユーザーID、Botの場合は開発者のユーザーID（任意・分かる範囲で）",
    )
    async def report(
        interaction: discord.Interaction,
        target_type: TargetType,
        target_id: str,
        evidence_image: discord.Attachment,
        note: Optional[str] = None,
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
            related_id,
            channel,
        )

    async def _sync_commands() -> None:
        if not GUILD_ID:
            logger.error("GUILD_ID が未設定のため、コマンドを同期できません。")
            return

        guild = discord.Object(id=GUILD_ID)
        tree.copy_global_to(guild=guild)
        synced = await tree.sync(guild=guild)
        logger.info(f"synced {len(synced)} command(s) to guild {GUILD_ID}")

        # 過去にグローバル同期していた名残（同名コマンドの重複表示の原因）を掃除する。
        # ここでクリアしても、ギルド限定側は上ですでに同期済みなのでコマンド自体は消えない。
        tree.clear_commands(guild=None)
        await tree.sync()
        logger.info("cleared stale global command copies")

    original_on_ready = getattr(bot, "on_ready", None)

    async def _on_ready() -> None:
        if original_on_ready is not None:
            await original_on_ready()
        await _sync_commands()

    bot.on_ready = _on_ready

    return tree
