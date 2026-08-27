"""
bot/reports/db.py
reportsテーブルへのアクセス。

database.py は psycopg2（同期・ブロッキング、呼び出しごとに接続を開いて閉じる）で
書かれている。discord.pyの非同期イベントループ上で同期関数をそのままawaitなしに
呼ぶとイベントループ全体（Discordのgateway含む）が止まってしまうため、
asyncio.to_thread() で別スレッドに逃がしてから呼ぶ。
"""

import asyncio
from dataclasses import dataclass
from typing import Optional

import database


@dataclass
class Report:
    id: int
    reporter_id: str
    reporter_username: str
    target_type: str
    target_id: str
    target_username: str  # user/bot: ユーザー名, server: サーバー名（対象の表示名として汎用利用）
    creator_or_developer_id: Optional[str]
    categories: list[str]
    note: Optional[str]
    status: str
    maintainer_channel_message_id: Optional[str]
    rejection_reason: Optional[str]
    created_at: str
    decided_at: Optional[str]


def _row_to_report(row: dict) -> Report:
    return Report(
        id=row["id"],
        reporter_id=row["reporter_id"],
        reporter_username=row["reporter_username"],
        target_type=row.get("target_type", "user"),
        target_id=row["target_id"],
        target_username=row["target_username"],
        creator_or_developer_id=row.get("creator_or_developer_id"),
        categories=row["categories"],
        note=row.get("note"),
        status=row["status"],
        maintainer_channel_message_id=row.get("maintainer_channel_message_id"),
        rejection_reason=row.get("rejection_reason"),
        created_at=row["created_at"],
        decided_at=row.get("decided_at"),
    )


async def insert_pending_report(
    *,
    reporter_id: str,
    reporter_username: str,
    target_type: str,
    target_id: str,
    target_username: str,
    categories: list[str],
    note: Optional[str],
    creator_or_developer_id: Optional[str] = None,
) -> int:
    return await asyncio.to_thread(
        database.insert_pending_report,
        reporter_id,
        reporter_username,
        target_type,
        target_id,
        target_username,
        categories,
        note,
        creator_or_developer_id,
    )


async def set_maintainer_message_id(report_id: int, message_id: str) -> None:
    await asyncio.to_thread(database.set_report_maintainer_message_id, report_id, message_id)


async def mark_merged(report_id: int) -> Report:
    row = await asyncio.to_thread(database.mark_report_merged, report_id)
    return _row_to_report(row)


async def mark_rejected(report_id: int, reason: str) -> Report:
    row = await asyncio.to_thread(database.mark_report_rejected, report_id, reason)
    return _row_to_report(row)


async def get_report(report_id: int) -> Optional[Report]:
    row = await asyncio.to_thread(database.get_report, report_id)
    return _row_to_report(row) if row else None
