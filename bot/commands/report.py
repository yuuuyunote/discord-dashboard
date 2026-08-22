"""
bot/commands/report.py
/report: 悪質なユーザーを通報する。

user/user_id はどちらか一方のみ。user_id指定時はfetch_userで実在確認する
（グローバル — サーバーに居ないユーザーも対象にできる）。証拠画像は必須で、
画像ファイルであることをcontent_typeで確認する。
"""

from typing import Optional

import discord

from bot.ui.report_flow import CategoryConsentView


async def handle_report(
    interaction: discord.Interaction,
    user: Optional[discord.User],
    user_id: Optional[str],
    evidence_image: discord.Attachment,
    note: Optional[str],
    maintainer_channel: discord.abc.Messageable,
) -> None:
    if (user is None) == (user_id is None):
        await interaction.response.send_message(
            "user か user_id のどちらか一方だけを指定してください。", ephemeral=True
        )
        return

    if evidence_image.content_type is None or not evidence_image.content_type.startswith("image/"):
        await interaction.response.send_message(
            "evidence_image には画像ファイルを添付してください。", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    if user is not None:
        target_id = str(user.id)
        target_username = user.name
    else:
        target_id_str = user_id.strip()
        if not target_id_str.isdigit() or not (17 <= len(target_id_str) <= 20):
            await interaction.followup.send(
                f"`{target_id_str}` はDiscordのユーザーIDとして正しい形式ではありません（17〜20桁の数字）。",
                ephemeral=True,
            )
            return
        try:
            fetched = await interaction.client.fetch_user(int(target_id_str))
        except discord.NotFound:
            await interaction.followup.send(
                f"ユーザーID `{target_id_str}` は存在しないようです。", ephemeral=True
            )
            return
        target_id = str(fetched.id)
        target_username = fetched.name

    view = CategoryConsentView(
        reporter=interaction.user,
        target_id=target_id,
        target_username=target_username,
        note=note,
        evidence_attachment=evidence_image,
        maintainer_channel=maintainer_channel,
    )
    await interaction.followup.send(content=view.render(), view=view, ephemeral=True)
