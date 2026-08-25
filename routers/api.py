"""
routers/api.py
残す機能：分析（新規ユーザー分析・退室理由）、招待リンク別定着率レポート、
招待リンク詳細（即抜け率）、処罰履歴、一斉DM・個別チャット対応
"""

import os
import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
import discord_rest
from routers.auth import get_session

templates: Jinja2Templates = None

router = APIRouter()

GUILD_ID    = os.getenv("GUILD_ID", "0")
MOD_ROLE_ID = os.getenv("MOD_ROLE_ID", "0")
ADMIN_ROLE_ID = os.getenv("ADMIN_ROLE_ID", "0")


def _check_auth(request: Request):
    session = get_session(request)
    if not session:
        return RedirectResponse("/login")
    return session


async def _has_admin_role(user_id: str) -> bool:
    """運営ロール(MOD_ROLE_ID)より人数・権限を絞った管理者ロール(ADMIN_ROLE_ID)を持つか。

    以前はGatewayが埋めるメンバーキャッシュ（bot.get_guild().get_member()）を見ていたが、
    Render側はGateway接続を持たない構成にしたため、REST APIを直接叩く形に変更した。
    """
    if not ADMIN_ROLE_ID or ADMIN_ROLE_ID == "0":
        print("[Dashboard] 管理者ロール判定NG: 環境変数 ADMIN_ROLE_ID が未設定（0）です")
        return False

    try:
        member = await discord_rest.get_guild_member(GUILD_ID, user_id)
    except discord_rest.DiscordRestError as e:
        print(f"[Dashboard] 管理者ロール判定NG: Discord API エラー ({e.status_code})")
        return False

    if member is None:
        print(f"[Dashboard] 管理者ロール判定NG: user_id={user_id} がサーバーのメンバーではありません")
        return False

    role_ids = member.get("roles", [])
    has_role = ADMIN_ROLE_ID in role_ids
    if not has_role:
        print(f"[Dashboard] 管理者ロール判定NG: user_id={user_id} は ADMIN_ROLE_ID={ADMIN_ROLE_ID} を保持していません（保有ロールID: {role_ids}）")
    return has_role


async def _check_admin(request: Request):
    """通常のログインチェックに加え、管理者ロール保持者のみを許可する"""
    result = _check_auth(request)
    if isinstance(result, RedirectResponse):
        return result
    session = result
    if not await _has_admin_role(session.get("user_id", "")):
        return RedirectResponse("/error/403")
    return session


# ─────────────────────────────────────────────────────────
# ページルート
# ─────────────────────────────────────────────────────────

@router.get("/invite/{code}", include_in_schema=False)
async def invite_detail_page(request: Request, code: str):
    """招待リンク別ユーザー一覧・即抜け率ページ"""
    result = _check_auth(request)
    if isinstance(result, RedirectResponse):
        return result
    session = result

    invite  = database.get_invite(code)
    members = database.get_join_logs_by_invite(code)

    # 即抜け判定フラグを付加
    for m in members:
        if m.get("left_at") and m.get("joined_at"):
            joined = datetime.fromisoformat(m["joined_at"])
            left   = datetime.fromisoformat(m["left_at"])
            m["instant_leave"] = (left - joined).total_seconds() < 86400
        else:
            m["instant_leave"] = False

    return templates.TemplateResponse("invite_detail.html", {
        "request": request,
        "session": session,
        "invite":  invite,
        "members": members,
        "code":    code,
    })


@router.get("/retention", include_in_schema=False)
async def retention_page(request: Request):
    """招待リンク別定着率レポート"""
    result = _check_auth(request)
    if isinstance(result, RedirectResponse):
        return result
    session = result

    stats = database.get_retention_stats_by_invite()

    return templates.TemplateResponse("retention.html", {
        "request": request,
        "session": session,
        "stats":   stats,
    })


@router.get("/analytics", include_in_schema=False)
async def analytics_page(request: Request):
    """分析ページ（新規ユーザー分析・退室理由の内訳）"""
    result = _check_auth(request)
    if isinstance(result, RedirectResponse):
        return result
    session = result

    # 新規ユーザー分析
    new_stats_30 = database.get_new_user_stats(days=30)
    new_stats_7  = database.get_new_user_stats(days=7)

    # 退室理由統計
    leave_stats = database.get_leave_reason_stats()

    return templates.TemplateResponse("analytics.html", {
        "request":      request,
        "session":      session,
        "new_stats_30": new_stats_30,
        "new_stats_7":  new_stats_7,
        "leave_stats":  leave_stats,
    })


@router.get("/punishments", include_in_schema=False)
async def punishments_page(
    request: Request,
    type: str = "",
    executor: str = "",
    days: int = 0,
):
    """処罰履歴検索・フィルター画面"""
    result = _check_auth(request)
    if isinstance(result, RedirectResponse):
        return result
    session = result

    punishments = database.get_punishment_filtered(
        punishment_type=type,
        executor=executor,
        days=days,
        limit=100,
    )

    return templates.TemplateResponse("punishments.html", {
        "request":     request,
        "session":     session,
        "punishments": punishments,
        "filter_type": type,
        "filter_executor": executor,
        "filter_days": days,
    })


@router.get("/error/403", include_in_schema=False)
async def error_403(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "アクセス権限がありません。運営ロールが必要です。"},
        status_code=403,
    )


# ─────────────────────────────────────────────────────────
# API エンドポイント
# ─────────────────────────────────────────────────────────

@router.post("/api/punishment/delete", include_in_schema=False)
async def delete_punishment(
    request: Request,
    punishment_id: int = Form(...),
):
    """処罰記録を削除（誤検知対応）"""
    result = _check_auth(request)
    if isinstance(result, RedirectResponse):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    database.delete_punishment(punishment_id)
    return JSONResponse({"ok": True})


# ─────────────────────────────────────────────────────────
# 一斉DM送信（管理者ロール限定・メニューには出さない）
# ─────────────────────────────────────────────────────────

@router.get("/admin/bulk-dm", include_in_schema=False)
async def bulk_dm_page(request: Request):
    result = await _check_admin(request)
    if isinstance(result, RedirectResponse):
        return result
    session = result

    keepalive_threads      = database.get_keepalive_threads()
    keepalive_interval_min = database.get_setting("keepalive_interval_min") or "60"

    return templates.TemplateResponse("bulk_dm.html", {
        "request": request,
        "session": session,
        "keepalive_threads": keepalive_threads,
        "keepalive_interval_min": keepalive_interval_min,
    })


@router.post("/api/keepalive-interval", include_in_schema=False)
async def set_keepalive_interval(request: Request, interval_min: str = Form(...)):
    """フォーラムキープアライブの送信間隔（分）を変更する（全スレッド共通）"""
    result = await _check_admin(request)
    if isinstance(result, RedirectResponse):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

    interval_min = interval_min.strip()
    if not interval_min.isdigit() or int(interval_min) < 1:
        return JSONResponse({"ok": False, "error": "間隔は1分以上の数字で入力してください"}, status_code=400)

    database.set_setting("keepalive_interval_min", interval_min)
    return JSONResponse({"ok": True})


@router.post("/api/keepalive-thread/add", include_in_schema=False)
async def add_keepalive_thread(
    request: Request,
    thread_id: str = Form(...),
    label: str = Form(""),
):
    """フォーラムキープアライブの対象スレッドを追加する"""
    result = await _check_admin(request)
    if isinstance(result, RedirectResponse):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

    thread_id = thread_id.strip()
    label     = label.strip()

    if not thread_id or not thread_id.isdigit():
        return JSONResponse({"ok": False, "error": "スレッドIDは数字のみで入力してください"}, status_code=400)

    now = datetime.now(timezone.utc).isoformat()
    database.add_keepalive_thread(thread_id, label, now)
    return JSONResponse({"ok": True})


@router.post("/api/keepalive-thread/remove", include_in_schema=False)
async def remove_keepalive_thread(request: Request, thread_id: str = Form(...)):
    """フォーラムキープアライブの対象スレッドを削除する"""
    result = await _check_admin(request)
    if isinstance(result, RedirectResponse):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

    database.remove_keepalive_thread(thread_id.strip())
    return JSONResponse({"ok": True})


@router.post("/api/bulk-dm/send", include_in_schema=False)
async def bulk_dm_send(
    request: Request,
    recipients: str = Form(...),
    message: str = Form(...),
):
    result = await _check_admin(request)
    if isinstance(result, RedirectResponse):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

    ids: list[str] = []
    for line in recipients.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.isdigit():
            ids.append(line)

    seen: set[str] = set()
    unique_ids: list[str] = []
    for uid in ids:
        if uid not in seen:
            seen.add(uid)
            unique_ids.append(uid)

    message = message.strip()
    if not unique_ids or not message:
        return JSONResponse({"ok": False, "error": "empty"}, status_code=400)

    results = []
    for i, uid in enumerate(unique_ids):
        try:
            user = await discord_rest.get_user(uid)
            if user is None:
                results.append({"user_id": uid, "name": "", "ok": False, "reason": "ユーザーが見つかりません"})
                continue

            sent = await discord_rest.send_dm(uid, message)
            display_name = user.get("global_name") or user.get("username", uid)
            results.append({"user_id": uid, "name": display_name, "ok": True, "reason": ""})

            now = datetime.now(timezone.utc).isoformat()
            try:
                database.add_dm_message(uid, "out", message, now, message_id=str(sent["id"]))
                database.upsert_dm_thread(uid, display_name, now, status="handled")
            except Exception as e:
                print(f"[Dashboard] 一斉DMのスレッド記録エラー: {e}")
        except discord_rest.DiscordRestError as e:
            reason = "DMを送れません（拒否設定等）" if e.status_code == 403 else f"送信エラー（status: {e.status_code}）"
            results.append({"user_id": uid, "name": "", "ok": False, "reason": reason})
        except Exception as e:
            results.append({"user_id": uid, "name": "", "ok": False, "reason": str(e)})

        # レート制限・スパム判定対策
        if i < len(unique_ids) - 1:
            await asyncio.sleep(1.2)

    success_count = sum(1 for r in results if r["ok"])
    return JSONResponse({
        "ok": True,
        "success_count": success_count,
        "failed_count": len(results) - success_count,
        "results": results,
    })


@router.get("/api/dm-threads", include_in_schema=False)
async def api_dm_threads(request: Request):
    """個別対応スレッドの一覧をJSONで返す"""
    result = await _check_admin(request)
    if isinstance(result, RedirectResponse):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    threads = database.get_dm_threads()
    return JSONResponse({"ok": True, "threads": threads})


@router.get("/admin/dm-thread", include_in_schema=False)
async def dm_thread_page(request: Request, user_id: str):
    """個別チャット画面"""
    result = await _check_admin(request)
    if isinstance(result, RedirectResponse):
        return result
    session = result

    thread_user = None
    try:
        thread_user = await discord_rest.get_user(user_id)
    except discord_rest.DiscordRestError:
        thread_user = None

    display_name = (thread_user.get("global_name") or thread_user.get("username")) if thread_user else user_id
    avatar_hash = thread_user.get("avatar") if thread_user else None
    avatar_url = (
        f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.png"
        if thread_user and avatar_hash
        else ""
    )

    return templates.TemplateResponse("dm_thread.html", {
        "request":  request,
        "session":  session,
        "user_id":  user_id,
        "username": display_name,
        "avatar_url": avatar_url,
    })


@router.get("/api/dm-thread/{user_id}", include_in_schema=False)
async def api_dm_thread_detail(request: Request, user_id: str):
    """特定ユーザーとのやりとり一覧をJSONで返す"""
    result = await _check_admin(request)
    if isinstance(result, RedirectResponse):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    thread   = database.get_dm_thread(user_id)
    messages = database.get_dm_messages(user_id)
    return JSONResponse({"ok": True, "thread": thread, "messages": messages})


@router.post("/api/dm-thread/{user_id}/send", include_in_schema=False)
async def api_dm_thread_send(request: Request, user_id: str, message: str = Form(...)):
    """個別チャットから1件だけ返信を送る"""
    result = await _check_admin(request)
    if isinstance(result, RedirectResponse):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

    message = message.strip()
    if not message:
        return JSONResponse({"ok": False, "error": "empty"}, status_code=400)

    try:
        user = await discord_rest.get_user(user_id)
        if user is None:
            return JSONResponse({"ok": False, "error": "ユーザーが見つかりません"}, status_code=404)
        sent = await discord_rest.send_dm(user_id, message)
    except discord_rest.DiscordRestError as e:
        if e.status_code == 403:
            return JSONResponse({"ok": False, "error": "DMを送れません（拒否設定等）"}, status_code=400)
        return JSONResponse({"ok": False, "error": f"送信エラー（status: {e.status_code}）"}, status_code=500)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    display_name = user.get("global_name") or user.get("username", user_id)
    now = datetime.now(timezone.utc).isoformat()
    database.add_dm_message(user_id, "out", message, now, message_id=str(sent["id"]))
    database.upsert_dm_thread(user_id, display_name, now, status="handled")

    return JSONResponse({"ok": True})


@router.post("/api/dm-thread/{user_id}/status", include_in_schema=False)
async def api_dm_thread_status(request: Request, user_id: str, status: str = Form(...)):
    """対応済み／未対応 の切り替え"""
    result = await _check_admin(request)
    if isinstance(result, RedirectResponse):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

    if status not in ("handled", "unhandled"):
        return JSONResponse({"ok": False, "error": "invalid status"}, status_code=400)

    database.set_dm_thread_status(user_id, status)
    return JSONResponse({"ok": True})
