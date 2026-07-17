"""
bot/events.py
全Botイベントハンドラ
改善：Wick等外部Bot処罰の確実な検知
改善：招待リンク検出のレースコンディション・API反映遅延対策
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

# ポーリング間隔を15秒に短縮
AUDIT_POLL_INTERVAL = 60

_invite_cache: dict[str, int] = {}

# 発言ごとのDB書き込みをやめてバッチ化するためのバッファ
# （メッセージ1件ごとに書き込むとNeonのコンピュートが常時起きた状態になりCU-hoursを消費するため）
_activity_buffer: list[tuple[str, str, str]] = []
_invite_msg_buffer: dict[str, int] = {}
ACTIVITY_FLUSH_INTERVAL = 60

# user_id → 招待コード のキャッシュ（on_messageで毎回DBを読みに行かないため）
_user_invite_cache: dict[str, Optional[str]] = {}
_last_audit_check: datetime = datetime.now(timezone.utc)

# 招待検出の同時実行制御用ロック（レースコンディション対策）
_invite_lock = asyncio.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_last_audit_check() -> datetime:
    """
    Bot再起動をまたいで監査ログの取りこぼしを防ぐため、
    前回チェックした時刻をDBから復元する。
    初回起動時（DBに記録がない場合）のみ現在時刻を使う。
    """
    saved = database.get_setting("last_audit_check")
    if saved:
        try:
            return datetime.fromisoformat(saved)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _save_last_audit_check(dt: datetime) -> None:
    database.set_setting("last_audit_check", dt.isoformat())


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


async def _detect_used_invite(guild: discord.Guild) -> Optional[str]:
    """
    使用された招待リンクを検出する。

    - asyncio.Lockで直列化し、複数人が同時入室してもキャッシュの競合を防ぐ
    - Discord API側のuses反映遅延に備え、最大3回・1.5秒間隔でリトライする
    """
    global _invite_cache

    async with _invite_lock:
        before = _invite_cache
        after  = before
        used_code: Optional[str] = None

        for attempt in range(3):
            after     = await _fetch_invite_cache(guild)
            used_code = _find_used_invite(before, after)
            if used_code:
                break
            if attempt < 2:
                await asyncio.sleep(1.5)

        _invite_cache = after
        return used_code


def setup_events(bot: discord.Client) -> None:

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

        _last_audit_check = _load_last_audit_check()
        bot.loop.create_task(_audit_log_poller(bot))
        bot.loop.create_task(_weekly_report_scheduler(bot))
        bot.loop.create_task(_retention_checker(bot))
        bot.loop.create_task(_activity_flusher(bot))
        print("[Bot] 全タスク起動完了")

    @bot.event
    async def on_member_join(member: discord.Member) -> None:
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

        used_code = await _detect_used_invite(member.guild)

        database.add_join_log(str(member.id), used_code, joined_at)
        database.add_retention_check(str(member.id), used_code, joined_at)
        _user_invite_cache[str(member.id)] = used_code

        print(f"[Bot] 入室: {member} | 招待コード: {used_code or '不明'}")

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

    @bot.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot:
            return
        if not isinstance(message.guild, discord.Guild):
            return
        if message.guild.id != GUILD_ID:
            return

        # システムメッセージ（入室通知・ブースト通知等）は発言として扱わない
        # MessageType.default = 通常のユーザー発言のみを対象とする
        if message.type != discord.MessageType.default:
            return

        user_id = str(message.author.id)

        # 直接INSERTせず、一定間隔でまとめて書き込むバッファに積む
        _activity_buffer.append((user_id, str(message.channel.id), _now_iso()))

        if user_id in _user_invite_cache:
            invite_code = _user_invite_cache[user_id]
        else:
            log = database.get_join_log_by_user(user_id)
            invite_code = log["invite_code"] if log and log.get("invite_code") else None
            _user_invite_cache[user_id] = invite_code

        if invite_code:
            _invite_msg_buffer[invite_code] = _invite_msg_buffer.get(invite_code, 0) + 1

        # 初回発言ロール自動付与
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

    @bot.event
    async def on_invite_create(invite: discord.Invite) -> None:
        if invite.guild is None or invite.guild.id != GUILD_ID:
            return
        creator_id = str(invite.inviter.id) if invite.inviter else "unknown"
        created_at = invite.created_at.isoformat() if invite.created_at else _now_iso()
        database.upsert_invite(invite.code, creator_id, created_at)
        _invite_cache[invite.code] = 0

    @bot.event
    async def on_invite_delete(invite: discord.Invite) -> None:
        _invite_cache.pop(invite.code, None)

    # ── Wickのwarn検知（メッセージembedから取得） ──────────
    @bot.event
    async def on_message_wick_warn(message: discord.Message) -> None:
        """Wickのwarnメッセージを検知してDBに記録"""
        if not message.author.bot:
            return
        if not isinstance(message.guild, discord.Guild):
            return
        if message.guild.id != GUILD_ID:
            return
        if not message.embeds:
            return

        # WickのBot IDリスト（主要なもの）
        WICK_BOT_IDS = [
            536991182035746816,   # Wick
            493428086768041995,   # Wick (old)
        ]
        if message.author.id not in WICK_BOT_IDS:
            return

        for embed in message.embeds:
            title = (embed.title or "").lower()
            desc  = (embed.description or "").lower()

            if "warn" not in title and "warn" not in desc:
                continue

            # ターゲットユーザーIDを取得
            target_id   = None
            target_name = "不明"

            for field in embed.fields:
                fname = field.name.lower()
                if "user" in fname or "member" in fname or "target" in fname:
                    val = field.value
                    # <@12345> 形式からIDを抽出
                    import re
                    match = re.search(r'<@!?(\d+)>', val)
                    if match:
                        target_id   = match.group(1)
                        target_name = val
                    elif val.strip().isdigit():
                        target_id = val.strip()

            if target_id is None:
                continue

            # 対象がBotの場合は記録しない
            target_user = message.guild.get_member(int(target_id))
            if target_user is None:
                try:
                    target_user = await bot.fetch_user(int(target_id))
                except Exception:
                    target_user = None
            if target_user is not None and getattr(target_user, 'bot', False):
                continue

            reason      = embed.description or ""
            executed_at = _now_iso()
            executor    = str(message.author)

            # 重複チェック（10秒以内の同一ユーザーへのWARNはスキップ）
            existing = database.get_punishments_by_user(target_id)
            recent_warn = any(
                p["punishment_type"] == "WARN" and
                p["executor"] == executor and
                (datetime.fromisoformat(executed_at) - datetime.fromisoformat(p["executed_at"])).total_seconds() < 10
                for p in existing
            )
            if recent_warn:
                continue

            database.add_punishment(
                user_id=target_id,
                target_name=target_name,
                punishment_type="WARN",
                executor=executor,
                reason=reason,
                executed_at=executed_at,
            )
            print(f"[Bot] Wickwarn検知: 対象={target_name} | 理由={reason[:50]}")


# ─────────────────────────────────────────────────────────
# 発言ログのバッチ書き込み（1分ごと）
# ─────────────────────────────────────────────────────────

async def _activity_flusher(bot: discord.Client) -> None:
    global _activity_buffer, _invite_msg_buffer

    while not bot.is_closed():
        await asyncio.sleep(ACTIVITY_FLUSH_INTERVAL)

        if _activity_buffer:
            batch, _activity_buffer = _activity_buffer, []
            try:
                database.add_activity_logs_bulk(batch)
            except Exception as e:
                print(f"[Bot] activity_logsフラッシュエラー: {e}")

        if _invite_msg_buffer:
            counts, _invite_msg_buffer = _invite_msg_buffer, {}
            try:
                database.bulk_increment_invite_messages(counts)
            except Exception as e:
                print(f"[Bot] invite集計フラッシュエラー: {e}")


# ─────────────────────────────────────────────────────────
# 監査ログポーリング（60秒ごと）
# ─────────────────────────────────────────────────────────

async def _audit_log_poller(bot: discord.Client) -> None:
    global _last_audit_check

    while not bot.is_closed():
        await asyncio.sleep(AUDIT_POLL_INTERVAL)

        guild = bot.get_guild(GUILD_ID)
        if guild is None:
            continue

        try:
            check_after       = _last_audit_check
            _last_audit_check = datetime.now(timezone.utc)
            had_activity      = False  # 実際に処罰の記録・削除が起きた場合のみDBへ永続化する

            async for entry in guild.audit_logs(limit=100):
                entry_time = entry.created_at.replace(tzinfo=timezone.utc)
                if entry_time <= check_after:
                    break

                action = entry.action

                # ── BAN ──────────────────────────────────
                if action == discord.AuditLogAction.ban:
                    punishment_type = "BAN"

                # ── BAN解除 ───────────────────────────────
                elif action == discord.AuditLogAction.unban:
                    punishment_type = "UNBAN"

                # ── KICK ─────────────────────────────────
                elif action == discord.AuditLogAction.kick:
                    punishment_type = "KICK"

                # ── TIMEOUT / TIMEOUT解除 ─────────────────
                # (discord.pyにはAuditLogAction.automod_timeoutという属性は存在しない。
                #  Wick等Bot経由のタイムアウトも、Discordネイティブのタイムアウトも、
                #  すべてmember_updateのtimed_out_until変化として検出できる)
                elif action == discord.AuditLogAction.member_update:
                    is_timeout        = False
                    is_timeout_remove = False
                    try:
                        after_changes  = entry.changes.after
                        before_changes = entry.changes.before
                        after_timeout  = getattr(after_changes,  'timed_out_until', None)
                        before_timeout = getattr(before_changes, 'timed_out_until', None)

                        if after_timeout is not None:
                            if str(after_timeout) == "None" or after_timeout is None:
                                is_timeout_remove = True
                            else:
                                is_timeout = True
                        elif before_timeout is not None:
                            is_timeout_remove = True
                    except Exception:
                        pass

                    # 文字列チェックで補完
                    if not is_timeout and not is_timeout_remove:
                        try:
                            changes_str = str(entry.changes)
                            if 'timed_out_until' in changes_str or 'timeout' in changes_str.lower():
                                if "'after': None" in changes_str or '"after": null' in changes_str:
                                    is_timeout_remove = True
                                else:
                                    is_timeout = True
                        except Exception:
                            pass

                    if not is_timeout and not is_timeout_remove:
                        continue

                    punishment_type = "TIMEOUT_REMOVE" if is_timeout_remove else "TIMEOUT"

                else:
                    continue

                target = entry.target
                if target is None:
                    continue

                # 対象がBotの場合は記録しない
                if getattr(target, 'bot', False):
                    continue

                target_id   = str(target.id)
                target_name = str(target) if hasattr(target, 'name') else str(target.id)
                executor    = str(entry.user) if entry.user else "Unknown"
                reason      = entry.reason or ""
                executed_at = entry_time.isoformat()

                # 重複チェック
                existing = database.get_punishments_by_user(target_id)
                already  = any(
                    p["executed_at"] == executed_at and
                    p["punishment_type"] == punishment_type
                    for p in existing
                )
                if already:
                    continue

                # TIMEOUT解除の場合：最新のTIMEOUTレコード1件のみ自動削除
                if punishment_type == "TIMEOUT_REMOVE":
                    # executed_atで降順ソートして最新のTIMEOUTを1件取得
                    latest_timeout = next(
                        (p for p in sorted(existing, key=lambda x: x["executed_at"], reverse=True)
                         if p["punishment_type"] == "TIMEOUT"),
                        None
                    )
                    if latest_timeout:
                        database.delete_punishment(latest_timeout["id"])
                        had_activity = True
                        print(f"[Bot] 最新のTIMEOUTを削除: id={latest_timeout['id']} | 対象: {target_name}")
                    # TIMEOUT_REMOVEはDBに記録せず終了
                    continue

                database.add_punishment(
                    user_id=target_id,
                    target_name=target_name,
                    punishment_type=punishment_type,
                    executor=executor,
                    reason=reason,
                    executed_at=executed_at,
                )
                had_activity = True

                # 運営メンバーなら実績カウント
                if entry.user:
                    member = guild.get_member(entry.user.id)
                    if member and any(r.id == MOD_ROLE_ID for r in member.roles):
                        database.increment_mod_audit(str(entry.user.id))

                print(
                    f"[Bot] 処罰検知: {punishment_type} | "
                    f"対象: {target_name} | 実行者: {executor} | 理由: {reason[:30]}"
                )

            # 実際に何かしら処罰の記録・削除が起きた回だけ、チェック時刻をDBへ永続化する
            # （毎回書き込むとNeonのコンピュートが常時起きた状態になりCU-hoursを消費するため）
            if had_activity:
                _save_last_audit_check(_last_audit_check)

        except discord.Forbidden:
            print("[Bot] 警告: 監査ログの読み取り権限がありません")
        except Exception as e:
            print(f"[Bot] 監査ログポーリングエラー: {e}")


# ─────────────────────────────────────────────────────────
# 週次レポート自動送信（月曜朝9時 JST）
# ─────────────────────────────────────────────────────────

async def _weekly_report_scheduler(bot: discord.Client) -> None:
    while not bot.is_closed():
        now = datetime.now(timezone.utc)
        days_until_monday = (7 - now.weekday()) % 7
        if days_until_monday == 0 and now.hour >= 0:
            days_until_monday = 7
        next_monday  = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=days_until_monday)
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
        stats        = database.get_weekly_stats()
        member_count = guild.member_count if guild else "不明"

        top_ch_lines = []
        for i, ch in enumerate(stats["top_channels"], 1):
            ch_obj  = bot.get_channel(int(ch["channel_id"])) if guild else None
            ch_name = f"#{ch_obj.name}" if ch_obj else ch["channel_id"]
            top_ch_lines.append(f"{i}. {ch_name}（{ch['cnt']}件）")

        top_ch_text = "\n".join(top_ch_lines) if top_ch_lines else "データなし"

        embed = discord.Embed(
            title="📊 週次サーバーレポート",
            color=0x00C896,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="👥 現在のメンバー数", value=f"{member_count}人", inline=True)
        embed.add_field(name="📥 今週の入室数",     value=f"{stats['joins']}人", inline=True)
        embed.add_field(name="📤 今週の退室数",     value=f"{stats['leaves']}人", inline=True)
        embed.add_field(name="⚡ 即抜け数",         value=f"{stats['instant_leaves']}人", inline=True)
        embed.add_field(name="💬 今週の発言数",     value=f"{stats['messages']:,}件", inline=True)
        embed.add_field(name="🔨 今週の処罰数",     value=f"{stats['punishments']}件", inline=True)
        embed.add_field(name="🔥 チャンネル熱量TOP3", value=top_ch_text, inline=False)
        embed.set_footer(text="Guide Base + Community Dashboard")

        await channel.send(embed=embed)
        print("[Bot] 週次レポート送信完了")

    except Exception as e:
        print(f"[Bot] 週次レポート送信エラー: {e}")


# ─────────────────────────────────────────────────────────
# 定着率チェック（1時間ごと）
# ─────────────────────────────────────────────────────────

async def _retention_checker(bot: discord.Client) -> None:
    while not bot.is_closed():
        await asyncio.sleep(3600)

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
                member    = guild.get_member(int(user_id))
                is_member = member is not None

                check_7d  = None
                check_30d = None

                if record["check_7d"] is None and elapsed >= 7 * 86400:
                    check_7d = is_member
                if record["check_30d"] is None and elapsed >= 30 * 86400:
                    check_30d = is_member

                if check_7d is not None or check_30d is not None:
                    database.update_retention_check(user_id, check_7d, check_30d)

        except Exception as e:
            print(f"[Bot] 定着率チェックエラー: {e}")
