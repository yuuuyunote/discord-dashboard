"""
bot/commands.py
スラッシュコマンド定義
/userinfo - ユーザーの基本情報・処罰履歴をDiscord上で表示
"""

import os
import json

import discord

import database

GUILD_ID    = int(os.getenv("GUILD_ID", "0"))
MOD_ROLE_ID = int(os.getenv("MOD_ROLE_ID", "0"))


def setup_commands(bot: discord.Client) -> None:

    tree = discord.app_commands.CommandTree(bot)

    @tree.command(
        name="userinfo",
        description="ユーザーの基本情報・処罰履歴を表示します（運営専用）",
        guild=discord.Object(id=GUILD_ID),
    )
    @discord.app_commands.describe(user="情報を確認したいユーザー")
    async def userinfo(
        interaction: discord.Interaction,
        user: discord.Member,
    ) -> None:
        # 運営ロールチェック
        invoker = interaction.user
        if not any(r.id == MOD_ROLE_ID for r in invoker.roles):
            await interaction.response.send_message(
                "❌ このコマンドは運営メンバーのみ使用できます。",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        user_id = str(user.id)
        db_user = database.get_user(user_id)
        punishments = database.get_punishments_by_user(user_id)
        join_log    = database.get_join_log_by_user(user_id)
        msg_count   = database.get_user_message_count(user_id)
        vc_seconds  = database.get_user_vc_seconds(user_id)
        notes       = database.get_user_notes(user_id)

        vc_h = vc_seconds // 3600
        vc_m = (vc_seconds % 3600) // 60

        embed = discord.Embed(
            title=f"👤 {user.display_name}",
            color=0x00C896,
        )
        embed.set_thumbnail(url=user.display_avatar.url)

        # 基本情報
        embed.add_field(
            name="📋 基本情報",
            value=(
                f"**ID:** `{user.id}`\n"
                f"**アカウント作成:** {user.created_at.strftime('%Y/%m/%d')}\n"
                f"**サーバー参加:** {user.joined_at.strftime('%Y/%m/%d') if user.joined_at else '不明'}"
            ),
            inline=False
        )

        # アクティビティ
        embed.add_field(
            name="💬 アクティビティ",
            value=(
                f"**発言数:** {msg_count:,}件\n"
                f"**VC時間:** {vc_h}h {vc_m}m"
            ),
            inline=True
        )

        # 参加経路
        invite_code = join_log.get("invite_code", "不明") if join_log else "不明"
        embed.add_field(
            name="🔗 参加招待",
            value=f"`{invite_code}`",
            inline=True
        )

        # 現在のロール
        roles = [r.name for r in user.roles if r.name != "@everyone"]
        embed.add_field(
            name="🏷️ 現在のロール",
            value=", ".join(roles) if roles else "なし",
            inline=False
        )

        # 処罰履歴
        if punishments:
            pun_lines = []
            for p in punishments[:5]:
                pun_lines.append(
                    f"**{p['punishment_type']}** | {p['executor']} | "
                    f"{p['executed_at'][:10]} | {p['reason'] or '理由なし'}"
                )
            embed.add_field(
                name=f"🔨 処罰履歴（{len(punishments)}件）",
                value="\n".join(pun_lines),
                inline=False
            )
        else:
            embed.add_field(name="🔨 処罰履歴", value="なし", inline=False)

        # ノート
        if notes:
            note_lines = []
            for n in notes[:3]:
                note_lines.append(f"• {n['content'][:50]}（{n['author_name']} / {n['created_at'][:10]}）")
            embed.add_field(
                name=f"📝 ノート（{len(notes)}件）",
                value="\n".join(note_lines),
                inline=False
            )

        embed.set_footer(text="Guide Base + Community Dashboard")

        await interaction.followup.send(embed=embed, ephemeral=True)

    # コマンドを同期する関数を返す
    return tree
