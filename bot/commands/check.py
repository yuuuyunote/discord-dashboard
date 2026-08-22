"""
bot/commands/check.py
/check: ユーザーが通報リスト（dist/users.json）に載っているか確認する。

設計メモ通り、ここで見るのは公開ブロックリストのみ。report_countや報告者自身の
履歴（Postgres）は扱わない — そちらは/report実装時に別途。

GitHubへのfetchが絡む分/idlookupより時間が読めないため、即時応答ではなく
defer→followupにしている（/idlookupは discord.py 内部キャッシュ頼みで
速いので即時応答のままにしてある。使い分けは意図的）。
"""

import re
from typing import Optional

import discord

from bot.commands.categories import label_for
from bot.data.blocklist import BlocklistFetchError, blocklist_cache

SNOWFLAKE_RE = re.compile(r"^[0-9]{17,20}$")


async def handle_check(
    interaction: discord.Interaction,
    user: Optional[discord.User],
    user_id: Optional[str],
) -> None:
    if (user is None) == (user_id is None):
        await interaction.response.send_message(
            "user か user_id のどちらか一方だけを指定してください。",
            ephemeral=True,
        )
        return

    if user is not None:
        target_id = str(user.id)
        display_hint = f"{user.name}（{user.mention}）"
    else:
        target_id = user_id.strip()
        if not SNOWFLAKE_RE.match(target_id):
            await interaction.response.send_message(
                f"`{target_id}` はDiscordのユーザーIDとして正しい形式ではありません（17〜20桁の数字）。",
                ephemeral=True,
            )
            return
        display_hint = f"`{target_id}`"

    await interaction.response.defer(ephemeral=True)

    try:
        entry = await blocklist_cache.find(target_id)
    except BlocklistFetchError as e:
        await interaction.followup.send(
            f"通報リストの取得に失敗しました: {e}\n時間をおいて再度お試しください。",
            ephemeral=True,
        )
        return

    if entry is None:
        await interaction.followup.send(
            f"{display_hint} は通報リストに登録されていません。\n"
            "（登録がないことは安全性を保証するものではありません）",
            ephemeral=True,
        )
        return

    categories = "、".join(label_for(c) for c in entry.get("categories", []))
    lines = [
        "⚠️ このユーザーは通報リストに登録されています",
        f"対象: {display_hint}",
        f"確認時点のユーザー名（報告時点のスナップショット）: `{entry.get('username', '?')}`",
        f"カテゴリ: {categories}",
        f"補足: {entry.get('note', '（なし）')}",
        f"独立した報告件数: {entry.get('report_count', '?')}",
        f"最終更新: {entry.get('updated_at', '?')}",
    ]
    await interaction.followup.send("\n".join(lines), ephemeral=True)
