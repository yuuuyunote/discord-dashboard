"""
bot/commands/report.py
/report: 悪質なユーザー/サーバー/Botを通報する。

target_typeで分岐:
- user / bot: fetch_userで実在確認する（Botもuserオブジェクトとして取得できる）。
  target_type=="bot"なのに実際はBotでない、あるいはその逆の場合は不整合として弾く。
- server: Botは対象サーバーに参加していないのが前提のため fetch_guild では
  実在確認できないことが多く、サーバー名の手入力も求めない
  （必須項目はサーバーID・証拠画像・カテゴリのみ）。
  target_idにはサーバーIDのほか招待リンク（discord.gg/xxx 等）やコード単体も
  受け付ける——数字だけのID（スノーフレーク）ならAPI呼び出し無しでそのまま使い、
  招待リンク/招待コードらしい入力の場合のみ fetch_invite で1回だけ解決する。
  IDをそのまま指定した場合の応答速度は従来通り変えず、招待リンクを使う場合だけ
  その解決コストを許容してもらう設計（そこそこ遅くなるようなら呼び出し側で
  ID直指定に倒せるよう、両対応にしてある）。
  表示名は fetch_guild / 招待情報から取れれば使い、取れなければID表示のみで進める
  （ベストエフォート、失敗しても通報自体は続行する）。
"""

from typing import Optional

import discord

from bot.ui.report_flow import CategoryConsentView

_SNOWFLAKE_MIN_LEN, _SNOWFLAKE_MAX_LEN = 17, 20


def _is_snowflake(value: str) -> bool:
    return value.isdigit() and _SNOWFLAKE_MIN_LEN <= len(value) <= _SNOWFLAKE_MAX_LEN


async def _resolve_server_id(
    interaction: discord.Interaction, raw_value: str
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    サーバー通報のtarget_id入力（サーバーID or 招待リンク/コード）を解決する。

    戻り値: (display_id, display_name, error_message)
    error_messageがNoneでなければ呼び出し側はそれをユーザーに返して処理を打ち切る。
    """
    if _is_snowflake(raw_value):
        display_id = raw_value
        display_name: Optional[str] = None
    else:
        try:
            invite = await interaction.client.fetch_invite(raw_value)
        except discord.NotFound:
            return None, None, (
                f"`{raw_value}` はサーバーIDとしても招待リンクとしても認識できませんでした。"
                "サーバーIDを直接指定するか、有効な招待リンクを貼ってください。"
            )
        except discord.HTTPException:
            return None, None, (
                "招待リンクの解決に失敗しました。時間をおいて再度試すか、"
                "サーバーIDを直接指定してください。"
            )

        if invite.guild is None:
            return None, None, (
                "このリンクはサーバーの招待ではないようです（グループDM等の可能性があります）。"
                "サーバーIDを直接指定してください。"
            )
        display_id = str(invite.guild.id)
        display_name = invite.guild.name

    # Botがたまたま対象サーバーに参加していた場合は、より正確な名前に更新する
    # （失敗・未参加でも通報自体は続行する — 参加していないのが通常のケースのため）
    try:
        guild = interaction.client.get_guild(int(display_id)) or await interaction.client.fetch_guild(
            int(display_id)
        )
        if guild is not None:
            display_name = guild.name
    except (discord.NotFound, discord.Forbidden):
        pass

    return display_id, (display_name or display_id), None


async def handle_report(
    interaction: discord.Interaction,
    target_type: str,
    target_id: str,
    evidence_image: discord.Attachment,
    note: Optional[str],
    related_id: Optional[str],
    maintainer_channel: discord.abc.Messageable,
) -> None:
    if evidence_image.content_type is None or not evidence_image.content_type.startswith("image/"):
        await interaction.response.send_message(
            "evidence_image には画像ファイルを添付してください。", ephemeral=True
        )
        return

    target_id_str = target_id.strip()

    # user/botはIDのみ受け付ける（招待リンクの概念が無いため従来通り）。
    # serverは招待リンクも受け付けるため、ここでは弾かず _resolve_server_id 側で判定する。
    if target_type in ("user", "bot") and not _is_snowflake(target_id_str):
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
        display_id, display_name, error_message = await _resolve_server_id(interaction, target_id_str)
        if error_message is not None:
            await interaction.followup.send(error_message, ephemeral=True)
            return

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
