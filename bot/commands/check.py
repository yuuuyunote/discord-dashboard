"""
bot/commands/check.py
/check: 指定したID（ユーザー/サーバー/Bot）が通報リストに載っているか確認する。

target_typeでdist/{users,servers,bots}.jsonのどれを見るか切り替える。
"""

import re

import discord

from bot.commands.categories import label_for, target_type_label
from bot.data.blocklist import BlocklistFetchError, cache_for

SNOWFLAKE_RE = re.compile(r"^[0-9]{17,20}$")

# target_typeごとに、エントリの「名前」フィールド名が違う（user/bot: username, server: name）
_NAME_FIELD_BY_TYPE = {"user": "username", "server": "name", "bot": "username"}
# server/bot の任意ID（creator_id / developer_id）フィールド名。userにはない。
_RELATED_ID_FIELD_BY_TYPE = {"server": "creator_id", "bot": "developer_id"}
_RELATED_ID_LABEL_BY_TYPE = {"server": "作成者ID", "bot": "開発者ID"}


async def handle_check(interaction: discord.Interaction, target_type: str, target_id: str) -> None:
    target_id = target_id.strip()
    if not SNOWFLAKE_RE.match(target_id):
        await interaction.response.send_message(
            f"`{target_id}` はDiscordのID（{target_type_label(target_type)}）として正しい形式ではありません（17〜20桁の数字）。",
            ephemeral=True,
        )
        return

    display_hint = f"`{target_id}`"

    await interaction.response.defer(ephemeral=True)

    try:
        entry = await cache_for(target_type).find(target_id)
    except BlocklistFetchError as e:
        await interaction.followup.send(
            f"通報リストの取得に失敗しました: {e}\n時間をおいて再度お試しください。",
            ephemeral=True,
        )
        return

    label = target_type_label(target_type)

    if entry is None:
        await interaction.followup.send(
            f"{display_hint}（{label}）は通報リストに登録されていません。\n"
            "（登録がないことは安全性を保証するものではありません）",
            ephemeral=True,
        )
        return

    name_field = _NAME_FIELD_BY_TYPE[target_type]
    categories = "、".join(label_for(c, target_type) for c in entry.get("categories", []))

    lines = [
        f"⚠️ この{label}は通報リストに登録されています",
        f"対象: {display_hint}",
        f"確認時点の名前（報告時点のスナップショット）: `{entry.get(name_field, '?')}`",
        f"カテゴリ: {categories}",
        f"補足: {entry.get('note', '（なし）')}",
        f"独立した報告件数: {entry.get('report_count', '?')}",
        f"最終更新: {entry.get('updated_at', '?')}",
    ]

    related_field = _RELATED_ID_FIELD_BY_TYPE.get(target_type)
    if related_field and entry.get(related_field):
        lines.append(f"{_RELATED_ID_LABEL_BY_TYPE[target_type]}: `{entry[related_field]}`")

    await interaction.followup.send("\n".join(lines), ephemeral=True)
