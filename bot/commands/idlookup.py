"""
bot/commands/idlookup.py
/idlookup: メッセージリンクから投稿者のユーザーID・ユーザー名を調べる。

Cloudflare Workers版と違い、discord.pyが認証・リトライ込みでHTTP呼び出しを
面倒見てくれるので、自前のRESTラッパーは不要（bot.fetch_channel /
channel.fetch_message をそのまま使うだけでよい）。
"""

import re

import discord

# https://discord.com/channels/<guildId|@me>/<channelId>/<messageId>
# canary/ptbサブドメイン、discordapp.com旧ドメインも許容する。
MESSAGE_LINK_RE = re.compile(
    r"^https?://(?:canary\.|ptb\.)?discord(?:app)?\.com/channels/(?:\d+|@me)/(\d+)/(\d+)/?(?:\?.*)?$"
)


async def handle_idlookup(interaction: discord.Interaction, message_link: str) -> None:
    match = MESSAGE_LINK_RE.match(message_link.strip())
    if not match:
        await interaction.response.send_message(
            "メッセージリンクの形式を認識できませんでした。\n"
            "対象メッセージを右クリック（スマホは長押し）→「メッセージリンクをコピー」で"
            "取得したリンクをそのまま貼り付けてください。",
            ephemeral=True,
        )
        return

    channel_id, message_id = int(match.group(1)), int(match.group(2))
    client = interaction.client

    try:
        channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        await interaction.response.send_message(
            "指定されたメッセージが見つかりませんでした（削除済み、またはリンクが誤っている可能性があります）。",
            ephemeral=True,
        )
        return
    except discord.Forbidden:
        await interaction.response.send_message(
            "Botがそのチャンネルにアクセスできないため、メッセージを取得できませんでした。\n"
            "無関係なサーバー・Botが参加していないチャンネルのリンクは解決できません。",
            ephemeral=True,
        )
        return
    except discord.HTTPException as e:
        await interaction.response.send_message(
            f"Discord APIエラーが発生しました（status: {e.status}）。時間をおいて再度お試しください。",
            ephemeral=True,
        )
        return

    author = message.author
    lines = [f"投稿者ID: `{author.id}`", f"ユーザー名: `{author.name}`"]
    global_name = getattr(author, "global_name", None)
    if global_name:
        lines.append(f"表示名: {global_name}")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)
