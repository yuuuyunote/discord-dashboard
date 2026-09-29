"""
database.py
Neon（PostgreSQL）対応版
残す機能：bot_state / フォーラムスレッド キープアライブ / DM（一斉DM・個別チャット） /
初回発言ロール / 通報（/report）
"""

import os
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool

logger = logging.getLogger(__name__)

# 以前は呼び出しごとにpsycopg2.connect()していたため、複数回のDB呼び出しが続く処理で
# その都度TLSハンドシェイクと
# Neon（サーバーレスPostgres）側のコールドスタートが重なり体感速度を悪化させていた。
# 常時1本以上のコネクションをプールで維持することで新規接続そのものを減らし、
# 副次的にNeon側のコンピュートが自動サスペンドしにくくなる効果もある。
_pool: Optional["psycopg2.pool.ThreadedConnectionPool"] = None


def _get_database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        try:
            with open("/home/container/.env", "r") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("DATABASE_URL="):
                        url = line[len("DATABASE_URL="):]
                        break
        except Exception:
            pass
    if not url:
        raise ValueError("DATABASE_URL が設定されていません")
    return url


def _get_pool() -> "psycopg2.pool.ThreadedConnectionPool":
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            1,
            5,
            dsn=_get_database_url(),
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
    return _pool


def get_conn():
    """プールから接続を1本借りる。使い終わったら release_conn() で必ず返すこと。"""
    return _get_pool().getconn()


def release_conn(conn, *, discard: bool = False) -> None:
    """
    借りた接続をプールに返す。
    discard=True の場合はプールに戻さず破棄する（Neon側でアイドルタイムアウト等に
    より既に切断されていた接続を、壊れたままプールに戻さないようにするため）。
    """
    _get_pool().putconn(conn, close=discard)


def _run(conn, sql: str, args: tuple) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, args)
        conn.commit()
        try:
            return [dict(r) for r in cur.fetchall()]
        except psycopg2.ProgrammingError:
            return []


def _execute(sql: str, args: tuple = ()) -> list[dict]:
    conn = get_conn()
    try:
        result = _run(conn, sql, args)
    except psycopg2.OperationalError:
        # Neon側のアイドルサスペンド等でプール内の接続が切れていた場合、
        # その接続は破棄して1回だけ新規接続で取り直す。
        logger.warning("stale DB connection detected, reconnecting and retrying once")
        release_conn(conn, discard=True)
        conn = get_conn()
        try:
            result = _run(conn, sql, args)
        except Exception:
            release_conn(conn, discard=True)
            raise
        release_conn(conn)
        return result
    else:
        release_conn(conn)
        return result


def _execute_many(statements: list[tuple]) -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for sql, args in statements:
                cur.execute(sql, args)
        conn.commit()
    except psycopg2.OperationalError:
        logger.warning("stale DB connection detected, reconnecting and retrying once")
        release_conn(conn, discard=True)
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                for sql, args in statements:
                    cur.execute(sql, args)
            conn.commit()
        except Exception:
            release_conn(conn, discard=True)
            raise
        release_conn(conn)
        return
    else:
        release_conn(conn)


# ─────────────────────────────────────────────────────────
# テーブル初期化
# ─────────────────────────────────────────────────────────

def init_db() -> None:
    stmts = [
        # Bot再起動をまたいで状態を保持する汎用キーバリュー
        ("""CREATE TABLE IF NOT EXISTS bot_state (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""", ()),

        # フォーラムスレッド キープアライブの対象スレッド一覧（複数登録可）
        ("""CREATE TABLE IF NOT EXISTS keepalive_threads (
            thread_id    TEXT PRIMARY KEY,
            label        TEXT NOT NULL DEFAULT '',
            added_at     TEXT NOT NULL,
            last_sent_at TEXT
        )""", ()),
        ("ALTER TABLE keepalive_threads ADD COLUMN IF NOT EXISTS last_sent_at TEXT", ()),

        # 個別チャット形式でのDM対応管理
        ("""CREATE TABLE IF NOT EXISTS dm_threads (
            user_id         TEXT PRIMARY KEY,
            username        TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'unhandled',
            last_message_at TEXT NOT NULL
        )""", ()),
        ("""CREATE TABLE IF NOT EXISTS dm_messages (
            id         SERIAL PRIMARY KEY,
            user_id    TEXT NOT NULL,
            direction  TEXT NOT NULL,
            content    TEXT NOT NULL,
            sent_at    TEXT NOT NULL,
            message_id TEXT
        )""", ()),
        ("CREATE UNIQUE INDEX IF NOT EXISTS idx_dmmsg_msgid ON dm_messages(message_id)", ()),
        ("CREATE INDEX IF NOT EXISTS idx_dmmsg_user ON dm_messages(user_id)", ()),
        # 初回発言ロール付与済みユーザー
        ("""CREATE TABLE IF NOT EXISTS first_message_granted (
            user_id     TEXT PRIMARY KEY,
            granted_at  TEXT NOT NULL
        )""", ()),

        # 通報の受付〜承認/却下（/report）
        # categoriesはinitial_rolesと同じ流儀でJSON文字列としてTEXTに入れる
        ("""CREATE TABLE IF NOT EXISTS reports (
            id                             SERIAL PRIMARY KEY,
            reporter_id                    TEXT NOT NULL,
            reporter_username              TEXT NOT NULL,
            target_id                      TEXT NOT NULL,
            target_username                TEXT NOT NULL,
            categories                     TEXT NOT NULL DEFAULT '[]',
            note                           TEXT,
            status                         TEXT NOT NULL DEFAULT 'pending',
            maintainer_channel_message_id  TEXT,
            rejection_reason               TEXT,
            created_at                     TEXT NOT NULL,
            decided_at                     TEXT
        )""", ()),
        # 既存テーブルに対するマイグレーション（user/server/bot拡張分）
        # target_typeが無い既存行は'user'扱いにする（このカラム追加以前は
        # user通報しか存在しなかったため、デフォルト値がそのまま正しい）
        ("ALTER TABLE reports ADD COLUMN IF NOT EXISTS target_type TEXT NOT NULL DEFAULT 'user'", ()),
        # server通報のcreator_id / bot通報のdeveloper_idを共用する任意カラム
        ("ALTER TABLE reports ADD COLUMN IF NOT EXISTS creator_or_developer_id TEXT", ()),

        # インデックス
        ("CREATE INDEX IF NOT EXISTS idx_reports_reporter    ON reports(reporter_id)", ()),
        ("CREATE INDEX IF NOT EXISTS idx_reports_target      ON reports(target_id)", ()),
        ("CREATE INDEX IF NOT EXISTS idx_reports_status      ON reports(status)", ()),
        ("CREATE INDEX IF NOT EXISTS idx_reports_target_type ON reports(target_type)", ()),

    ]
    _execute_many(stmts)


# ─────────────────────────────────────────────────────────
# bot_state（Bot再起動をまたぐ永続状態）
# ─────────────────────────────────────────────────────────

def get_setting(key: str) -> Optional[str]:
    rows = _execute("SELECT value FROM bot_state WHERE key = %s", (key,))
    return rows[0]["value"] if rows else None


def set_setting(key: str, value: str) -> None:
    _execute(
        "INSERT INTO bot_state (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, value),
    )


# ─────────────────────────────────────────────────────────
# keepalive_threads（フォーラムスレッド キープアライブの対象一覧）
# ─────────────────────────────────────────────────────────

def add_keepalive_thread(thread_id: str, label: str, added_at: str) -> None:
    _execute(
        "INSERT INTO keepalive_threads (thread_id, label, added_at) VALUES (%s,%s,%s) "
        "ON CONFLICT (thread_id) DO UPDATE SET label = EXCLUDED.label",
        (thread_id, label, added_at),
    )


def remove_keepalive_thread(thread_id: str) -> None:
    _execute("DELETE FROM keepalive_threads WHERE thread_id = %s", (thread_id,))


def get_keepalive_threads() -> list[dict]:
    return _execute(
        "SELECT thread_id, label, added_at FROM keepalive_threads ORDER BY added_at ASC"
    )


def try_claim_keepalive_send(thread_id: str, now_iso: str, cutoff_iso: str) -> bool:
    """
    Renderのデプロイ切り替え時などに複数プロセスが同時に動いていても
    二重送信しないための排他制御。
    last_sent_atがNULL、またはcutoff_isoより古い場合のみ更新に成功しTrueを返す。
    （他プロセスが先にこの行を更新済みなら、WHERE条件に一致せずFalseになる）
    """
    rows = _execute(
        "UPDATE keepalive_threads SET last_sent_at = %s "
        "WHERE thread_id = %s AND (last_sent_at IS NULL OR last_sent_at < %s) "
        "RETURNING thread_id",
        (now_iso, thread_id, cutoff_iso),
    )
    return bool(rows)


# ─────────────────────────────────────────────────────────
# dm_threads / dm_messages（個別DM対応のチャットスレッド）
# ─────────────────────────────────────────────────────────

def upsert_dm_thread(user_id: str, username: str, last_message_at: str, status: Optional[str] = None) -> None:
    """
    スレッドを作成、または最終メッセージ日時・ユーザー名を更新する。
    status を指定した場合のみ対応状況も上書きする（省略時は既存の状態を維持）。
    """
    if status is None:
        _execute(
            "INSERT INTO dm_threads (user_id, username, status, last_message_at) "
            "VALUES (%s,%s,'unhandled',%s) "
            "ON CONFLICT (user_id) DO UPDATE SET "
            "username = EXCLUDED.username, last_message_at = EXCLUDED.last_message_at",
            (user_id, username, last_message_at),
        )
    else:
        _execute(
            "INSERT INTO dm_threads (user_id, username, status, last_message_at) "
            "VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (user_id) DO UPDATE SET "
            "username = EXCLUDED.username, status = EXCLUDED.status, last_message_at = EXCLUDED.last_message_at",
            (user_id, username, status, last_message_at),
        )


def set_dm_thread_status(user_id: str, status: str) -> None:
    _execute("UPDATE dm_threads SET status = %s WHERE user_id = %s", (status, user_id))


def get_dm_threads() -> list[dict]:
    return _execute(
        "SELECT user_id, username, status, last_message_at "
        "FROM dm_threads ORDER BY last_message_at DESC"
    )


def get_dm_thread(user_id: str) -> Optional[dict]:
    rows = _execute(
        "SELECT user_id, username, status, last_message_at FROM dm_threads WHERE user_id = %s",
        (user_id,),
    )
    return rows[0] if rows else None


def add_dm_message(
    user_id: str, direction: str, content: str, sent_at: str, message_id: Optional[str] = None
) -> bool:
    """
    direction: 'in'（相手から） / 'out'（こちらから）
    戻り値: 実際に新規保存されたらTrue、同じmessage_idで重複ならFalse
    """
    rows = _execute(
        "INSERT INTO dm_messages (user_id, direction, content, sent_at, message_id) "
        "VALUES (%s,%s,%s,%s,%s) "
        "ON CONFLICT (message_id) DO NOTHING "
        "RETURNING id",
        (user_id, direction, content, sent_at, message_id),
    )
    return bool(rows)


def get_dm_messages(user_id: str, limit: int = 300) -> list[dict]:
    return _execute(
        "SELECT direction, content, sent_at FROM dm_messages "
        "WHERE user_id = %s ORDER BY sent_at ASC LIMIT %s",
        (user_id, limit),
    )


# ─────────────────────────────────────────────────────────
# 初回発言ロール
# ─────────────────────────────────────────────────────────

def has_first_message(user_id: str) -> bool:
    rows = _execute("SELECT 1 FROM first_message_granted WHERE user_id=%s", (user_id,))
    return len(rows) > 0


def record_first_message(user_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    _execute(
        "INSERT INTO first_message_granted (user_id, granted_at) VALUES (%s,%s) ON CONFLICT DO NOTHING",
        (user_id, now),
    )


# ─────────────────────────────────────────────────────────
# reports（/report の受付〜承認/却下）
# user/server/bot 拡張分: target_type, creator_or_developer_id
# ─────────────────────────────────────────────────────────

def insert_pending_report(
    reporter_id: str,
    reporter_username: str,
    target_type: str,
    target_id: str,
    target_username: str,
    categories: list[str],
    note: Optional[str],
    creator_or_developer_id: Optional[str] = None,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    rows = _execute(
        "INSERT INTO reports "
        "(reporter_id, reporter_username, target_type, target_id, target_username, "
        " creator_or_developer_id, categories, note, status, created_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s) RETURNING id",
        (
            reporter_id,
            reporter_username,
            target_type,
            target_id,
            target_username,
            creator_or_developer_id,
            json.dumps(categories, ensure_ascii=False),
            note,
            now,
        ),
    )
    return rows[0]["id"]


def set_report_maintainer_message_id(report_id: int, message_id: str) -> None:
    _execute(
        "UPDATE reports SET maintainer_channel_message_id=%s WHERE id=%s",
        (message_id, report_id),
    )


def _decode_report_row(row: dict) -> dict:
    row["categories"] = json.loads(row["categories"])
    return row


def get_report(report_id: int) -> Optional[dict]:
    rows = _execute("SELECT * FROM reports WHERE id=%s", (report_id,))
    return _decode_report_row(rows[0]) if rows else None


def mark_report_merged(report_id: int) -> Optional[dict]:
    now = datetime.now(timezone.utc).isoformat()
    _execute(
        "UPDATE reports SET status='merged', decided_at=%s WHERE id=%s",
        (now, report_id),
    )
    return get_report(report_id)


def mark_report_rejected(report_id: int, reason: str) -> Optional[dict]:
    now = datetime.now(timezone.utc).isoformat()
    _execute(
        "UPDATE reports SET status='rejected', rejection_reason=%s, decided_at=%s WHERE id=%s",
        (reason, now, report_id),
    )
    return get_report(report_id)


def get_reports_by_reporter(reporter_id: str, limit: int = 50) -> list[dict]:
    rows = _execute(
        "SELECT * FROM reports WHERE reporter_id=%s ORDER BY created_at DESC LIMIT %s",
        (reporter_id, limit),
    )
    return [_decode_report_row(r) for r in rows]
