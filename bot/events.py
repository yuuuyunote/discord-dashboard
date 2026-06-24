"""
bot/events.py
Discord Bot の全イベントハンドラ

- 招待リンクトラッカー（誰がどのリンクから入ったか）
- オンボーディング後ロール記録（入室10分後）
- 発言アクティビティ記録
- VC入退室記録
- 監査ログポーリング（30秒ごと・BAN/キック/タイムアウト自動検知）
"""

import asyncio
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord

import database

GUILD_ID   = int(os.getenv("GUILD_ID", "0"))
MOD_ROLE_ID= int(os.getenv("MOD_ROLE_ID", "0"))

# 監査ログポーリング間隔（秒）
AUDIT_POLL_INTERVAL = 30

# 起動時の招待キャッシュ { code: uses }
_invite_cache: dict[str, int] = {}

# 監査ログの最終確認時刻
_last_audit_check: datetime = datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────
# ヘルパー
# ─────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _fetch_invite_cache(guild: discord.Guild) -> dict[str, int]:
    """招待リンクの現在の使用回数を取得してキャッシュ用dictを返す"""
    try:
        invites = await guild.invites()
        return {inv.code: inv.uses or 0 for inv in invites}
    except discord.Forbidden:
        return {}


def _find_used_invite(
    before: dict[str, int],
    after: dict[str, int]
) -> Optional[str]:
    """使用回数が増えた招待コードを特定する"""
    for code, uses in after.items():
        if uses > before.get(code, 0):
            return code
    return None


# ─────────────────────────────────────────────────────────
# イベント登録
# ─────────────────────────────────────────────────────────

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

        # 招待キャッシュ初期化
        _invite_cache = await _fetch_invite_cache(guild)
        print(f"[Bot] 招待キャッシュ初期化: {len(_invite_cache)} 件")

        # 招待リンク情報をDBに同期
        try:
            invites = await guild.invites()
            for inv in invites:
                creator_id = str(inv.inviter.id) if inv.inviter else "unknown"
                created_at = inv.created_at.isoformat() if inv.created_at else _now_iso()
                database.upsert_invite(inv.code, creator_id, created_at)
        except discord.Forbidden:
            print("[Bot] 警告: 招待リンク取得権限がありません")

        # 監査ログポーリング開始
        _last_audit_check = datetime.now(timezone.utc)
        bot.loop.create_task(_audit_log_poller(bot))
        print("[Bot] 監査ログポーリング開始")

    # ── メンバー入室 ────────────────────────────────────

    @bot.event
    async def on_member_join(member: discord.Member) -> None:
        global _invite_cache

        if member.guild.id != GUILD_ID:
            return

        joined_at = member.joined_at.isoformat() if member.joined_at else _now_iso()

        # ユーザー情報をDBに保存
        account_created = member.created_at.isoformat()
        database.upsert_user(
            user_id=str(member.id),
            username=str(member),
            account_created=account_created,
            joined_at=joined_at,
        )

        # 招待リンク特定
        new_cache  = await _fetch_invite_cache(member.guild)
        used_code  = _find_used_invite(_invite_cache, new_cache)
        _invite_cache = new_cache

        # 入室ログ記録
        database.add_join_log(
            user_id=str(member.id),
            invite_code=used_code,
            joined_at=joined_at,
        )

        print(f"[Bot] 入室: {member} | 招待コード: {used_code or '不明'}")

        # 10分後にロールを記録（オンボーディング完了後を想定）
        async def _record_initial_roles() -> None:
            await asyncio.sleep(600)  # 10分待機
            try:
                # 最新のメンバー情報を再取得
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
            # 即抜け判定（24時間以内）
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

        database.add_activity_log(
            user_id=str(message.author.id),
            channel_id=str(message.channel.id),
            sent_at=_now_iso(),
        )

        # 招待リンク別の発言数を更新
        log = database.get_join_log_by_user(str(message.author.id))
        if log and log.get("invite_code"):
            database.update_invite_activity(log["invite_code"], messages=1, vc_sec=0)

    # ── VC 入退室 ────────────────────────────────────────

    @bot.event
    async def on_voice_state_update(
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.guild.id != GUILD_ID:
            return
        if member.bot:
            return

        now = _now_iso()

        # VCに入室した
        if before.channel is None and after.channel is not None:
            database.add_voice_join(str(member.id), now)

        # VCから退出した
        elif before.channel is not None and after.channel is None:
            vc_sec = database.record_voice_leave(str(member.id), now)
            if vc_sec is not None and vc_sec > 0:
                # 招待リンク別のVC時間を更新
                log = database.get_join_log_by_user(str(member.id))
                if log and log.get("invite_code"):
                    database.update_invite_activity(
                        log["invite_code"], messages=0, vc_sec=vc_sec
                    )

    # ── 招待リンク作成 ──────────────────────────────────

    @bot.event
    async def on_invite_create(invite: discord.Invite) -> None:
        if invite.guild is None or invite.guild.id != GUILD_ID:
            return

        creator_id = str(invite.inviter.id) if invite.inviter else "unknown"
        created_at = invite.created_at.isoformat() if invite.created_at else _now_iso()
        database.upsert_invite(invite.code, creator_id, created_at)

        # キャッシュ更新
        _invite_cache[invite.code] = 0
        print(f"[Bot] 招待リンク作成: {invite.code} by {invite.inviter}")

    # ── 招待リンク削除 ──────────────────────────────────

    @bot.event
    async def on_invite_delete(invite: discord.Invite) -> None:
        _invite_cache.pop(invite.code, None)
        print(f"[Bot] 招待リンク削除: {invite.code}")


# ─────────────────────────────────────────────────────────
# 監査ログポーリング（30秒ごと）
# ─────────────────────────────────────────────────────────

async def _audit_log_poller(bot: discord.Client) -> None:
    """
    30秒ごとに監査ログを取得し、BAN/キック/タイムアウトを検知して
    punishments テーブルに記録する
    """
    global _last_audit_check

    # 処罰種別マッピング
    ACTION_MAP = {
        discord.AuditLogAction.ban:              "BAN",
        discord.AuditLogAction.kick:             "KICK",
        discord.AuditLogAction.member_update:    "TIMEOUT",  # タイムアウトはmember_update
    }

    while not bot.is_closed():
        await asyncio.sleep(AUDIT_POLL_INTERVAL)

        guild = bot.get_guild(GUILD_ID)
        if guild is None:
            continue

        try:
            check_after = _last_audit_check
            _last_audit_check = datetime.now(timezone.utc)

            async for entry in guild.audit_logs(limit=50):
                # 最終確認時刻より古いエントリはスキップ
                entry_time = entry.created_at.replace(tzinfo=timezone.utc)
                if entry_time <= check_after:
                    break

                action = entry.action

                # タイムアウトの場合は timed_out_until が設定されているか確認
                if action == discord.AuditLogAction.member_update:
                    changes = entry.changes
                    after_vals = {
                        change.key: change.new_value
                        for change in (changes.after if changes.after else [])
                    } if hasattr(changes, 'after') else {}
                    if "timed_out_until" not in str(changes):
                        continue  # タイムアウト以外のmember_updateは無視
                    punishment_type = "TIMEOUT"
                elif action in ACTION_MAP:
                    punishment_type = ACTION_MAP[action]
                else:
                    continue

                # 対象ユーザー
                target = entry.target
                if target is None:
                    continue

                target_id   = str(target.id)
                target_name = str(target) if hasattr(target, "name") else str(target.id)
                executor    = str(entry.user) if entry.user else "Unknown"
                reason      = entry.reason or ""
                executed_at = entry_time.isoformat()

                # 重複記録防止（同一ユーザー・同一種別・同一時刻）
                existing = database.get_punishments_by_user(target_id)
                already  = any(
                    p["executed_at"] == executed_at and p["punishment_type"] == punishment_type
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

                # 実行者が運営メンバーなら実績カウント
                if entry.user:
                    member = guild.get_member(entry.user.id)
                    if member and any(r.id == MOD_ROLE_ID for r in member.roles):
                        database.increment_mod_audit(str(entry.user.id))

                print(
                    f"[Bot] 処罰検知: {punishment_type} | "
                    f"対象: {target_name} | 実行者: {executor}"
                )

        except discord.Forbidden:
            print("[Bot] 警告: 監査ログの読み取り権限がありません")
        except Exception as e:
            print(f"[Bot] 監査ログポーリングエラー: {e}")
