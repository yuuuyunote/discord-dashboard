"""
database.py
Neon（PostgreSQL）対応版
テーブル追加：user_notes, retention_checks
reports拡張：target_type / creator_or_developer_id（user/server/bot対応）
"""

import os
import json
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras


def get_conn():
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
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


def _execute(sql: str, args: tuple = ()) -> list[dict]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            conn.commit()
            try:
                rows = cur.fetchall()
                return [dict(r) for r in rows]
            except psycopg2.ProgrammingError:
                return []
    finally:
        conn.close()


def _execute_many(statements: list[tuple]) -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for sql, args in statements:
                cur.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────
# テーブル初期化
# ─────────────────────────────────────────────────────────

def init_db() -> None:
    stmts = [
        # 既存テーブル
        ("CREATE TABLE IF NOT EXISTS invites (code TEXT PRIMARY KEY, memo TEXT DEFAULT '', instant_leave INTEGER DEFAULT 0, total_messages INTEGER DEFAULT 0, total_vc_sec INTEGER DEFAULT 0, creator_id TEXT NOT NULL, created_at TEXT NOT NULL)", ()),
        ("CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY, username TEXT NOT NULL, account_created TEXT NOT NULL, joined_at TEXT, initial_roles TEXT DEFAULT '[]', updated_at TEXT NOT NULL)", ()),
        ("CREATE TABLE IF NOT EXISTS join_logs (id SERIAL PRIMARY KEY, user_id TEXT NOT NULL, invite_code TEXT, joined_at TEXT NOT NULL, left_at TEXT)", ()),
        ("CREATE TABLE IF NOT EXISTS activity_logs (id SERIAL PRIMARY KEY, user_id TEXT NOT NULL, channel_id TEXT NOT NULL, sent_at TEXT NOT NULL)", ()),
        ("CREATE TABLE IF NOT EXISTS voice_logs (id SERIAL PRIMARY KEY, user_id TEXT NOT NULL, joined_at TEXT NOT NULL, left_at TEXT)", ()),
        ("CREATE TABLE IF NOT EXISTS punishments (id SERIAL PRIMARY KEY, user_id TEXT NOT NULL, target_name TEXT NOT NULL, punishment_type TEXT NOT NULL, executor TEXT NOT NULL, reason TEXT DEFAULT '', executed_at TEXT NOT NULL)", ()),
        ("CREATE TABLE IF NOT EXISTS moderator_stats (moderator_id TEXT PRIMARY KEY, warn_count INTEGER DEFAULT 0, audit_count INTEGER DEFAULT 0, updated_at TEXT NOT NULL)", ()),

        # 新規：ユーザーノート（複数追記対応）
        ("""CREATE TABLE IF NOT EXISTS user_notes (
            id          SERIAL PRIMARY KEY,
            user_id     TEXT NOT NULL,
            author_id   TEXT NOT NULL,
            author_name TEXT NOT NULL,
            content     TEXT NOT NULL,
            created_at  TEXT NOT NULL
        )""", ()),

        # 新規：定着率チェック
        ("""CREATE TABLE IF NOT EXISTS retention_checks (
            id          SERIAL PRIMARY KEY,
            user_id     TEXT NOT NULL,
            invite_code TEXT,
            joined_at   TEXT NOT NULL,
            check_7d    BOOLEAN DEFAULT NULL,
            check_30d   BOOLEAN DEFAULT NULL,
            checked_at  TEXT
        )""", ()),

        # 新規：初回発言ロール付与済みフラグ
        ("""CREATE TABLE IF NOT EXISTS first_message_granted (
            user_id     TEXT PRIMARY KEY,
            granted_at  TEXT NOT NULL
        )""", ()),

        # 新規：Bot再起動をまたいで状態を保持するための汎用キーバリュー
        # （監査ログの最終チェック時刻など）
        ("""CREATE TABLE IF NOT EXISTS bot_state (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""", ()),

        # 新規：フォーラムスレッド キープアライブの対象スレッド一覧（複数登録可）
        ("""CREATE TABLE IF NOT EXISTS keepalive_threads (
            thread_id    TEXT PRIMARY KEY,
            label        TEXT NOT NULL DEFAULT '',
            added_at     TEXT NOT NULL,
            last_sent_at TEXT
        )""", ()),
        # 既存テーブルに対するマイグレーション（初回追加時はNO-OP）
        ("ALTER TABLE keepalive_threads ADD COLUMN IF NOT EXISTS last_sent_at TEXT", ()),

        # 新規：個別チャット形式でのDM対応管理
        # （dm_repliesは片方向専用だったため、双方向のスレッド管理に置き換え）
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

        # 新規：通報の受付〜承認/却下（/report）
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
        ("CREATE INDEX IF NOT EXISTS idx_join_user    ON join_logs(user_id)", ()),
        ("CREATE INDEX IF NOT EXISTS idx_join_invite  ON join_logs(invite_code)", ()),
        ("CREATE INDEX IF NOT EXISTS idx_act_user     ON activity_logs(user_id)", ()),
        ("CREATE INDEX IF NOT EXISTS idx_act_ch       ON activity_logs(channel_id)", ()),
        ("CREATE INDEX IF NOT EXISTS idx_voice_user   ON voice_logs(user_id)", ()),
        ("CREATE INDEX IF NOT EXISTS idx_pun_user     ON punishments(user_id)", ()),
        ("CREATE INDEX IF NOT EXISTS idx_notes_user   ON user_notes(user_id)", ()),
        ("CREATE INDEX IF NOT EXISTS idx_ret_user     ON retention_checks(user_id)", ()),
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
# invites 操作
# ─────────────────────────────────────────────────────────

def upsert_invite(code: str, creator_id: str, created_at: str) -> None:
    _execute(
        "INSERT INTO invites (code, creator_id, created_at) VALUES (%s,%s,%s) ON CONFLICT(code) DO NOTHING",
        (code, creator_id, created_at)
    )

def increment_invite_leave(code: str) -> None:
    _execute("UPDATE invites SET instant_leave=instant_leave+1 WHERE code=%s", (code,))

def update_invite_activity(code: str, messages: int, vc_sec: int) -> None:
    _execute(
        "UPDATE invites SET total_messages=total_messages+%s, total_vc_sec=total_vc_sec+%s WHERE code=%s",
        (messages, vc_sec, code)
    )

def get_invite(code: str) -> Optional[dict]:
    rows = _execute("SELECT * FROM invites WHERE code=%s", (code,))
    return rows[0] if rows else None


# ─────────────────────────────────────────────────────────
# users 操作
# ─────────────────────────────────────────────────────────

def upsert_user(user_id: str, username: str, account_created: str,
                joined_at: Optional[str] = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    _execute("""
        INSERT INTO users (user_id, username, account_created, joined_at, updated_at)
        VALUES (%s,%s,%s,%s,%s)
        ON CONFLICT(user_id) DO UPDATE SET username=%s, updated_at=%s
    """, (user_id, username, account_created, joined_at, now, username, now))

def insert_new_users_bulk(rows: list[tuple[str, str, str, Optional[str]]]) -> int:
    """
    (user_id, username, account_created, joined_at) のリストを1回のSQLで一括INSERTする。
    既に存在するuser_idはON CONFLICT DO NOTHINGでそのままスキップする（更新はしない）。

    起動時の既存メンバー一括インポート用。1人ずつupsert_user()を呼ぶと
    669人なら669回psycopg2.connect()することになり、その間ずっとイベントループを
    ブロックしてDiscordのハートビートが止まる（実際にこれが原因でbotがオフラインに
    なっていた）。1回のバルクINSERTに置き換えることで接続を1回に減らす。
    """
    if not rows:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    values = [(user_id, username, account_created, joined_at, now) for user_id, username, account_created, joined_at in rows]
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO users (user_id, username, account_created, joined_at, updated_at)
                VALUES %s
                ON CONFLICT (user_id) DO NOTHING
                """,
                values,
            )
            inserted = cur.rowcount
        conn.commit()
        return inserted
    finally:
        conn.close()

def get_all_user_ids() -> set[str]:
    rows = _execute("SELECT user_id FROM users")
    return {r["user_id"] for r in rows}

def update_user_initial_roles(user_id: str, roles: list[str]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    _execute(
        "UPDATE users SET initial_roles=%s, updated_at=%s WHERE user_id=%s",
        (json.dumps(roles, ensure_ascii=False), now, user_id)
    )

def get_user(user_id: str) -> Optional[dict]:
    rows = _execute("SELECT * FROM users WHERE user_id=%s", (user_id,))
    return rows[0] if rows else None

def add_join_log(user_id: str, invite_code: Optional[str], joined_at: str) -> None:
    # 重複防止：同一ユーザーの未退出ログが直近5分以内にあればスキップ
    # （discord.pyのon_member_joinが再接続等で複数回発火するケースに対応）
    existing = _execute("""
        SELECT id FROM join_logs
        WHERE user_id=%s
        AND left_at IS NULL
        AND joined_at >= (NOW() - INTERVAL '5 minutes')::TEXT
        LIMIT 1
    """, (user_id,))
    if existing:
        return

    _execute(
        "INSERT INTO join_logs (user_id, invite_code, joined_at) VALUES (%s,%s,%s)",
        (user_id, invite_code, joined_at)
    )

def record_leave(user_id: str, left_at: str) -> Optional[dict]:
    rows = _execute(
        "SELECT id, invite_code, joined_at FROM join_logs WHERE user_id=%s AND left_at IS NULL ORDER BY id DESC LIMIT 1",
        (user_id,)
    )
    if not rows:
        return None
    _execute("UPDATE join_logs SET left_at=%s WHERE id=%s", (left_at, rows[0]["id"]))
    return rows[0]

def get_join_log_by_user(user_id: str) -> Optional[dict]:
    rows = _execute(
        "SELECT * FROM join_logs WHERE user_id=%s ORDER BY id DESC LIMIT 1", (user_id,)
    )
    return rows[0] if rows else None

def get_join_logs_by_invite(invite_code: str) -> list[dict]:
    """招待コード別のユーザー一覧"""
    return _execute("""
        SELECT jl.*, u.username
        FROM join_logs jl
        LEFT JOIN users u ON u.user_id = jl.user_id
        WHERE jl.invite_code = %s
        ORDER BY jl.joined_at DESC
    """, (invite_code,))


# ─────────────────────────────────────────────────────────
# activity_logs 操作
# ─────────────────────────────────────────────────────────

def add_activity_logs_bulk(records: list[tuple[str, str, str]]) -> None:
    """
    records: [(user_id, channel_id, sent_at), ...]
    メッセージ1件ごとにDB書き込みするとNeonのコンピュートが常時起きた状態に
    なりCU-hoursを消費するため、一定間隔でまとめて書き込むためのバルク版。
    """
    if not records:
        return
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO activity_logs (user_id, channel_id, sent_at) VALUES %s",
                records,
            )
            conn.commit()
    finally:
        conn.close()

def bulk_increment_invite_messages(counts: dict[str, int]) -> None:
    """counts: {invite_code: このバッチ内での発言数}"""
    if not counts:
        return
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for code, cnt in counts.items():
                cur.execute(
                    "UPDATE invites SET total_messages = total_messages + %s WHERE code = %s",
                    (cnt, code),
                )
            conn.commit()
    finally:
        conn.close()

def has_first_message(user_id: str) -> bool:
    rows = _execute("SELECT 1 FROM first_message_granted WHERE user_id=%s", (user_id,))
    return len(rows) > 0

def record_first_message(user_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    _execute(
        "INSERT INTO first_message_granted (user_id, granted_at) VALUES (%s,%s) ON CONFLICT DO NOTHING",
        (user_id, now)
    )


# ─────────────────────────────────────────────────────────
# voice_logs 操作
# ─────────────────────────────────────────────────────────

def add_voice_join(user_id: str, joined_at: str) -> None:
    _execute("INSERT INTO voice_logs (user_id, joined_at) VALUES (%s,%s)", (user_id, joined_at))

def record_voice_leave(user_id: str, left_at: str) -> Optional[int]:
    rows = _execute(
        "SELECT id, joined_at FROM voice_logs WHERE user_id=%s AND left_at IS NULL ORDER BY id DESC LIMIT 1",
        (user_id,)
    )
    if not rows:
        return None
    _execute("UPDATE voice_logs SET left_at=%s WHERE id=%s", (left_at, rows[0]["id"]))
    joined = datetime.fromisoformat(rows[0]["joined_at"])
    left   = datetime.fromisoformat(left_at)
    return max(0, int((left - joined).total_seconds()))

def add_punishment(user_id: str, target_name: str, punishment_type: str,
                   executor: str, reason: str, executed_at: str) -> None:
    _execute(
        "INSERT INTO punishments (user_id, target_name, punishment_type, executor, reason, executed_at) VALUES (%s,%s,%s,%s,%s,%s)",
        (user_id, target_name, punishment_type, executor, reason, executed_at)
    )

def get_punishments_by_user(user_id: str) -> list[dict]:
    return _execute(
        "SELECT * FROM punishments WHERE user_id=%s ORDER BY executed_at DESC", (user_id,)
    )

def increment_mod_audit(moderator_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    _execute("""
        INSERT INTO moderator_stats (moderator_id, warn_count, audit_count, updated_at)
        VALUES (%s,0,1,%s)
        ON CONFLICT(moderator_id) DO UPDATE SET audit_count=moderator_stats.audit_count+1, updated_at=%s
    """, (moderator_id, now, now))

def add_retention_check(user_id: str, invite_code: Optional[str], joined_at: str) -> None:
    _execute("""
        INSERT INTO retention_checks (user_id, invite_code, joined_at)
        VALUES (%s,%s,%s)
        ON CONFLICT DO NOTHING
    """, (user_id, invite_code, joined_at))

def get_pending_retention_checks() -> list[dict]:
    """7日・30日チェックが未完了のレコードを返す"""
    return _execute("""
        SELECT * FROM retention_checks
        WHERE check_7d IS NULL OR check_30d IS NULL
        ORDER BY joined_at ASC
    """)

def update_retention_check(user_id: str, check_7d: Optional[bool], check_30d: Optional[bool]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    if check_7d is not None:
        _execute(
            "UPDATE retention_checks SET check_7d=%s, checked_at=%s WHERE user_id=%s",
            (check_7d, now, user_id)
        )
    if check_30d is not None:
        _execute(
            "UPDATE retention_checks SET check_30d=%s, checked_at=%s WHERE user_id=%s",
            (check_30d, now, user_id)
        )

def get_retention_stats_by_invite() -> list[dict]:
    """招待コード別の定着率を返す"""
    return _execute("""
        SELECT
            invite_code,
            COUNT(*) AS total,
            COUNT(CASE WHEN check_7d = TRUE THEN 1 END) AS retained_7d,
            COUNT(CASE WHEN check_30d = TRUE THEN 1 END) AS retained_30d,
            ROUND(COUNT(CASE WHEN check_7d = TRUE THEN 1 END)::NUMERIC / NULLIF(COUNT(*),0) * 100, 1) AS rate_7d,
            ROUND(COUNT(CASE WHEN check_30d = TRUE THEN 1 END)::NUMERIC / NULLIF(COUNT(*),0) * 100, 1) AS rate_30d
        FROM retention_checks
        WHERE check_7d IS NOT NULL
        GROUP BY invite_code
        ORDER BY rate_7d DESC
    """)


def delete_punishment(punishment_id: int) -> None:
    _execute("DELETE FROM punishments WHERE id=%s", (punishment_id,))


# ─────────────────────────────────────────────────────────
# 処罰履歴フィルター検索
# ─────────────────────────────────────────────────────────

def get_punishment_filtered(
    punishment_type: str = "",
    executor: str = "",
    days: int = 0,
    limit: int = 50
) -> list[dict]:
    """処罰履歴フィルター検索（機能46）"""
    conditions = []
    args = []

    if punishment_type:
        conditions.append("punishment_type = %s")
        args.append(punishment_type)
    if executor:
        conditions.append("executor ILIKE %s")
        args.append(f"%{executor}%")
    if days > 0:
        conditions.append(f"executed_at >= (NOW() - INTERVAL '{days} days')::TEXT")

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    args.append(limit)

    return _execute(f"""
        SELECT * FROM punishments
        {where}
        ORDER BY executed_at DESC
        LIMIT %s
    """, tuple(args))


# ─────────────────────────────────────────────────────────
# 新規ユーザー分析
# ─────────────────────────────────────────────────────────

def get_new_user_stats(days: int = 30) -> dict:
    """直近N日の新規ユーザー分析"""
    # 新規入室数
    new_joins = (_execute("""
        SELECT COUNT(*) AS c FROM join_logs
        WHERE joined_at >= (NOW() - INTERVAL '1 day' * %s)::TEXT
    """, (days,)) or [{"c": 0}])[0]["c"] or 0

    # 即抜け数
    instant = (_execute("""
        SELECT COUNT(*) AS c FROM join_logs
        WHERE joined_at >= (NOW() - INTERVAL '1 day' * %s)::TEXT
        AND left_at IS NOT NULL
        AND (left_at::timestamp - joined_at::timestamp) < INTERVAL '24 hours'
    """, (days,)) or [{"c": 0}])[0]["c"] or 0

    # 発言したユーザー数（新規のみ）
    active_new = (_execute("""
        SELECT COUNT(DISTINCT a.user_id) AS c
        FROM activity_logs a
        INNER JOIN join_logs j ON j.user_id = a.user_id
        WHERE j.joined_at >= (NOW() - INTERVAL '1 day' * %s)::TEXT
        AND a.sent_at >= (NOW() - INTERVAL '1 day' * %s)::TEXT
    """, (days, days)) or [{"c": 0}])[0]["c"] or 0

    # 7日定着率
    retained_7d = (_execute("""
        SELECT COUNT(*) AS c FROM retention_checks
        WHERE joined_at >= (NOW() - INTERVAL '1 day' * %s)::TEXT
        AND check_7d = TRUE
    """, (days,)) or [{"c": 0}])[0]["c"] or 0

    instant_rate  = round(instant / new_joins * 100, 1) if new_joins else 0.0
    active_rate   = round(active_new / new_joins * 100, 1) if new_joins else 0.0
    retained_rate = round(retained_7d / new_joins * 100, 1) if new_joins else 0.0

    return {
        "new_joins":      new_joins,
        "instant":        instant,
        "instant_rate":   instant_rate,
        "active_new":     active_new,
        "active_rate":    active_rate,
        "retained_7d":    retained_7d,
        "retained_rate":  retained_rate,
    }


# ─────────────────────────────────────────────────────────
# 退室理由の分類統計
# ─────────────────────────────────────────────────────────

def get_leave_reason_stats() -> dict:
    """退室理由の分類統計"""
    # BAN退室
    ban_count = (_execute("""
        SELECT COUNT(DISTINCT p.user_id) AS c FROM punishments p
        WHERE p.punishment_type = 'BAN'
    """) or [{"c": 0}])[0]["c"] or 0

    # KICK退室
    kick_count = (_execute("""
        SELECT COUNT(DISTINCT p.user_id) AS c FROM punishments p
        WHERE p.punishment_type = 'KICK'
    """) or [{"c": 0}])[0]["c"] or 0

    # 即抜け（24時間以内）
    instant_count = (_execute("""
        SELECT COUNT(*) AS c FROM join_logs
        WHERE left_at IS NOT NULL
        AND (left_at::timestamp - joined_at::timestamp) < INTERVAL '24 hours'
    """) or [{"c": 0}])[0]["c"] or 0

    # 長期在籍後の退室（7日以上）
    long_count = (_execute("""
        SELECT COUNT(*) AS c FROM join_logs
        WHERE left_at IS NOT NULL
        AND (left_at::timestamp - joined_at::timestamp) >= INTERVAL '7 days'
    """) or [{"c": 0}])[0]["c"] or 0

    # 短期（24時間〜7日）
    short_count = (_execute("""
        SELECT COUNT(*) AS c FROM join_logs
        WHERE left_at IS NOT NULL
        AND (left_at::timestamp - joined_at::timestamp) >= INTERVAL '24 hours'
        AND (left_at::timestamp - joined_at::timestamp) < INTERVAL '7 days'
    """) or [{"c": 0}])[0]["c"] or 0

    return {
        "ban":     ban_count,
        "kick":    kick_count,
        "instant": instant_count,
        "short":   short_count,
        "long":    long_count,
    }


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
