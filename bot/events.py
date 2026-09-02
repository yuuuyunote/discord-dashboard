"""
bot/events.py
全Botイベントハンドラ
改善：招待リンク検出のレースコンディション・API反映遅延対策
"""

import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord

import database

logger = logging.getLogger(__name__)

GUILD_ID           = int(os.getenv("GUILD_ID", "0"))
MOD_ROLE_ID        = int(os.getenv("MOD_ROLE_ID", "0"))
FIRST_MSG_ROLE_ID  = int(os.getenv("FIRST_MSG_ROLE_ID", "0"))
DM_REPLY_CHANNEL_ID = int(os.getenv("DM_REPLY_CHANNEL_ID", "0"))

# ポーリング間隔を15秒に短縮
AUDIT_POLL_INTERVAL = 60

_invite_cache: dict[str, int] = {}

# 発言ごとのDB書き込みをやめてバッチ化するためのバッファ
# （メッセージ1件ごとに書き込むとNeonのコンピュートが常時起きた状態になりCU-hoursを消費するため）
_activity_buffer: list[tuple[str, str, str]] = []
_invite_msg_buffer: dict[str, int] = {}
ACTIVITY_FLUSH_INTERVAL = 60
THREAD_KEEPALIVE_MESSAGE  = "."

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


async def _handle_dm_reply(bot: discord.Client, message: discord.Message) -> None:
    """個別チャットスレッドにDM返信を保存し、指定チャンネルに通知する"""
    user_id = str(message.author.id)
    content = message.content.strip() if message.content else "（本文なし・添付ファイルのみ等）"
    now = _now_iso()

    try:
        inserted = database.add_dm_message(user_id, "in", content, now, message_id=str(message.id))
        if inserted:
            # 相手から新着があったら「未対応」に戻す（既に対応済みにしていた場合も含む）
            database.upsert_dm_thread(user_id, str(message.author), now, status="unhandled")
    except Exception as e:
        logger.error(f"DM返信の保存エラー: {e}", exc_info=True)
        return

    if not inserted:
        # 同じメッセージが既に保存済み（デプロイ切替時の重複イベント等）。通知も出さない
        return

    logger.info(f"DM返信受信: {message.author} ({user_id}) | {content[:50]}")

    if not DM_REPLY_CHANNEL_ID:
        logger.warning("DM返信通知NG: 環境変数 DM_REPLY_CHANNEL_ID が未設定（0）です")
        return

    channel = bot.get_channel(DM_REPLY_CHANNEL_ID)
    if channel is None:
        logger.warning(f"DM返信通知NG: DM_REPLY_CHANNEL_ID={DM_REPLY_CHANNEL_ID} のチャンネルが見つかりません"
              f"（IDが誤っているか、Botがそのチャンネルを閲覧できない可能性があります）")
        return

    try:
        embed = discord.Embed(
            title="📩 メッセージが届きました",
            description=content[:500],
            color=0xE8A13E,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"User ID: {user_id}")
        icon_url = message.author.display_avatar.url if message.author.display_avatar else None
        if icon_url:
            embed.set_author(name=str(message.author), icon_url=icon_url)
        else:
            embed.set_author(name=str(message.author))
        await channel.send(embed=embed)
    except Exception as e:
        logger.error(f"DM返信通知の送信エラー: {e}", exc_info=True)


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

        logger.info(f"ログイン成功: {bot.user} (ID: {bot.user.id})")

        guild = bot.get_guild(GUILD_ID)
        if guild is None:
            logger.warning(f"GUILD_ID={GUILD_ID} のサーバーが見つかりません")
            return

        _invite_cache = await _fetch_invite_cache(guild)
        logger.info(f"招待キャッシュ初期化: {len(_invite_cache)} 件")

        try:
            invites = await guild.invites()
            for inv in invites:
                creator_id = str(inv.inviter.id) if inv.inviter else "unknown"
                created_at = inv.created_at.isoformat() if inv.created_at else _now_iso()
                database.upsert_invite(inv.code, creator_id, created_at)
        except discord.Forbidden:
            logger.warning("招待リンク取得権限がありません")

        _last_audit_check = _load_last_audit_check()
        bot.loop.create_task(_audit_log_poller(bot))
        bot.loop.create_task(_retention_checker(bot))
        bot.loop.create_task(_activity_flusher(bot))
        bot.loop.create_task(_thread_keepalive_pinger(bot))
        logger.info("全タスク起動完了")

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

        logger.info(f"入室: {member} | 招待コード: {used_code or '不明'}")

        async def _record_initial_roles() -> None:
            await asyncio.sleep(600)
            try:
                updated = member.guild.get_member(member.id)
                if updated is None:
                    return
                role_names = [r.name for r in updated.roles if r.name != "@everyone"]
                database.update_user_initial_roles(str(member.id), role_names)
                logger.info(f"初期ロール記録: {member} → {role_names}")
            except Exception as e:
                logger.error(f"初期ロール記録エラー: {e}", exc_info=True)

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
                logger.info(f"即抜け検知: {member} | コード: {log['invite_code']}")

        logger.info(f"退室: {member}")

    @bot.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot:
            return

        # DM（サーバーに紐付かないメッセージ）は一斉DMへの返信として扱う
        if not isinstance(message.guild, discord.Guild):
            await _handle_dm_reply(bot, message)
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
                    logger.info(f"初回発言ロール付与: {member} → {role.name}")
            except Exception as e:
                logger.error(f"初回発言ロール付与エラー: {e}", exc_info=True)

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
                logger.error(f"activity_logsフラッシュエラー: {e}", exc_info=True)

        if _invite_msg_buffer:
            counts, _invite_msg_buffer = _invite_msg_buffer, {}
            try:
                database.bulk_increment_invite_messages(counts)
            except Exception as e:
                logger.error(f"invite集計フラッシュエラー: {e}", exc_info=True)


# ─────────────────────────────────────────────────────────
# 監査ログポーリング（60秒ごと）
# BAN/KICKのみ検知し punishments テーブルへ記録する
# （/stats の「退室理由の内訳」でKICK/BAN件数の集計に使われる）
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

                # ── KICK ─────────────────────────────────
                elif action == discord.AuditLogAction.kick:
                    punishment_type = "KICK"

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

                logger.info(
                    f"処罰検知: {punishment_type} | "
                    f"対象: {target_name} | 実行者: {executor} | 理由: {reason[:30]}"
                )

            # 実際に何かしら処罰の記録・削除が起きた回だけ、チェック時刻をDBへ永続化する
            # （毎回書き込むとNeonのコンピュートが常時起きた状態になりCU-hoursを消費するため）
            if had_activity:
                _save_last_audit_check(_last_audit_check)

        except discord.Forbidden:
            logger.warning("監査ログの読み取り権限がありません")
        except Exception as e:
            logger.error(f"監査ログポーリングエラー: {e}", exc_info=True)


# ─────────────────────────────────────────────────────────
# フォーラムスレッド キープアライブ
# 指定スレッドに固定メッセージを送信→約1秒後に削除する。
# 対象スレッドID・送信間隔はダッシュボードからいつでも変更できる（bot_stateに保存）。
# ─────────────────────────────────────────────────────────

DEFAULT_KEEPALIVE_INTERVAL_MIN = 60  # 未設定時のデフォルト（分）


def _get_keepalive_interval_sec() -> int:
    raw = database.get_setting("keepalive_interval_min")
    try:
        minutes = int(raw) if raw else DEFAULT_KEEPALIVE_INTERVAL_MIN
    except ValueError:
        minutes = DEFAULT_KEEPALIVE_INTERVAL_MIN
    minutes = max(minutes, 1)  # 1分未満は事故防止のため許可しない
    return minutes * 60


async def _thread_keepalive_pinger(bot: discord.Client) -> None:
    while not bot.is_closed():
        interval_sec = _get_keepalive_interval_sec()
        await asyncio.sleep(interval_sec)

        threads = database.get_keepalive_threads()
        if not threads:
            continue  # 未設定の間は何もしない

        now    = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=interval_sec * 0.5)

        for i, row in enumerate(threads):
            thread_id = row["thread_id"]

            # Renderのデプロイ切り替え等で複数プロセスが同時に動いていても
            # 二重送信しないよう、DB側で排他制御する
            claimed = database.try_claim_keepalive_send(
                thread_id, now.isoformat(), cutoff.isoformat()
            )
            if not claimed:
                logger.debug(f"キープアライブスキップ: スレッド(ID={thread_id})は直近に送信済みのためスキップ")
                continue

            try:
                thread = bot.get_channel(int(thread_id))
                if thread is None:
                    thread = await bot.fetch_channel(int(thread_id))
            except (discord.NotFound, discord.Forbidden) as e:
                logger.warning(f"キープアライブNG: スレッド(ID={thread_id})が見つからないか閲覧できません: {e}")
                continue
            except Exception as e:
                logger.error(f"キープアライブNG: スレッド取得エラー: {e}", exc_info=True)
                continue

            try:
                sent = await thread.send(THREAD_KEEPALIVE_MESSAGE)
                await asyncio.sleep(1)
                await sent.delete()
                logger.info(f"キープアライブ送信完了: スレッド(ID={thread_id})")
            except Exception as e:
                logger.error(f"キープアライブNG: 送信・削除エラー: {e}", exc_info=True)

            # 複数スレッドを一気に叩かないよう、間に少し間隔を空ける
            if i < len(threads) - 1:
                await asyncio.sleep(0.5)


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
            logger.error(f"定着率チェックエラー: {e}", exc_info=True)
