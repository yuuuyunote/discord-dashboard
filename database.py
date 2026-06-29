"""
database.py
Neon（PostgreSQL）対応版
テーブル追加：user_notes, retention_checks
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

        # インデックス
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
# invites 操作
# ─────────────────────────────────────────────────────────

def upsert_invite(code: str, creator_id: str, created_at: str) -> None:
    _execute(
        "INSERT INTO invites (code, creator_id, created_at) VALUES (%s,%s,%s) ON CONFLICT(code) DO NOTHING",
        (code, creator_id, created_at)
    )

def update_invite_memo(code: str, memo: str) -> None:
    _execute("UPDATE invites SET memo=%s WHERE code=%s", (memo, code))

def increment_invite_leave(code: str) -> None:
    _execute("UPDATE invites SET instant_leave=instant_leave+1 WHERE code=%s", (code,))

def update_invite_activity(code: str, messages: int, vc_sec: int) -> None:
    _execute(
        "UPDATE invites SET total_messages=total_messages+%s, total_vc_sec=total_vc_sec+%s WHERE code=%s",
        (messages, vc_sec, code)
    )

def get_all_invites() -> list[dict]:
    rows = _execute("""
        SELECT i.code, i.memo, i.instant_leave, i.total_messages,
               i.total_vc_sec, i.creator_id, i.created_at,
               COUNT(jl.id) AS total_joins
        FROM invites i
        LEFT JOIN join_logs jl ON jl.invite_code = i.code
        GROUP BY i.code, i.memo, i.instant_leave, i.total_messages,
                 i.total_vc_sec, i.creator_id, i.created_at
        ORDER BY i.total_messages DESC
    """)
    for r in rows:
        joins  = r["total_joins"] or 0
        leaves = r["instant_leave"] or 0
        r["instant_leave_rate"]  = round(leaves / joins * 100, 1) if joins > 0 else 0.0
        r["contribution_score"]  = (r["total_messages"] or 0) + round((r["total_vc_sec"] or 0) / 60)
    return rows

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

def update_user_initial_roles(user_id: str, roles: list[str]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    _execute(
        "UPDATE users SET initial_roles=%s, updated_at=%s WHERE user_id=%s",
        (json.dumps(roles, ensure_ascii=False), now, user_id)
    )

def get_user(user_id: str) -> Optional[dict]:
    rows = _execute("SELECT * FROM users WHERE user_id=%s", (user_id,))
    return rows[0] if rows else None

def search_users(query: str) -> list[dict]:
    return _execute(
        "SELECT * FROM users WHERE user_id LIKE %s OR username LIKE %s ORDER BY joined_at DESC LIMIT 30",
        (f"%{query}%", f"%{query}%")
    )


# ─────────────────────────────────────────────────────────
# join_logs 操作
# ─────────────────────────────────────────────────────────

def add_join_log(user_id: str, invite_code: Optional[str], joined_at: str) -> None:
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

def get_join_logs_by_date(days: int = 30) -> list[dict]:
    """直近N日の日別入室数を返す"""
    return _execute("""
        SELECT DATE(joined_at::timestamp) AS date, COUNT(*) AS count
        FROM join_logs
        WHERE joined_at >= (NOW() - INTERVAL '%s days')::TEXT
        GROUP BY DATE(joined_at::timestamp)
        ORDER BY date ASC
    """, (days,))

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

def add_activity_log(user_id: str, channel_id: str, sent_at: str) -> None:
    _execute(
        "INSERT INTO activity_logs (user_id, channel_id, sent_at) VALUES (%s,%s,%s)",
        (user_id, channel_id, sent_at)
    )

def get_channel_ranking(limit: int = 10) -> list[dict]:
    return _execute(
        "SELECT channel_id, COUNT(*) AS msg_count FROM activity_logs GROUP BY channel_id ORDER BY msg_count DESC LIMIT %s",
        (limit,)
    )

def get_user_message_count(user_id: str) -> int:
    rows = _execute("SELECT COUNT(*) AS cnt FROM activity_logs WHERE user_id=%s", (user_id,))
    return rows[0]["cnt"] if rows else 0

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

def get_user_vc_seconds(user_id: str) -> int:
    rows = _execute("""
        SELECT COALESCE(SUM(
            EXTRACT(EPOCH FROM (left_at::timestamp - joined_at::timestamp))
        ), 0)::INTEGER AS total_sec
        FROM voice_logs WHERE user_id=%s AND left_at IS NOT NULL
    """, (user_id,))
    return int(rows[0]["total_sec"] or 0) if rows else 0


# ─────────────────────────────────────────────────────────
# punishments 操作
# ─────────────────────────────────────────────────────────

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

def get_recent_punishments(limit: int = 20) -> list[dict]:
    return _execute("SELECT * FROM punishments ORDER BY executed_at DESC LIMIT %s", (limit,))


# ─────────────────────────────────────────────────────────
# moderator_stats 操作
# ─────────────────────────────────────────────────────────

def increment_mod_warn(moderator_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    _execute("""
        INSERT INTO moderator_stats (moderator_id, warn_count, audit_count, updated_at)
        VALUES (%s,1,0,%s)
        ON CONFLICT(moderator_id) DO UPDATE SET warn_count=moderator_stats.warn_count+1, updated_at=%s
    """, (moderator_id, now, now))

def increment_mod_audit(moderator_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    _execute("""
        INSERT INTO moderator_stats (moderator_id, warn_count, audit_count, updated_at)
        VALUES (%s,0,1,%s)
        ON CONFLICT(moderator_id) DO UPDATE SET audit_count=moderator_stats.audit_count+1, updated_at=%s
    """, (moderator_id, now, now))

def get_all_mod_stats() -> list[dict]:
    return _execute("SELECT * FROM moderator_stats ORDER BY audit_count DESC")


# ─────────────────────────────────────────────────────────
# user_notes 操作
# ─────────────────────────────────────────────────────────

def add_user_note(user_id: str, author_id: str, author_name: str, content: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    _execute("""
        INSERT INTO user_notes (user_id, author_id, author_name, content, created_at)
        VALUES (%s,%s,%s,%s,%s)
    """, (user_id, author_id, author_name, content, now))

def get_user_notes(user_id: str) -> list[dict]:
    return _execute(
        "SELECT * FROM user_notes WHERE user_id=%s ORDER BY created_at DESC",
        (user_id,)
    )

def delete_user_note(note_id: int) -> None:
    _execute("DELETE FROM user_notes WHERE id=%s", (note_id,))


# ─────────────────────────────────────────────────────────
# retention_checks 操作
# ─────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────
# ダッシュボード統計
# ─────────────────────────────────────────────────────────

def get_dashboard_stats() -> dict:
    total_users       = (_execute("SELECT COUNT(*) AS c FROM users") or [{"c":0}])[0]["c"] or 0
    active_users      = (_execute("SELECT COUNT(DISTINCT user_id) AS c FROM activity_logs WHERE sent_at >= (NOW() - INTERVAL '30 days')::TEXT") or [{"c":0}])[0]["c"] or 0
    total_messages    = (_execute("SELECT COUNT(*) AS c FROM activity_logs") or [{"c":0}])[0]["c"] or 0
    total_punishments = (_execute("SELECT COUNT(*) AS c FROM punishments") or [{"c":0}])[0]["c"] or 0
    active_rate       = round(active_users / total_users * 100, 1) if total_users else 0.0
    return {
        "total_users": total_users, "active_users": active_users,
        "active_rate": active_rate, "total_messages": total_messages,
        "total_punishments": total_punishments,
    }

def get_weekly_stats() -> dict:
    """定期レポート用週次統計"""
    joins = (_execute("""
        SELECT COUNT(*) AS c FROM join_logs
        WHERE joined_at >= (NOW() - INTERVAL '7 days')::TEXT
    """) or [{"c":0}])[0]["c"] or 0

    leaves = (_execute("""
        SELECT COUNT(*) AS c FROM join_logs
        WHERE left_at >= (NOW() - INTERVAL '7 days')::TEXT
        AND left_at IS NOT NULL
    """) or [{"c":0}])[0]["c"] or 0

    instant = (_execute("""
        SELECT COUNT(*) AS c FROM join_logs
        WHERE joined_at >= (NOW() - INTERVAL '7 days')::TEXT
        AND left_at IS NOT NULL
        AND (left_at::timestamp - joined_at::timestamp) < INTERVAL '24 hours'
    """) or [{"c":0}])[0]["c"] or 0

    messages = (_execute("""
        SELECT COUNT(*) AS c FROM activity_logs
        WHERE sent_at >= (NOW() - INTERVAL '7 days')::TEXT
    """) or [{"c":0}])[0]["c"] or 0

    punishments = (_execute("""
        SELECT COUNT(*) AS c FROM punishments
        WHERE executed_at >= (NOW() - INTERVAL '7 days')::TEXT
    """) or [{"c":0}])[0]["c"] or 0

    top_channels = _execute("""
        SELECT channel_id, COUNT(*) AS cnt
        FROM activity_logs
        WHERE sent_at >= (NOW() - INTERVAL '7 days')::TEXT
        GROUP BY channel_id
        ORDER BY cnt DESC
        LIMIT 3
    """)

    return {
        "joins": joins, "leaves": leaves, "instant_leaves": instant,
        "messages": messages, "punishments": punishments,
        "top_channels": top_channels,
    }


# ─────────────────────────────────────────────────────────
# CSV出力用
# ─────────────────────────────────────────────────────────

def get_all_users_for_csv() -> list[dict]:
    return _execute("SELECT * FROM users ORDER BY joined_at DESC")

def get_all_punishments_for_csv() -> list[dict]:
    return _execute("SELECT * FROM punishments ORDER BY executed_at DESC")

def get_all_invites_for_csv() -> list[dict]:
    return get_all_invites()


def delete_punishment(punishment_id: int) -> None:
    _execute("DELETE FROM punishments WHERE id=%s", (punishment_id,))


# ─────────────────────────────────────────────────────────
# 機能34・36・37・38・39 追加クエリ
# ─────────────────────────────────────────────────────────

def get_leave_reason_stats() -> dict:
    """退室理由の自動分類（機能34）"""
    # BAN
    ban = (_execute("SELECT COUNT(*) AS c FROM punishments WHERE punishment_type='BAN'") or [{"c":0}])[0]["c"] or 0
    # KICK
    kick = (_execute("SELECT COUNT(*) AS c FROM punishments WHERE punishment_type='KICK'") or [{"c":0}])[0]["c"] or 0
    # 即抜け（24時間以内退室）
    instant = (_execute("""
        SELECT COUNT(*) AS c FROM join_logs
        WHERE left_at IS NOT NULL
        AND (left_at::timestamp - joined_at::timestamp) < INTERVAL '24 hours'
    """) or [{"c":0}])[0]["c"] or 0
    # 長期在籍後の退室
    long_leave = (_execute("""
        SELECT COUNT(*) AS c FROM join_logs
        WHERE left_at IS NOT NULL
        AND (left_at::timestamp - joined_at::timestamp) >= INTERVAL '24 hours'
    """) or [{"c":0}])[0]["c"] or 0
    return {
        "ban": ban,
        "kick": kick,
        "instant": instant,
        "long_leave": long_leave,
    }


def get_activity_heatmap() -> list[dict]:
    """曜日×時間帯の発言数ヒートマップ（機能36）"""
    return _execute("""
        SELECT
            EXTRACT(DOW FROM sent_at::timestamp)::INTEGER  AS dow,
            EXTRACT(HOUR FROM (sent_at::timestamp AT TIME ZONE 'Asia/Tokyo'))::INTEGER AS hour,
            COUNT(*) AS count
        FROM activity_logs
        GROUP BY dow, hour
        ORDER BY dow, hour
    """)


def get_user_message_ranking(limit: int = 10) -> list[dict]:
    """発言数ランキング（ユーザー別）（機能37）"""
    rows = _execute("""
        SELECT a.user_id, u.username, COUNT(*) AS msg_count
        FROM activity_logs a
        LEFT JOIN users u ON u.user_id = a.user_id
        GROUP BY a.user_id, u.username
        ORDER BY msg_count DESC
        LIMIT %s
    """, (limit,))
    return rows


def get_new_user_analysis() -> dict:
    """直近30日の新規ユーザー分析（機能38）"""
    # 直近30日の新規入室者
    new_users = _execute("""
        SELECT user_id FROM join_logs
        WHERE joined_at >= (NOW() - INTERVAL '30 days')::TEXT
        AND left_at IS NULL
    """)
    new_user_ids = [r["user_id"] for r in new_users]
    total = len(new_user_ids)

    if total == 0:
        return {
            "total_new": 0,
            "avg_messages": 0,
            "instant_leave_rate": 0,
            "still_member_rate": 0,
        }

    # 平均発言数
    avg_msg = (_execute("""
        SELECT ROUND(AVG(cnt),1) AS avg FROM (
            SELECT user_id, COUNT(*) AS cnt
            FROM activity_logs
            WHERE user_id = ANY(%s::text[])
            GROUP BY user_id
        ) t
    """, (new_user_ids,)) or [{"avg": 0}])[0]["avg"] or 0

    # 即抜け率
    instant = (_execute("""
        SELECT COUNT(*) AS c FROM join_logs
        WHERE joined_at >= (NOW() - INTERVAL '30 days')::TEXT
        AND left_at IS NOT NULL
        AND (left_at::timestamp - joined_at::timestamp) < INTERVAL '24 hours'
    """) or [{"c":0}])[0]["c"] or 0

    # 在籍中の割合
    still = (_execute("""
        SELECT COUNT(*) AS c FROM join_logs
        WHERE joined_at >= (NOW() - INTERVAL '30 days')::TEXT
        AND left_at IS NULL
    """) or [{"c":0}])[0]["c"] or 0

    all_joins = (_execute("""
        SELECT COUNT(*) AS c FROM join_logs
        WHERE joined_at >= (NOW() - INTERVAL '30 days')::TEXT
    """) or [{"c":0}])[0]["c"] or 1

    return {
        "total_new": total,
        "avg_messages": float(avg_msg),
        "instant_leave_rate": round(instant / all_joins * 100, 1),
        "still_member_rate": round(still / all_joins * 100, 1),
    }


def get_invite_creator_ranking() -> list[dict]:
    """招待リンク作成者別ランキング（機能39）"""
    return _execute("""
        SELECT
            i.creator_id,
            COUNT(DISTINCT jl.user_id)                    AS total_joins,
            SUM(i.total_messages)                         AS total_messages,
            ROUND(AVG(i.instant_leave * 100.0 /
                NULLIF((SELECT COUNT(*) FROM join_logs j2
                        WHERE j2.invite_code = i.code), 0)), 1) AS avg_leave_rate
        FROM invites i
        LEFT JOIN join_logs jl ON jl.invite_code = i.code
        GROUP BY i.creator_id
        ORDER BY total_joins DESC
        LIMIT 10
    """)


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
# 機能36：ヒートマップ用データ
# ─────────────────────────────────────────────────────────

def get_activity_heatmap() -> list[dict]:
    """曜日×時間帯の発言数ヒートマップデータ（JST換算）"""
    return _execute("""
        SELECT
            EXTRACT(DOW FROM (sent_at::timestamp AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Tokyo'))::INTEGER AS dow,
            EXTRACT(HOUR FROM (sent_at::timestamp AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Tokyo'))::INTEGER AS hour,
            COUNT(*) AS cnt
        FROM activity_logs
        GROUP BY dow, hour
        ORDER BY dow, hour
    """)


# ─────────────────────────────────────────────────────────
# 機能37：発言数ランキング
# ─────────────────────────────────────────────────────────

def get_user_message_ranking(period_days: int = 30, limit: int = 10) -> list[dict]:
    """期間内の発言数ランキング"""
    if period_days == 0:
        return _execute("""
            SELECT a.user_id, u.username, COUNT(*) AS msg_count
            FROM activity_logs a
            LEFT JOIN users u ON u.user_id = a.user_id
            GROUP BY a.user_id, u.username
            ORDER BY msg_count DESC
            LIMIT %s
        """, (limit,))
    return _execute("""
        SELECT a.user_id, u.username, COUNT(*) AS msg_count
        FROM activity_logs a
        LEFT JOIN users u ON u.user_id = a.user_id
        WHERE a.sent_at >= (NOW() - INTERVAL '1 day' * %s)::TEXT
        GROUP BY a.user_id, u.username
        ORDER BY msg_count DESC
        LIMIT %s
    """, (period_days, limit))


# ─────────────────────────────────────────────────────────
# 機能38：新規ユーザー分析パネル
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
# 機能39：招待リンク作成者別ランキング
# ─────────────────────────────────────────────────────────

def get_invite_creator_ranking() -> list[dict]:
    """招待リンク作成者別の入室数・貢献スコアランキング"""
    return _execute("""
        SELECT
            i.creator_id,
            COUNT(DISTINCT i.code)      AS invite_count,
            COUNT(DISTINCT jl.id)       AS total_joins,
            SUM(i.total_messages)       AS total_messages,
            SUM(i.total_vc_sec)         AS total_vc_sec,
            SUM(i.instant_leave)        AS total_instant_leave,
            SUM(i.total_messages) + ROUND(SUM(i.total_vc_sec) / 60.0) AS contribution_score
        FROM invites i
        LEFT JOIN join_logs jl ON jl.invite_code = i.code
        GROUP BY i.creator_id
        ORDER BY contribution_score DESC
        LIMIT 20
    """)


# ─────────────────────────────────────────────────────────
# 機能34：退室理由分類
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
# 機能46：処罰履歴フィルター検索
# ─────────────────────────────────────────────────────────

def get_punishments_filtered(
    punishment_type: str = "",
    executor: str = "",
    days: int = 0,
    limit: int = 50
) -> list[dict]:
    """処罰履歴をフィルタリングして返す"""
    conditions = []
    args = []

    if punishment_type:
        conditions.append("punishment_type = %s")
        args.append(punishment_type)
    if executor:
        conditions.append("executor ILIKE %s")
        args.append(f"%{executor}%")
    if days > 0:
        conditions.append("executed_at >= (NOW() - INTERVAL '1 day' * %s)::TEXT")
        args.append(days)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    args.append(limit)

    return _execute(f"""
        SELECT * FROM punishments
        {where}
        ORDER BY executed_at DESC
        LIMIT %s
    """, tuple(args))
