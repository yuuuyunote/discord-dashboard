"""
bot/commands/report.py
/report: 悪質なユーザー/サーバー/Botを通報する。

target_typeで分岐:
- user / bot: fetch_userで実在確認する（Botもuserオブジェクトとして取得できる）。
  target_type=="bot"なのに実際はBotでない、あるいはその逆の場合は不整合として弾く。
- server: Botは対象サーバーに参加していないのが前提のため fetch_guild では
  実在確認できないことが多い。そのため server_name を必須の手入力にし、
  fetch_guild が偶然成功した場合のみ名前の自動更新に使う（失敗しても続行）。
"""

from typing import Optional

import discord

from bot.ui.report_flow import CategoryConsentView

_SNOWFLAKE_MIN_LEN, _SNOWFLAKE_MAX_LEN = 17, 20


def _is_snowflake(value: str) -> bool:
    return value.isdigit() and _SNOWFLAKE_MIN_LEN <= len(value) <= _SNOWFLAKE_MAX_LEN


async def handle_report(
    interaction: discord.Interaction,
    target_type: str,
    target_id: str,
    evidence_image: discord.Attachment,
    note: Optional[str],
    server_name: Optional[str],
    related_id: Optional[str],
    maintainer_channel: discord.abc.Messageable,
) -> None:
    if evidence_image.content_type is None or not evidence_image.content_type.startswith("image/"):
        await interaction.response.send_message(
            "evidence_image には画像ファイルを添付してください。", ephemeral=True
        )
        return

    target_id_str = target_id.strip()
    if not _is_snowflake(target_id_str):
        await interaction.response.send_message(
            f"`{target_id_str}` はDiscordのIDとして正しい形式ではありません（17〜20桁の数字）。",
            ephemeral=True,
        )
        return

    if related_id is not None:
        related_id = related_id.strip()
        if not _is_snowflake(related_id):
            await interaction.response.send_message(
                f"related_id `{related_id}` はDiscordのユーザーIDとして正しい形式ではありません（17〜20桁の数字）。",
                ephemeral=True,
            )
            return

    await interaction.response.defer(ephemeral=True)

    if target_type in ("user", "bot"):
        try:
            fetched = await interaction.client.fetch_user(int(target_id_str))
        except discord.NotFound:
            await interaction.followup.send(
                f"ユーザーID `{target_id_str}` は存在しないようです。", ephemeral=True
            )
            return

        if target_type == "bot" and not fetched.bot:
            await interaction.followup.send(
                f"`{target_id_str}` はBotアカウントではないようです。"
                "ユーザーとして通報する場合は target_type を「user」にしてください。",
                ephemeral=True,
            )
            return
        if target_type == "user" and fetched.bot:
            await interaction.followup.send(
                f"`{target_id_str}` はBotアカウントのようです。"
                "Botとして通報する場合は target_type を「bot」にしてください。",
                ephemeral=True,
            )
            return

        display_id = str(fetched.id)
        display_name = fetched.name

    else:  # server
        if not server_name or not server_name.strip():
            await interaction.followup.send(
                "サーバーを通報する場合は server_name（サーバー名）の入力が必須です。"
                "Botは対象サーバーに参加していないため、名前を自動取得できません。",
                ephemeral=True,
            )
            return

        display_id = target_id_str
        display_name = server_name.strip()

        # Botがたまたま対象サーバーに参加していた場合は、名前の自動更新に使う
        # （失敗しても通報自体は続行する — 参加していないのが通常のケースのため）
        try:
            guild = interaction.client.get_guild(int(target_id_str)) or await interaction.client.fetch_guild(
                int(target_id_str)
            )
            if guild is not None:
                display_name = guild.name
        except (discord.NotFound, discord.Forbidden):
            pass

    view = CategoryConsentView(
        target_type=target_type,
        reporter=interaction.user,
        target_id=display_id,
        target_username=display_name,
        note=note,
        evidence_attachment=evidence_image,
        maintainer_channel=maintainer_channel,
        related_id=related_id,
    )
    await interaction.followup.send(content=view.render(), view=view, ephemeral=True)
