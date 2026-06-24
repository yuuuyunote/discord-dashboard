"""
database.py
Turso（libsql）クラウドDB対応版
全テーブル定義・データ操作関数
"""

import os
import json
from datetime import datetime, timezone
from typing import Optional

import libsql_client

TURSO_URL   = os.getenv("TURSO_URL", "")
TURSO_TOKEN = os.getenv("TURSO_TOKEN", "")


def get_client() -> libsql_client.Client:
    return libsql_client.create_client_sync(
        url=TURSO_URL,
        auth_token=TURSO_TOKEN,
    )


def _execute(sql: str, args: tuple = ()) -> libsql_client.ResultSet:
    with get_client() as client:
        return client.execute(libsql_client.Statement(sql, list(args)))


def _execute_many(statements: list) -> None:
    with get_client() as client:
        client.batch(statements)


def _rows_as_dicts(result: libsql_client.ResultSet) -> list[dict]:
    cols = result.columns
    return [dict(zip(cols, row)) for row in result.rows]


# ─── テーブル初期化 ───────────────────────────────────────

def init_db() -> None:
    S = libsql_client.Statement
    _execute_many([
        S("""CREATE TABLE IF NOT EXISTS invites (
            code TEXT PRIMARY KEY, memo TEXT DEFAULT '',
            instant_leave INTEGER DEFAULT 0, total_messages INTEGER DEFAULT 0,
            total_vc_sec INTEGER DEFAULT 0, creator_id TEXT NOT NULL,
            created_at TEXT NOT NULL)"""),
        S("""CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY, username TEXT NOT NULL,
            account_created TEXT NOT NULL, joined_at TEXT,
            initial_roles TEXT DEFAULT '[]', updated_at TEXT NOT NULL)"""),
        S("""CREATE TABLE IF NOT EXISTS join_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
            invite_code TEXT, joined_at TEXT NOT NULL, left_at TEXT)"""),
        S("""CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
            channel_id TEXT NOT NULL, sent_at TEXT NOT NULL)"""),
        S("""CREATE TABLE IF NOT EXISTS voice_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
            joined_at TEXT NOT NULL, left_at TEXT)"""),
        S("""CREATE TABLE IF NOT EXISTS punishments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
            target_name TEXT NOT NULL, punishment_type TEXT NOT NULL,
            executor TEXT NOT NULL, reason TEXT DEFAULT '',
            executed_at TEXT NOT NULL)"""),
        S("""CREATE TABLE IF NOT EXISTS moderator_stats (
            moderator_id TEXT PRIMARY KEY, warn_count INTEGER DEFAULT 0,
            audit_count INTEGER DEFAULT 0, updated_at TEXT NOT NULL)"""),
        S("CREATE INDEX IF NOT EXISTS idx_join_user   ON join_logs(user_id)"),
        S("CREATE INDEX IF NOT EXISTS idx_join_invite ON join_logs(invite_code)"),
        S("CREATE INDEX IF NOT EXISTS idx_act_user    ON activity_logs(user_id)"),
        S("CREATE INDEX IF NOT EXISTS idx_act_ch      ON activity_logs(channel_id)"),
        S("CREATE INDEX IF NOT EXISTS idx_voice_user  ON voice_logs(user_id)"),
        S("CREATE INDEX IF NOT EXISTS idx_pun_user    ON punishments(user_id)"),
    ])


# ─── invites ──────────────────────────────────────────────

def upsert_invite(code: str, creator_id: str, created_at: str) -> None:
    _execute(
        "INSERT INTO invites (code, creator_id, created_at) VALUES (?,?,?) ON CONFLICT(code) DO NOTHING",
        (code, creator_id, created_at)
    )

def update_invite_memo(code: str, memo: str) -> None:
    _execute("UPDATE invites SET memo=? WHERE code=?", (memo, code))

def increment_invite_leave(code: str) -> None:
    _execute("UPDATE invites SET instant_leave=instant_leave+1 WHERE code=?", (code,))

def update_invite_activity(code: str, messages: int, vc_sec: int) -> None:
    _execute(
        "UPDATE invites SET total_messages=total_messages+?, total_vc_sec=total_vc_sec+? WHERE code=?",
        (messages, vc_sec, code)
    )

def get_all_invites() -> list[dict]:
    result = _execute("""
        SELECT i.code, i.memo, i.instant_leave, i.total_messages,
               i.total_vc_sec, i.creator_id, i.created_at,
               COUNT(jl.id) AS total_joins
        FROM invites i
        LEFT JOIN join_logs jl ON jl.invite_code = i.code
        GROUP BY i.code ORDER BY i.total_messages DESC
    """)
    rows = _rows_as_dicts(result)
    for r in rows:
        joins  = r["total_joins"] or 0
        leaves = r["instant_leave"] or 0
        r["instant_leave_rate"]  = round(leaves / joins * 100, 1) if joins > 0 else 0.0
        r["contribution_score"]  = (r["total_messages"] or 0) + round((r["total_vc_sec"] or 0) / 60)
    return rows

def get_invite(code: str) -> Optional[dict]:
    rows = _rows_as_dicts(_execute("SELECT * FROM invites WHERE code=?", (code,)))
    return rows[0] if rows else None


# ─── users ────────────────────────────────────────────────

def upsert_user(user_id: str, username: str, account_created: str,
                joined_at: Optional[str] = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    _execute("""
        INSERT INTO users (user_id, username, account_created, joined_at, updated_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET username=?, updated_at=?
    """, (user_id, username, account_created, joined_at, now, username, now))

def update_user_initial_roles(user_id: str, roles: list[str]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    _execute("UPDATE users SET initial_roles=?, updated_at=? WHERE user_id=?",
             (json.dumps(roles, ensure_ascii=False), now, user_id))

def get_user(user_id: str) -> Optional[dict]:
    rows = _rows_as_dicts(_execute("SELECT * FROM users WHERE user_id=?", (user_id,)))
    return rows[0] if rows else None

def search_users(query: str) -> list[dict]:
    rows = _rows_as_dicts(_execute(
        "SELECT * FROM users WHERE user_id LIKE ? OR username LIKE ? ORDER BY joined_at DESC LIMIT 30",
        (f"%{query}%", f"%{query}%")
    ))
    return rows


# ─── join_logs ────────────────────────────────────────────

def add_join_log(user_id: str, invite_code: Optional[str], joined_at: str) -> None:
    _execute("INSERT INTO join_logs (user_id, invite_code, joined_at) VALUES (?,?,?)",
             (user_id, invite_code, joined_at))

def record_leave(user_id: str, left_at: str) -> Optional[dict]:
    rows = _rows_as_dicts(_execute(
        "SELECT id, invite_code, joined_at FROM join_logs WHERE user_id=? AND left_at IS NULL ORDER BY id DESC LIMIT 1",
        (user_id,)
    ))
    if not rows:
        return None
    _execute("UPDATE join_logs SET left_at=? WHERE id=?", (left_at, rows[0]["id"]))
    return rows[0]

def get_join_log_by_user(user_id: str) -> Optional[dict]:
    rows = _rows_as_dicts(_execute(
        "SELECT * FROM join_logs WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)
    ))
    return rows[0] if rows else None


# ─── activity_logs ────────────────────────────────────────

def add_activity_log(user_id: str, channel_id: str, sent_at: str) -> None:
    _execute("INSERT INTO activity_logs (user_id, channel_id, sent_at) VALUES (?,?,?)",
             (user_id, channel_id, sent_at))

def get_channel_ranking(limit: int = 10) -> list[dict]:
    return _rows_as_dicts(_execute(
        "SELECT channel_id, COUNT(*) AS msg_count FROM activity_logs GROUP BY channel_id ORDER BY msg_count DESC LIMIT ?",
        (limit,)
    ))

def get_user_message_count(user_id: str) -> int:
    rows = _rows_as_dicts(_execute(
        "SELECT COUNT(*) AS cnt FROM activity_logs WHERE user_id=?", (user_id,)
    ))
    return rows[0]["cnt"] if rows else 0


# ─── voice_logs ───────────────────────────────────────────

def add_voice_join(user_id: str, joined_at: str) -> None:
    _execute("INSERT INTO voice_logs (user_id, joined_at) VALUES (?,?)", (user_id, joined_at))

def record_voice_leave(user_id: str, left_at: str) -> Optional[int]:
    rows = _rows_as_dicts(_execute(
        "SELECT id, joined_at FROM voice_logs WHERE user_id=? AND left_at IS NULL ORDER BY id DESC LIMIT 1",
        (user_id,)
    ))
    if not rows:
        return None
    _execute("UPDATE voice_logs SET left_at=? WHERE id=?", (left_at, rows[0]["id"]))
    joined = datetime.fromisoformat(rows[0]["joined_at"])
    left   = datetime.fromisoformat(left_at)
    return max(0, int((left - joined).total_seconds()))

def get_user_vc_seconds(user_id: str) -> int:
    rows = _rows_as_dicts(_execute("""
        SELECT SUM(CAST((julianday(left_at)-julianday(joined_at))*86400 AS INTEGER)) AS total_sec
        FROM voice_logs WHERE user_id=? AND left_at IS NOT NULL
    """, (user_id,)))
    return int(rows[0]["total_sec"] or 0) if rows else 0


# ─── punishments ──────────────────────────────────────────

def add_punishment(user_id: str, target_name: str, punishment_type: str,
                   executor: str, reason: str, executed_at: str) -> None:
    _execute("""
        INSERT INTO punishments (user_id, target_name, punishment_type, executor, reason, executed_at)
        VALUES (?,?,?,?,?,?)
    """, (user_id, target_name, punishment_type, executor, reason, executed_at))

def get_punishments_by_user(user_id: str) -> list[dict]:
    return _rows_as_dicts(_execute(
        "SELECT * FROM punishments WHERE user_id=? ORDER BY executed_at DESC", (user_id,)
    ))

def get_recent_punishments(limit: int = 20) -> list[dict]:
    return _rows_as_dicts(_execute(
        "SELECT * FROM punishments ORDER BY executed_at DESC LIMIT ?", (limit,)
    ))


# ─── moderator_stats ──────────────────────────────────────

def increment_mod_warn(moderator_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    _execute("""
        INSERT INTO moderator_stats (moderator_id, warn_count, audit_count, updated_at)
        VALUES (?,1,0,?) ON CONFLICT(moderator_id) DO UPDATE SET warn_count=warn_count+1, updated_at=?
    """, (moderator_id, now, now))

def increment_mod_audit(moderator_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    _execute("""
        INSERT INTO moderator_stats (moderator_id, warn_count, audit_count, updated_at)
        VALUES (?,0,1,?) ON CONFLICT(moderator_id) DO UPDATE SET audit_count=audit_count+1, updated_at=?
    """, (moderator_id, now, now))

def get_all_mod_stats() -> list[dict]:
    return _rows_as_dicts(_execute("SELECT * FROM moderator_stats ORDER BY audit_count DESC"))


# ─── ダッシュボード統計 ────────────────────────────────────

def get_dashboard_stats() -> dict:
    total_users      = _rows_as_dicts(_execute("SELECT COUNT(*) AS c FROM users"))[0]["c"] or 0
    active_users     = _rows_as_dicts(_execute(
        "SELECT COUNT(DISTINCT user_id) AS c FROM activity_logs WHERE sent_at >= datetime('now','-30 days')"
    ))[0]["c"] or 0
    total_messages   = _rows_as_dicts(_execute("SELECT COUNT(*) AS c FROM activity_logs"))[0]["c"] or 0
    total_punishments= _rows_as_dicts(_execute("SELECT COUNT(*) AS c FROM punishments"))[0]["c"] or 0
    active_rate      = round(active_users / total_users * 100, 1) if total_users else 0.0
    return {
        "total_users": total_users, "active_users": active_users,
        "active_rate": active_rate, "total_messages": total_messages,
        "total_punishments": total_punishments,
    }


# ─── CSV出力用 ─────────────────────────────────────────────

def get_all_users_for_csv() -> list[dict]:
    return _rows_as_dicts(_execute("SELECT * FROM users ORDER BY joined_at DESC"))

def get_all_punishments_for_csv() -> list[dict]:
    return _rows_as_dicts(_execute("SELECT * FROM punishments ORDER BY executed_at DESC"))

def get_all_invites_for_csv() -> list[dict]:
    return get_all_invites()
