"""
routers/api.py
ダッシュボード・ユーザー検索・CSV出力・メモ保存の全エンドポイント
"""

import csv
import io
import json
import os
from datetime import datetime, timezone

import discord
from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

import database
from routers.auth import get_session

# main.py から注入される
templates: Jinja2Templates = None  # type: ignore
bot: discord.Client = None         # type: ignore

router = APIRouter()

GUILD_ID    = int(os.getenv("GUILD_ID", "0"))
MOD_ROLE_ID = int(os.getenv("MOD_ROLE_ID", "0"))


# ─── 認証ガード ────────────────────────────────────────────

def _check_auth(request: Request):
    """未ログインなら RedirectResponse を返す。ログイン済みなら session dict"""
    session = get_session(request)
    if not session:
        return RedirectResponse("/login")
    return session


# ─── DiscordギルドメンバーのロールIDリスト取得 ────────────

def _get_member_roles(user_id: str) -> list[str]:
    if bot is None:
        return []
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return []
    member = guild.get_member(int(user_id))
    if member is None:
        return []
    return [r.name for r in member.roles if r.name != "@everyone"]


def _get_guild_member_count() -> int:
    if bot is None:
        return 0
    guild = bot.get_guild(GUILD_ID)
    return guild.member_count if guild else 0


# ─────────────────────────────────────────────────────────
# ページルート
# ─────────────────────────────────────────────────────────

@router.get("/dashboard", include_in_schema=False)
async def dashboard_page(request: Request):
    result = _check_auth(request)
    if isinstance(result, RedirectResponse):
        return result
    session = result

    stats        = database.get_dashboard_stats()
    invites      = database.get_all_invites()
    ch_ranking   = database.get_channel_ranking(limit=10)
    recent_puns  = database.get_recent_punishments(limit=10)
    mod_stats    = database.get_all_mod_stats()

    # チャンネル名を解決（Botから取得）
    ch_ranking_named = []
    if bot:
        guild = bot.get_guild(GUILD_ID)
        for ch in ch_ranking:
            channel = guild.get_channel(int(ch["channel_id"])) if guild else None
            ch_ranking_named.append({
                "channel_id":   ch["channel_id"],
                "channel_name": f"#{channel.name}" if channel else ch["channel_id"],
                "msg_count":    ch["msg_count"],
            })
    else:
        ch_ranking_named = [
            {**ch, "channel_name": ch["channel_id"]} for ch in ch_ranking
        ]

    # サーバー現在人数をBotから取得
    stats["server_member_count"] = _get_guild_member_count()

    return templates.TemplateResponse("dashboard.html", {
        "request":      request,
        "session":      session,
        "stats":        stats,
        "invites":      invites,
        "ch_ranking":   ch_ranking_named,
        "recent_puns":  recent_puns,
        "mod_stats":    mod_stats,
    })


@router.get("/search", include_in_schema=False)
async def search_page(request: Request, q: str = ""):
    result = _check_auth(request)
    if isinstance(result, RedirectResponse):
        return result
    session = result

    users       = []
    target_user = None
    punishments = []
    join_log    = None
    live_roles  = []

    if q:
        users = database.search_users(q)
        if len(users) == 1:
            # 1件ヒットなら詳細表示
            target_user = users[0]
            uid         = target_user["user_id"]
            punishments = database.get_punishments_by_user(uid)
            join_log    = database.get_join_log_by_user(uid)
            live_roles  = _get_member_roles(uid)

            # initial_roles を JSON → list に変換
            try:
                target_user["initial_roles"] = json.loads(
                    target_user.get("initial_roles", "[]")
                )
            except (json.JSONDecodeError, TypeError):
                target_user["initial_roles"] = []

            # メッセージ数・VC時間も付加
            target_user["message_count"] = database.get_user_message_count(uid)
            target_user["vc_seconds"]    = database.get_user_vc_seconds(uid)

    return templates.TemplateResponse("search.html", {
        "request":     request,
        "session":     session,
        "query":       q,
        "users":       users,
        "target_user": target_user,
        "punishments": punishments,
        "join_log":    join_log,
        "live_roles":  live_roles,
    })


# ─────────────────────────────────────────────────────────
# API エンドポイント
# ─────────────────────────────────────────────────────────

@router.post("/api/invite/memo", include_in_schema=False)
async def update_memo(
    request: Request,
    code: str = Form(...),
    memo: str = Form(""),
):
    """招待リンクの用途メモを保存"""
    result = _check_auth(request)
    if isinstance(result, RedirectResponse):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    database.update_invite_memo(code, memo)
    return JSONResponse({"ok": True})


@router.post("/api/punishment/add", include_in_schema=False)
async def add_manual_punishment(
    request: Request,
    user_id:    str = Form(...),
    target_name:str = Form(...),
    reason:     str = Form(""),
):
    """手動警告メモを追記"""
    result = _check_auth(request)
    if isinstance(result, RedirectResponse):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    session = result
    executor = session.get("username", "運営")

    database.add_punishment(
        user_id=user_id,
        target_name=target_name,
        punishment_type="WARN",
        executor=executor,
        reason=reason,
        executed_at=datetime.now(timezone.utc).isoformat(),
    )
    database.increment_mod_warn(session.get("user_id", "unknown"))

    return JSONResponse({"ok": True})


# ─────────────────────────────────────────────────────────
# CSV ダウンロード
# ─────────────────────────────────────────────────────────

def _make_csv(rows: list[dict], filename: str) -> StreamingResponse:
    if not rows:
        output = io.StringIO()
        output.write("データなし\n")
        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv; charset=utf-8-sig",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    output.seek(0)

    # BOM付きUTF-8（Excelで文字化けしないように）
    bom = "\ufeff"
    return StreamingResponse(
        iter([bom + output.getvalue()]),
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/csv/users", include_in_schema=False)
async def csv_users(request: Request):
    result = _check_auth(request)
    if isinstance(result, RedirectResponse):
        return result
    rows = database.get_all_users_for_csv()
    return _make_csv(rows, "users.csv")


@router.get("/api/csv/punishments", include_in_schema=False)
async def csv_punishments(request: Request):
    result = _check_auth(request)
    if isinstance(result, RedirectResponse):
        return result
    rows = database.get_all_punishments_for_csv()
    return _make_csv(rows, "punishments.csv")


@router.get("/api/csv/invites", include_in_schema=False)
async def csv_invites(request: Request):
    result = _check_auth(request)
    if isinstance(result, RedirectResponse):
        return result
    rows = database.get_all_invites_for_csv()
    return _make_csv(rows, "invites.csv")


# ─────────────────────────────────────────────────────────
# エラーページ
# ─────────────────────────────────────────────────────────

@router.get("/error/403", include_in_schema=False)
async def error_403(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "アクセス権限がありません。運営ロールが必要です。"},
        status_code=403,
    )
