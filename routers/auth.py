"""
routers/auth.py
Discord OAuth2 ログイン・コールバック・ログアウト処理
"""

import logging
import os
import hashlib
import hmac
import json
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

# main.py から注入される
templates: Jinja2Templates = None  # type: ignore

router = APIRouter()

# ─── 設定 ─────────────────────────────────────────────────

CLIENT_ID     = os.getenv("DISCORD_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
REDIRECT_URI  = os.getenv("REDIRECT_URI", "http://localhost:8000/auth/callback")
SECRET_KEY    = os.getenv("SECRET_KEY", "changeme").encode()

# 管理者ロールをDiscord API（guilds.members.read）で毎回確認する方式は、
# サーバーメンバー情報取得エンドポイントのレート制限に引っかかりやすいため、
# 固定人数（3人）の管理者Discordユーザーidをそのまま許可リストとして持つ方式に変更。
# これによりログイン時はcode→token交換・/users/@me の2回のAPI呼び出しだけで完結する。
ADMIN_USER_IDS = {
    uid.strip() for uid in os.getenv("ADMIN_USER_IDS", "").split(",") if uid.strip()
}

# ─── 一時的なパスワードログイン ───────────────────────────
# DiscordのOAuth2 token交換がRenderの共有IPに対するグローバルレート制限(429)で
# 恒常的に失敗するようになったため、復旧までの一時措置としてパスワードのみで
# 管理者ログインできる経路を追加する。Discord APIを一切呼ばないため429の影響を受けない。
# 復旧後は環境変数 ADMIN_PASSWORD を削除すれば /login/password は自動的に無効化される。
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

DISCORD_API   = "https://discord.com/api/v10"
SESSION_COOKIE= "dashboard_session"
SESSION_DAYS  = 7

# OAuth2 スコープ（identify: ユーザー情報のみ。ロールはIDの許可リストで判定するため
# guilds.members.read は不要）
SCOPES = "identify"


# ─── セッション管理（署名付きCookie） ─────────────────────

def _sign(data: str) -> str:
    sig = hmac.new(SECRET_KEY, data.encode(), hashlib.sha256).hexdigest()
    return f"{data}.{sig}"


def _verify(token: str) -> dict | None:
    try:
        data, sig = token.rsplit(".", 1)
        expected = hmac.new(SECRET_KEY, data.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        return json.loads(data)
    except Exception:
        return None


def set_session(response: Response, payload: dict) -> None:
    data  = json.dumps(payload, separators=(",", ":"))
    token = _sign(data)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * SESSION_DAYS,
    )


def get_session(request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return _verify(token)


def clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE)


# ─── 認証チェックヘルパー ──────────────────────────────────

def require_auth(request: Request):
    """セッションを返す。未ログインなら None"""
    return get_session(request)


# ─── ルート ───────────────────────────────────────────────

@router.get("/debug/discord-ip-check", include_in_schema=False)
async def debug_discord_ip_check():
    """
    一時的な診断用エンドポイント。原因切り分けが済んだら削除してよい。
    認証不要のDiscord公開API（/gateway）を叩き、RenderのIPからDiscord API自体に
    到達できるかを見る。ここも429になるならIP全体がブロックされている。
    ここは200なのにOAuthのtoken交換だけ429になるなら、token交換エンドポイント
    （またはこのclient_id）に絞った制限がかかっている可能性が高い。
    """
    async with httpx.AsyncClient() as client:
        res = await client.get(f"{DISCORD_API}/gateway")
    return {"status_code": res.status_code, "body": res.text[:500]}


@router.head("/", include_in_schema=False)
async def root_head():
    """UptimeRobot等のヘルスチェック用（HEADリクエスト対応）"""
    from fastapi.responses import Response
    return Response(status_code=200)


@router.head("/login", include_in_schema=False)
async def login_head():
    """UptimeRobot等のヘルスチェック用（HEADリクエスト対応）"""
    from fastapi.responses import Response
    return Response(status_code=200)


@router.get("/login", include_in_schema=False)
async def login_page(request: Request):
    session = get_session(request)
    if session:
        return RedirectResponse("/admin/bulk-dm")
    return templates.TemplateResponse("login.html", {"request": request})


@router.get("/auth/discord", include_in_schema=False)
async def auth_discord():
    """Discord の OAuth2 認証ページにリダイレクト"""
    params = (
        f"client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&scope={SCOPES.replace(' ', '%20')}"
    )
    return RedirectResponse(
        f"https://discord.com/oauth2/authorize?{params}"
    )


@router.post("/login/password", include_in_schema=False)
async def login_password(request: Request, password: str = Form(...)):
    """一時的なパスワードログイン。

    Discord OAuth2のtoken交換がRenderの共有IPに対するグローバルレート制限で
    恒常的に失敗している間の暫定措置。Discord APIを一切呼ばないため429の影響を
    受けない。ADMIN_PASSWORD未設定なら常に無効（password_login_disabled）。
    """
    if not ADMIN_PASSWORD:
        return RedirectResponse("/login?error=password_login_disabled")

    if not hmac.compare_digest(password, ADMIN_PASSWORD):
        logger.warning("パスワードログイン失敗（パスワード不一致）")
        return RedirectResponse("/login?error=wrong_password")

    session_payload = {
        "user_id":   "password-login",
        "username":  "管理者（パスワードログイン）",
        "avatar":    "",
        "is_admin":  True,
        "logged_in": datetime.now(timezone.utc).isoformat(),
    }
    response = RedirectResponse("/admin/bulk-dm", status_code=302)
    set_session(response, session_payload)
    return response


@router.get("/auth/callback", include_in_schema=False)
async def auth_callback(request: Request, code: str = "", error: str = ""):
    """Discord からのコールバック処理"""

    if error or not code:
        return RedirectResponse("/login?error=cancelled")

    async with httpx.AsyncClient() as client:

        # 1. code → access_token 交換
        token_res = await client.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "client_id":     CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type":    "authorization_code",
                "code":          code,
                "redirect_uri":  REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if token_res.status_code != 200:
            logger.error(f"token交換失敗 status={token_res.status_code} body={token_res.text}")
            return RedirectResponse("/login?error=token_failed")

        token_data   = token_res.json()
        access_token = token_data.get("access_token", "")

        headers = {"Authorization": f"Bearer {access_token}"}

        # 2. ユーザー情報取得
        user_res = await client.get(f"{DISCORD_API}/users/@me", headers=headers)
        if user_res.status_code != 200:
            return RedirectResponse("/login?error=user_failed")

        user = user_res.json()
        user_id = user["id"]

        # 3. 管理者判定。サーバーメンバー情報取得API（レート制限に引っかかりやすい）
        #    は使わず、固定の管理者Discordユーザーid許可リストと突き合わせるだけ。
        is_admin = user_id in ADMIN_USER_IDS
        if not is_admin:
            return RedirectResponse("/login?error=no_permission")

    # 4. セッション発行
    session_payload = {
        "user_id":   user_id,
        "username":  user.get("username", ""),
        "avatar":    user.get("avatar", ""),
        "is_admin":  is_admin,
        "logged_in": datetime.now(timezone.utc).isoformat(),
    }

    response = RedirectResponse("/admin/bulk-dm", status_code=302)
    set_session(response, session_payload)
    return response


@router.get("/logout", include_in_schema=False)
async def logout():
    response = RedirectResponse("/login")
    clear_session(response)
    return response
