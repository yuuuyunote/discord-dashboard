"""
routers/api.py
ダッシュボード・ユーザー検索・CSV出力・メモ保存の全エンドポイント
追加：ノート複数追記・削除、入室数グラフ、招待別ユーザー一覧、定着率レポート
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

templates: Jinja2Templates = None
bot: discord.Client = None

router = APIRouter()

GUILD_ID    = int(os.getenv("GUILD_ID", "0"))
MOD_ROLE_ID = int(os.getenv("MOD_ROLE_ID", "0"))


def _check_auth(request: Request):
    session = get_session(request)
    if not session:
        return RedirectResponse("/login")
    return session


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


def _resolve_channel_name(channel_id: str) -> str:
    if bot is None:
        return channel_id
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return channel_id
    ch = guild.get_channel(int(channel_id))
    return f"#{ch.name}" if ch else channel_id


# ─────────────────────────────────────────────────────────
# ページルート
# ─────────────────────────────────────────────────────────

@router.get("/dashboard", include_in_schema=False)
async def dashboard_page(request: Request):
    result = _check_auth(request)
    if isinstance(result, RedirectResponse):
        return result
    session = result

    stats       = database.get_dashboard_stats()
    invites     = database.get_all_invites()
    ch_ranking  = database.get_channel_ranking(limit=10)
    recent_puns = database.get_recent_punishments(limit=10)
    mod_stats   = database.get_all_mod_stats()

    # 入室数グラフ用データ（30日分）
    join_graph_raw = database.get_join_logs_by_date(days=30)
    join_graph = {
        "labels": [str(r["date"]) for r in join_graph_raw],
        "data":   [r["count"] for r in join_graph_raw],
    }

    # チャンネル名解決
    ch_ranking_named = []
    for ch in ch_ranking:
        ch_ranking_named.append({
            "channel_id":   ch["channel_id"],
            "channel_name": _resolve_channel_name(ch["channel_id"]),
            "msg_count":    ch["msg_count"],
        })

    stats["server_member_count"] = _get_guild_member_count()
    return templates.TemplateResponse("dashboard.html", {
        "request":     request,
        "session":     session,
        "stats":       stats,
        "invites":     invites,
        "ch_ranking":  ch_ranking_named,
        "recent_puns": recent_puns,
        "mod_stats":   mod_stats,
        "join_graph":  json.dumps(join_graph, ensure_ascii=False),
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
    notes       = []

    if q:
        users = database.search_users(q)
        if len(users) == 1:
            target_user = users[0]
            uid         = target_user["user_id"]
            punishments = database.get_punishments_by_user(uid)
            join_log    = database.get_join_log_by_user(uid)
            live_roles  = _get_member_roles(uid)
            notes       = database.get_user_notes(uid)

            try:
                target_user["initial_roles"] = json.loads(
                    target_user.get("initial_roles", "[]")
                )
            except (json.JSONDecodeError, TypeError):
                target_user["initial_roles"] = []

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
        "notes":       notes,
    })


@router.get("/invite/{code}", include_in_schema=False)
async def invite_detail_page(request: Request, code: str):
    """招待リンク別ユーザー一覧ページ"""
    result = _check_auth(request)
    if isinstance(result, RedirectResponse):
        return result
    session = result

    invite  = database.get_invite(code)
    members = database.get_join_logs_by_invite(code)

    # 即抜け判定フラグを付加
    for m in members:
        if m.get("left_at") and m.get("joined_at"):
            from datetime import datetime
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


# ─────────────────────────────────────────────────────────
# API エンドポイント
# ─────────────────────────────────────────────────────────

@router.post("/api/invite/memo", include_in_schema=False)
async def update_memo(
    request: Request,
    code: str = Form(...),
    memo: str = Form(""),
):
    result = _check_auth(request)
    if isinstance(result, RedirectResponse):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    database.update_invite_memo(code, memo)
    return JSONResponse({"ok": True})


@router.post("/api/punishment/add", include_in_schema=False)
async def add_manual_punishment(
    request: Request,
    user_id:     str = Form(...),
    target_name: str = Form(...),
    reason:      str = Form(""),
):
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


@router.post("/api/note/add", include_in_schema=False)
async def add_note(
    request: Request,
    user_id:  str = Form(...),
    content:  str = Form(...),
):
    """ユーザーノートを追記"""
    result = _check_auth(request)
    if isinstance(result, RedirectResponse):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    session = result

    database.add_user_note(
        user_id=user_id,
        author_id=session.get("user_id", "unknown"),
        author_name=session.get("username", "運営"),
        content=content,
    )
    return JSONResponse({"ok": True})


@router.post("/api/note/delete", include_in_schema=False)
async def delete_note(
    request: Request,
    note_id: int = Form(...),
):
    """ユーザーノートを削除"""
    result = _check_auth(request)
    if isinstance(result, RedirectResponse):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    database.delete_user_note(note_id)
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
    return _make_csv(database.get_all_users_for_csv(), "users.csv")


@router.get("/api/csv/punishments", include_in_schema=False)
async def csv_punishments(request: Request):
    result = _check_auth(request)
    if isinstance(result, RedirectResponse):
        return result
    return _make_csv(database.get_all_punishments_for_csv(), "punishments.csv")


@router.get("/api/csv/invites", include_in_schema=False)
async def csv_invites(request: Request):
    result = _check_auth(request)
    if isinstance(result, RedirectResponse):
        return result
    return _make_csv(database.get_all_invites_for_csv(), "invites.csv")


@router.get("/error/403", include_in_schema=False)
async def error_403(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "アクセス権限がありません。運営ロールが必要です。"},
        status_code=403,
    )


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
# 機能34・36・37・38・39・45・46 追加エンドポイント
# ─────────────────────────────────────────────────────────

@router.get("/punishments", include_in_schema=False)
async def punishments_page(
    request: Request,
    type: str = "",
    executor: str = "",
    days: int = 0,
):
    """処罰履歴検索・フィルター画面（機能46）"""
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


@router.get("/api/stats/realtime", include_in_schema=False)
async def stats_realtime(request: Request):
    """統計カードのリアルタイム更新用API（機能45）"""
    result = _check_auth(request)
    if isinstance(result, RedirectResponse):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    stats = database.get_dashboard_stats()
    stats["server_member_count"] = _get_guild_member_count()
    return JSONResponse(stats)


# ─────────────────────────────────────────────────────────
# 新機能ページ
# ─────────────────────────────────────────────────────────

@router.get("/analytics", include_in_schema=False)
async def analytics_page(request: Request):
    """分析ページ（ヒートマップ・ランキング・新規ユーザー分析）"""
    result = _check_auth(request)
    if isinstance(result, RedirectResponse):
        return result
    session = result

    import json as _json

    # ヒートマップデータ
    heatmap_raw = database.get_activity_heatmap()
    heatmap = [[0] * 24 for _ in range(7)]
    for r in heatmap_raw:
        dow  = int(r["dow"])
        hour = int(r["hour"])
        heatmap[dow][hour] = int(r["cnt"])

    # 発言数ランキング
    ranking_30d  = database.get_user_message_ranking(period_days=30,  limit=10)
    ranking_all  = database.get_user_message_ranking(period_days=0,   limit=10)

    # 新規ユーザー分析
    new_stats_30 = database.get_new_user_stats(days=30)
    new_stats_7  = database.get_new_user_stats(days=7)

    # 招待作成者ランキング
    creator_ranking = database.get_invite_creator_ranking()

    # 退室理由統計
    leave_stats = database.get_leave_reason_stats()

    # Bot名を解決（creator_id → Discordユーザー名）
    for r in creator_ranking:
        if bot:
            guild  = bot.get_guild(GUILD_ID)
            member = guild.get_member(int(r["creator_id"])) if guild and r["creator_id"].isdigit() else None
            r["creator_name"] = member.display_name if member else r["creator_id"]
        else:
            r["creator_name"] = r["creator_id"]

    return templates.TemplateResponse("analytics.html", {
        "request":          request,
        "session":          session,
        "heatmap":          _json.dumps(heatmap),
        "ranking_30d":      ranking_30d,
        "ranking_all":      ranking_all,
        "new_stats_30":     new_stats_30,
        "new_stats_7":      new_stats_7,
        "creator_ranking":  creator_ranking,
        "leave_stats":      leave_stats,
    })


@router.get("/punishments", include_in_schema=False)
async def punishments_page(
    request: Request,
    type: str = "",
    executor: str = "",
    days: int = 0,
):
    """処罰履歴フィルター画面"""
    result = _check_auth(request)
    if isinstance(result, RedirectResponse):
        return result
    session = result

    punishments = database.get_punishments_filtered(
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


# ─── 統計APIエンドポイント（45：自動更新用） ──────────────

@router.get("/api/stats", include_in_schema=False)
async def api_stats(request: Request):
    """ダッシュボード統計をJSONで返す（自動更新用）"""
    result = _check_auth(request)
    if isinstance(result, RedirectResponse):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    stats = database.get_dashboard_stats()
    stats["server_member_count"] = _get_guild_member_count()
    return JSONResponse(stats)
