"""
main.py
FastAPI（uvicorn）を起動するダッシュボード専用のメインスクリプト。

以前はここでdiscord.Client（Gateway接続）も同時に起動していたが、Pterodactyl側
（bot_only.py）が本番のbotとして稼働するようになったため、Render側でも同じ
トークンでbotを起動すると二重接続になり、Discordのグローバルレート制限（429）に
達してOAuthログインまで巻き添えで失敗する事故が起きた。
そのため、Render側はGateway接続を持たない純粋なダッシュボードに切り離した。
管理者ロール判定・DM送信など、以前はdiscord.pyのクライアント経由でやっていた
処理は discord_rest.py 経由のREST API直接呼び出しに置き換えてある。
"""

import os
from datetime import datetime, timezone, timedelta

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import database
from routers import auth, api

# ─── 環境変数読み込み ──────────────────────────────────────
load_dotenv()

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))


# ─── FastAPI セットアップ ──────────────────────────────────

app = FastAPI(title="Community Dashboard")

app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")

# ─── カスタムフィルター（UTC→JST変換） ────────────────────

JST = timezone(timedelta(hours=9))

def to_jst(value: str, fmt: str = "%Y/%m/%d %H:%M") -> str:
    """ISO形式のUTC文字列をJST表示に変換"""
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(JST).strftime(fmt)
    except Exception:
        return str(value)[:16].replace("T", " ")

templates.env.filters["jst"] = to_jst

# ルーター登録
app.include_router(auth.router)
app.include_router(api.router)

auth.templates = templates
api.templates  = templates


# ─── 起動エントリーポイント ────────────────────────────────

def main() -> None:
    database.init_db()

    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()
