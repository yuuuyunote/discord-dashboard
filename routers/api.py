"""
routers/api.py
残す機能：分析（新規ユーザー分析・退室理由）、招待リンク別定着率レポート、
招待リンク詳細（即抜け率）、処罰履歴、一斉DM・個別チャット対応
"""

import os
import asyncio
from datetime import datetime, timezone

import discord
from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from routers.auth import get_session

templates: Jinja2Templates = None
bot: discord.Client = None

router = APIRouter()

GUILD_ID    = int(os.getenv("GUILD_ID", "0"))
MOD_ROLE_ID = int(os.getenv("MOD_ROLE_ID", "0"))
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0"))


def _check_auth(request: Request):
    session = get_session(request)
    if not session:
        return RedirectResponse("/login")
    return session


def _has_admin_role(user_id: str) -> bool:
    """運営ロール(MOD_ROLE_ID)より人数・権限を絞った管理者ロール(ADMIN_ROLE_ID)を持つか"""
    if bot is None:
        print("[Dashboard] 管理者ロール判定NG: Botインスタンス未初期化")
        return False

    if not ADMIN_ROLE_ID:
        print("[Dashboard] 管理者ロール判定NG: 環境変数 ADMIN_ROLE_ID が未設定（0）です")
        return False

    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        print(f"[Dashboard] 管理者ロール判定NG: GUILD_ID={GUILD_ID} のサーバーが見つかりません")
        return False

    member = guild.get_member(int(user_id))
    if member is None:
        print(f"[Dashboard] 管理者ロール判定NG: user_id={user_id} がメンバーキャッシュにありません")
        return False

    has_role = any(r.id == ADMIN_ROLE_ID for r in member.roles)
    if not has_role:
        role_ids = [r.id for r in member.roles]
        print(f"[Dashboard] 管理者ロール判定NG: user_id={user_id} は ADMIN_ROLE_ID={ADMIN_ROLE_ID} を保持していません（保有ロールID: {role_ids}）")
    return has_role


def _check_admin(request: Request):
    """通常のログインチェックに加え、管理者ロール保持者のみを許可する"""
    result = _check_auth(request)
    if isinstance(result, RedirectResponse):
        return result
    session = result
    if not _has_admin_role(session.get("user_id", "")):
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
    result = _check_admin(request)
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
    result = _check_admin(request)
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
    result = _check_admin(request)
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
    result = _check_admin(request)
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
    result = _check_admin(request)
    if isinstance(result, RedirectResponse):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

    if bot is None:
        return JSONResponse({"ok": False, "error": "bot_not_ready"}, status_code=503)

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
            user = await bot.fetch_user(int(uid))
            dm   = await user.create_dm()
            sent = await dm.send(message)
            results.append({"user_id": uid, "name": str(user), "ok": True, "reason": ""})

            now = datetime.now(timezone.utc).isoformat()
            try:
                database.add_dm_message(uid, "out", message, now, message_id=str(sent.id))
                database.upsert_dm_thread(uid, str(user), now, status="handled")
            except Exception as e:
                print(f"[Dashboard] 一斉DMのスレッド記録エラー: {e}")
        except discord.NotFound:
            results.append({"user_id": uid, "name": "", "ok": False, "reason": "ユーザーが見つかりません"})
        except discord.Forbidden:
            results.append({"user_id": uid, "name": "", "ok": False, "reason": "DMを送れません（拒否設定等）"})
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
    result = _check_admin(request)
    if isinstance(result, RedirectResponse):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    threads = database.get_dm_threads()
    return JSONResponse({"ok": True, "threads": threads})


@router.get("/admin/dm-thread", include_in_schema=False)
async def dm_thread_page(request: Request, user_id: str):
    """個別チャット画面"""
    result = _check_admin(request)
    if isinstance(result, RedirectResponse):
        return result
    session = result

    thread_user = None
    if bot is not None:
        try:
            thread_user = await bot.fetch_user(int(user_id))
        except Exception:
            thread_user = None

    return templates.TemplateResponse("dm_thread.html", {
        "request":  request,
        "session":  session,
        "user_id":  user_id,
        "username": str(thread_user) if thread_user else user_id,
        "avatar_url": thread_user.display_avatar.url if thread_user and thread_user.display_avatar else "",
    })


@router.get("/api/dm-thread/{user_id}", include_in_schema=False)
async def api_dm_thread_detail(request: Request, user_id: str):
    """特定ユーザーとのやりとり一覧をJSONで返す"""
    result = _check_admin(request)
    if isinstance(result, RedirectResponse):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    thread   = database.get_dm_thread(user_id)
    messages = database.get_dm_messages(user_id)
    return JSONResponse({"ok": True, "thread": thread, "messages": messages})


@router.post("/api/dm-thread/{user_id}/send", include_in_schema=False)
async def api_dm_thread_send(request: Request, user_id: str, message: str = Form(...)):
    """個別チャットから1件だけ返信を送る"""
    result = _check_admin(request)
    if isinstance(result, RedirectResponse):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

    if bot is None:
        return JSONResponse({"ok": False, "error": "bot_not_ready"}, status_code=503)

    message = message.strip()
    if not message:
        return JSONResponse({"ok": False, "error": "empty"}, status_code=400)

    try:
        user = await bot.fetch_user(int(user_id))
        dm   = await user.create_dm()
        sent = await dm.send(message)
    except discord.NotFound:
        return JSONResponse({"ok": False, "error": "ユーザーが見つかりません"}, status_code=404)
    except discord.Forbidden:
        return JSONResponse({"ok": False, "error": "DMを送れません（拒否設定等）"}, status_code=400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    now = datetime.now(timezone.utc).isoformat()
    database.add_dm_message(user_id, "out", message, now, message_id=str(sent.id))
    database.upsert_dm_thread(user_id, str(user), now, status="handled")

    return JSONResponse({"ok": True})


@router.post("/api/dm-thread/{user_id}/status", include_in_schema=False)
async def api_dm_thread_status(request: Request, user_id: str, status: str = Form(...)):
    """対応済み／未対応 の切り替え"""
    result = _check_admin(request)
    if isinstance(result, RedirectResponse):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

    if status not in ("handled", "unhandled"):
        return JSONResponse({"ok": False, "error": "invalid status"}, status_code=400)

    database.set_dm_thread_status(user_id, status)
    return JSONResponse({"ok": True})
