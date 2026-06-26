"""
bot/events.py
全Botイベントハンドラ
追加機能：
- 初回発言ロール自動付与
- 定期レポート（月曜朝9時）
- 定着率チェック（7日・30日）
"""

import asyncio
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord

import database

GUILD_ID           = int(os.getenv("GUILD_ID", "0"))
MOD_ROLE_ID        = int(os.getenv("MOD_ROLE_ID", "0"))
FIRST_MSG_ROLE_ID  = int(os.getenv("FIRST_MSG_ROLE_ID", "0"))
REPORT_CHANNEL_ID  = int(os.getenv("REPORT_CHANNEL_ID", "0"))

AUDIT_POLL_INTERVAL = 30

_invite_cache: dict[str, int] = {}
_last_audit_check: datetime = datetime.now(timezone.utc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _fetch_invite_cache(guild: discord.Guild) -> dict[str, int]:
    try:
        invites = await guild.invites()
        return {inv.code: inv.uses or 0 for inv in invites}
    except discord.Forbidden:
        return {}


def _find_used_invite(before: dict[str, int], after: dict[str, int]) -> Optional[str]:
    for code, uses in after.items():
        if uses > before.get(code, 0):
            return code
    return None


def setup_events(bot: discord.Client) -> None:

    # ── Bot 起動時 ──────────────────────────────────────

    @bot.event
    async def on_ready() -> None:
        global _invite_cache, _last_audit_check

        print(f"[Bot] ログイン成功: {bot.user} (ID: {bot.user.id})")

        guild = bot.get_guild(GUILD_ID)
        if guild is None:
            print(f"[Bot] 警告: GUILD_ID={GUILD_ID} のサーバーが見つかりません")
            return

        _invite_cache = await _fetch_invite_cache(guild)
        print(f"[Bot] 招待キャッシュ初期化: {len(_invite_cache)} 件")

        try:
            invites = await guild.invites()
            for inv in invites:
                creator_id = str(inv.inviter.id) if inv.inviter else "unknown"
                created_at = inv.created_at.isoformat() if inv.created_at else _now_iso()
                database.upsert_invite(inv.code, creator_id, created_at)
        except discord.Forbidden:
            print("[Bot] 警告: 招待リンク取得権限がありません")

        _last_audit_check = datetime.now(timezone.utc)
        bot.loop.create_task(_audit_log_poller(bot))
        bot.loop.create_task(_weekly_report_scheduler(bot))
        bot.loop.create_task(_retention_checker(bot))
        print("[Bot] 全タスク起動完了")

    # ── メンバー入室 ────────────────────────────────────

    @bot.event
    async def on_member_join(member: discord.Member) -> None:
        global _invite_cache
        if member.guild.id != GUILD_ID:
            return

        joined_at       = member.joined_at.isoformat() if member.joined_at else _now_iso()
        account_created = member.created_at.isoformat()

        database.upsert_user(
            user_id=str(member.id),
            username=str(member),
            account_created=account_created,
            joined_at=joined_at,
        )

        new_cache = await _fetch_invite_cache(member.guild)
        used_code = _find_used_invite(_invite_cache, new_cache)
        _invite_cache = new_cache

        database.add_join_log(str(member.id), used_code, joined_at)

        # 定着率チェック用レコード追加
        database.add_retention_check(str(member.id), used_code, joined_at)

        print(f"[Bot] 入室: {member} | 招待コード: {used_code or '不明'}")

        # 10分後に初期ロール記録
        async def _record_initial_roles() -> None:
            await asyncio.sleep(600)
            try:
                updated = member.guild.get_member(member.id)
                if updated is None:
                    return
                role_names = [r.name for r in updated.roles if r.name != "@everyone"]
                database.update_user_initial_roles(str(member.id), role_names)
                print(f"[Bot] 初期ロール記録: {member} → {role_names}")
            except Exception as e:
                print(f"[Bot] 初期ロール記録エラー: {e}")

        bot.loop.create_task(_record_initial_roles())

    # ── メンバー退室 ────────────────────────────────────

    @bot.event
    async def on_member_remove(member: discord.Member) -> None:
        if member.guild.id != GUILD_ID:
            return

        left_at = _now_iso()
        log = database.record_leave(str(member.id), left_at)

        if log and log.get("invite_code"):
            joined = datetime.fromisoformat(log["joined_at"])
            left   = datetime.fromisoformat(left_at)
            if (left - joined).total_seconds() < 86400:
                database.increment_invite_leave(log["invite_code"])
                print(f"[Bot] 即抜け検知: {member} | コード: {log['invite_code']}")

        print(f"[Bot] 退室: {member}")

    # ── メッセージ送信 ──────────────────────────────────

    @bot.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot:
            return
        if not isinstance(message.guild, discord.Guild):
            return
        if message.guild.id != GUILD_ID:
            return

        user_id = str(message.author.id)

        # アクティビティ記録
        database.add_activity_log(
            user_id=user_id,
            channel_id=str(message.channel.id),
            sent_at=_now_iso(),
        )

        # 招待リンク別発言数更新
        log = database.get_join_log_by_user(user_id)
        if log and log.get("invite_code"):
            database.update_invite_activity(log["invite_code"], messages=1, vc_sec=0)

        # ── 初回発言ロール自動付与 ──────────────────────
        if FIRST_MSG_ROLE_ID and not database.has_first_message(user_id):
            try:
                guild  = message.guild
                member = guild.get_member(message.author.id)
                role   = guild.get_role(FIRST_MSG_ROLE_ID)
                if member and role and role not in member.roles:
                    await member.add_roles(role, reason="初回発言ロール自動付与")
                    database.record_first_message(user_id)
                    print(f"[Bot] 初回発言ロール付与: {member} → {role.name}")
            except Exception as e:
                print(f"[Bot] 初回発言ロール付与エラー: {e}")

    # ── VC 入退室 ────────────────────────────────────────

    @bot.event
    async def on_voice_state_update(
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.guild.id != GUILD_ID or member.bot:
            return

        now = _now_iso()
        if before.channel is None and after.channel is not None:
            database.add_voice_join(str(member.id), now)
        elif before.channel is not None and after.channel is None:
            vc_sec = database.record_voice_leave(str(member.id), now)
            if vc_sec and vc_sec > 0:
                log = database.get_join_log_by_user(str(member.id))
                if log and log.get("invite_code"):
                    database.update_invite_activity(log["invite_code"], messages=0, vc_sec=vc_sec)

    # ── 招待リンク作成・削除 ────────────────────────────

    @bot.event
    async def on_invite_create(invite: discord.Invite) -> None:
        if invite.guild is None or invite.guild.id != GUILD_ID:
            return
        creator_id = str(invite.inviter.id) if invite.inviter else "unknown"
        created_at = invite.created_at.isoformat() if invite.created_at else _now_iso()
        database.upsert_invite(invite.code, creator_id, created_at)
        _invite_cache[invite.code] = 0
        print(f"[Bot] 招待リンク作成: {invite.code}")

    @bot.event
    async def on_invite_delete(invite: discord.Invite) -> None:
        _invite_cache.pop(invite.code, None)
        print(f"[Bot] 招待リンク削除: {invite.code}")


# ─────────────────────────────────────────────────────────
# 監査ログポーリング（30秒ごと）
# ─────────────────────────────────────────────────────────

async def _audit_log_poller(bot: discord.Client) -> None:
    global _last_audit_check

    ACTION_MAP = {
        discord.AuditLogAction.ban:           "BAN",
        discord.AuditLogAction.kick:          "KICK",
        discord.AuditLogAction.member_update: "TIMEOUT",
    }

    while not bot.is_closed():
        await asyncio.sleep(AUDIT_POLL_INTERVAL)

        guild = bot.get_guild(GUILD_ID)
        if guild is None:
            continue

        try:
            check_after       = _last_audit_check
            _last_audit_check = datetime.now(timezone.utc)

            async for entry in guild.audit_logs(limit=50):
                entry_time = entry.created_at.replace(tzinfo=timezone.utc)
                if entry_time <= check_after:
                    break

                action = entry.action
                if action == discord.AuditLogAction.member_update:
                    if "timed_out_until" not in str(entry.changes):
                        continue
                    punishment_type = "TIMEOUT"
                elif action in ACTION_MAP:
                    punishment_type = ACTION_MAP[action]
                else:
                    continue

                target = entry.target
                if target is None:
                    continue

                target_id   = str(target.id)
                target_name = str(target) if hasattr(target, "name") else str(target.id)
                executor    = str(entry.user) if entry.user else "Unknown"
                reason      = entry.reason or ""
                executed_at = entry_time.isoformat()

                existing = database.get_punishments_by_user(target_id)
                already  = any(
                    p["executed_at"] == executed_at and p["punishment_type"] == punishment_type
                    for p in existing
                )
                if already:
                    continue

                database.add_punishment(target_id, target_name, punishment_type, executor, reason, executed_at)

                if entry.user:
                    member = guild.get_member(entry.user.id)
                    if member and any(r.id == MOD_ROLE_ID for r in member.roles):
                        database.increment_mod_audit(str(entry.user.id))

                print(f"[Bot] 処罰検知: {punishment_type} | 対象: {target_name} | 実行者: {executor}")

        except discord.Forbidden:
            print("[Bot] 警告: 監査ログの読み取り権限がありません")
        except Exception as e:
            print(f"[Bot] 監査ログポーリングエラー: {e}")


# ─────────────────────────────────────────────────────────
# 週次レポート自動送信（月曜朝9時 JST）
# ─────────────────────────────────────────────────────────

async def _weekly_report_scheduler(bot: discord.Client) -> None:
    """毎週月曜の9:00 JST（0:00 UTC）にレポートを送信"""
    while not bot.is_closed():
        now = datetime.now(timezone.utc)
        # 次の月曜0:00 UTCを計算
        days_until_monday = (7 - now.weekday()) % 7
        if days_until_monday == 0 and now.hour >= 0:
            days_until_monday = 7
        next_monday = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=days_until_monday)
        wait_seconds = (next_monday - now).total_seconds()

        print(f"[Bot] 次回レポート送信まで: {int(wait_seconds // 3600)}時間")
        await asyncio.sleep(wait_seconds)

        await _send_weekly_report(bot)


async def _send_weekly_report(bot: discord.Client) -> None:
    guild   = bot.get_guild(GUILD_ID)
    channel = bot.get_channel(REPORT_CHANNEL_ID)
    if not channel:
        print("[Bot] レポートチャンネルが見つかりません")
        return

    try:
        stats = database.get_weekly_stats()
        member_count = guild.member_count if guild else "不明"

        # チャンネル名解決
        top_ch_lines = []
        for i, ch in enumerate(stats["top_channels"], 1):
            ch_obj = bot.get_channel(int(ch["channel_id"])) if guild else None
            ch_name = f"#{ch_obj.name}" if ch_obj else ch["channel_id"]
            top_ch_lines.append(f"{i}. {ch_name}（{ch['cnt']}件）")

        top_ch_text = "\n".join(top_ch_lines) if top_ch_lines else "データなし"

        embed = discord.Embed(
            title="📊 週次サーバーレポート",
            color=0x00C896,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="👥 現在のメンバー数", value=f"{member_count}人", inline=True)
        embed.add_field(name="📥 今週の入室数", value=f"{stats['joins']}人", inline=True)
        embed.add_field(name="📤 今週の退室数", value=f"{stats['leaves']}人", inline=True)
        embed.add_field(name="⚡ 即抜け数", value=f"{stats['instant_leaves']}人", inline=True)
        embed.add_field(name="💬 今週の発言数", value=f"{stats['messages']:,}件", inline=True)
        embed.add_field(name="🔨 今週の処罰数", value=f"{stats['punishments']}件", inline=True)
        embed.add_field(name="🔥 チャンネル熱量TOP3", value=top_ch_text, inline=False)
        embed.set_footer(text="Guide Base + Community Dashboard")

        await channel.send(embed=embed)
        print("[Bot] 週次レポート送信完了")

    except Exception as e:
        print(f"[Bot] 週次レポート送信エラー: {e}")


# ─────────────────────────────────────────────────────────
# 定着率チェック（1時間ごとに7日・30日を確認）
# ─────────────────────────────────────────────────────────

async def _retention_checker(bot: discord.Client) -> None:
    while not bot.is_closed():
        await asyncio.sleep(3600)  # 1時間ごと

        guild = bot.get_guild(GUILD_ID)
        if guild is None:
            continue

        try:
            pending = database.get_pending_retention_checks()
            now     = datetime.now(timezone.utc)

            for record in pending:
                user_id   = record["user_id"]
                joined_at = datetime.fromisoformat(record["joined_at"])
                elapsed   = (now - joined_at).total_seconds()

                # メンバーがまだいるか確認
                member = guild.get_member(int(user_id))
                is_member = member is not None

                check_7d  = None
                check_30d = None

                # 7日経過チェック
                if record["check_7d"] is None and elapsed >= 7 * 86400:
                    check_7d = is_member

                # 30日経過チェック
                if record["check_30d"] is None and elapsed >= 30 * 86400:
                    check_30d = is_member

                if check_7d is not None or check_30d is not None:
                    database.update_retention_check(user_id, check_7d, check_30d)

        except Exception as e:
            print(f"[Bot] 定着率チェックエラー: {e}")
