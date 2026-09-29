"""
bot/events.py
Botイベントハンドラ（残す機能のみ）
  - DM返信の受信（個別チャットへ保存＋通知チャンネルへ転送）
  - フォーラムスレッド キープアライブ（自動送信）
  - 初回発言ロール自動付与
"""

import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta

import discord

import database

logger = logging.getLogger(__name__)

GUILD_ID            = int(os.getenv("GUILD_ID", "0"))
FIRST_MSG_ROLE_ID   = int(os.getenv("FIRST_MSG_ROLE_ID", "0"))
DM_REPLY_CHANNEL_ID = int(os.getenv("DM_REPLY_CHANNEL_ID", "0"))

THREAD_KEEPALIVE_MESSAGE = "."


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────
# DM返信の受信
# ─────────────────────────────────────────────────────────

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


def setup_events(bot: discord.Client) -> None:

    @bot.event
    async def on_ready() -> None:
        logger.info(f"ログイン成功: {bot.user} (ID: {bot.user.id})")
        bot.loop.create_task(_thread_keepalive_pinger(bot))
        logger.info("全タスク起動完了")

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
        if message.type != discord.MessageType.default:
            return

        # 初回発言ロール自動付与
        user_id = str(message.author.id)
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
