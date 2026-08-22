"""
bot/reports/github.py
承認された通報を discord-reports リポジトリの users/<id>.json として
GitHub Contents API 経由で直接 main へ commit する。

スキーマは discord-reports/schema/user.schema.json を実行時に取得して検証する
（Node/Workers側のvalidation.mjsをPythonに手で複製するのではなく、
スキーマ自体を単一情報源として両方が参照する形にしている）。
jsonschemaはevalを使わないので、Cloudflare Workersで問題になった制約はここでは関係ない。

filename（<id>.json）とレコード内のidの一致は additionalProperties 等と違い
JSON Schema単体では表現できないため、ここで別途チェックする
（discord-reportsのvalidation.mjsが持っているのと同じ追加ロジック）。
"""

import base64
import json
import os
import time
from typing import Optional

import aiohttp
import jsonschema

DATA_REPO = os.getenv("BLOCKLIST_DATA_REPO", "")  # 例: "yuuuyunote/discord-reports"
DATA_BRANCH = os.getenv("BLOCKLIST_DATA_BRANCH", "main")
GITHUB_TOKEN = os.getenv("REPORTS_GITHUB_TOKEN", "")

GITHUB_API_BASE = "https://api.github.com"
SCHEMA_CACHE_TTL_SECONDS = 300


class ReportCommitError(Exception):
    pass


class SchemaValidationError(ReportCommitError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


class _SchemaCache:
    def __init__(self) -> None:
        self._schema: Optional[dict] = None
        self._fetched_at: float = 0.0

    async def get(self) -> dict:
        now = time.monotonic()
        if self._schema is not None and (now - self._fetched_at) < SCHEMA_CACHE_TTL_SECONDS:
            return self._schema

        if not DATA_REPO:
            raise ReportCommitError("BLOCKLIST_DATA_REPO が設定されていません。")

        url = f"https://raw.githubusercontent.com/{DATA_REPO}/{DATA_BRANCH}/schema/user.schema.json"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as res:
                if res.status != 200:
                    raise ReportCommitError(f"スキーマの取得に失敗しました（status: {res.status}）")
                schema = await res.json(content_type=None)

        self._schema = schema
        self._fetched_at = now
        return schema


_schema_cache = _SchemaCache()


def _github_headers() -> dict:
    if not GITHUB_TOKEN:
        raise ReportCommitError("REPORTS_GITHUB_TOKEN が設定されていません。")
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def validate_user_record(record: dict) -> None:
    """schema/user.schema.json + filename==id の追加チェック。不正なら SchemaValidationError を投げる。"""
    schema = await _schema_cache.get()

    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema)

    errors = sorted(validator.iter_errors(record), key=lambda e: e.path)
    error_messages = [f"{'.'.join(str(p) for p in e.path) or '(root)'}: {e.message}" for e in errors]

    if error_messages:
        raise SchemaValidationError(error_messages)


async def _get_existing_file(session: aiohttp.ClientSession, path: str) -> Optional[tuple[str, dict]]:
    """既存の users/<id>.json があれば (sha, デコード済みdict) を返す。無ければNone。"""
    url = f"{GITHUB_API_BASE}/repos/{DATA_REPO}/contents/{path}"
    async with session.get(url, headers=_github_headers(), params={"ref": DATA_BRANCH}) as res:
        if res.status == 404:
            return None
        if res.status != 200:
            raise ReportCommitError(f"既存ファイルの確認に失敗しました（status: {res.status}）: {await res.text()}")
        body = await res.json()
        sha = body.get("sha")
        content_b64 = body.get("content", "")
        decoded = base64.b64decode(content_b64.replace("\n", "")).decode("utf-8")
        return sha, json.loads(decoded)


async def get_existing_record(target_id: str) -> Optional[dict]:
    """target_id の既存レコードがあれば返す（承認時のマージ判定用）。存在しなければNone。"""
    if not DATA_REPO:
        raise ReportCommitError("BLOCKLIST_DATA_REPO が設定されていません。")
    path = f"users/{target_id}.json"
    async with aiohttp.ClientSession() as session:
        existing = await _get_existing_file(session, path)
    return existing[1] if existing else None


def merge_records(
    existing: Optional[dict],
    *,
    target_id: str,
    target_username: str,
    categories: list[str],
    note: Optional[str],
    today: str,
) -> dict:
    """
    承認時に users/<id>.json へ書き込む最終形を組み立てる。

    設計上noteは1レコードにつき1つしか持てないため、既存レコードがある場合は
    noteを上書きしない（最初の通報時のnoteを維持する）。カテゴリは和集合、
    report_countは+1、statusは常にlistedへ戻す（delisted済みの相手が
    再度通報された場合の再listedもこの経路で扱う）。
    """
    if existing is None:
        return {
            "id": target_id,
            "username": target_username,
            "categories": sorted(set(categories)),
            "note": note or "(通報時の補足なし)",
            "status": "listed",
            "report_count": 1,
            "added_at": today,
            "updated_at": today,
        }

    merged = dict(existing)
    merged["username"] = target_username  # 直近確認時点のスナップショットに更新
    merged["categories"] = sorted(set(existing.get("categories", [])) | set(categories))
    merged["status"] = "listed"
    merged["report_count"] = int(existing.get("report_count", 0)) + 1
    merged["updated_at"] = today
    return merged


async def _get_existing_sha(session: aiohttp.ClientSession, path: str) -> Optional[str]:
    url = f"{GITHUB_API_BASE}/repos/{DATA_REPO}/contents/{path}"
    async with session.get(url, headers=_github_headers(), params={"ref": DATA_BRANCH}) as res:
        if res.status == 404:
            return None
        if res.status != 200:
            raise ReportCommitError(f"既存ファイルの確認に失敗しました（status: {res.status}）: {await res.text()}")
        body = await res.json()
        return body.get("sha")


async def commit_user_record(record: dict, *, commit_message: str) -> None:
    """
    users/<id>.json を検証してからGitHub Contents APIでmainへ直接commitする。
    PRを経由しない設計のため、ここでの検証が実質唯一の関所になる。
    """
    if not DATA_REPO:
        raise ReportCommitError("BLOCKLIST_DATA_REPO が設定されていません。")

    record_id = record.get("id")
    if not isinstance(record_id, str) or not record_id:
        raise SchemaValidationError(['"id" must be a non-empty string'])
    filename = f"{record_id}.json"
    path = f"users/{filename}"

    await validate_user_record(record)

    content_str = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    content_b64 = base64.b64encode(content_str.encode("utf-8")).decode("ascii")

    async with aiohttp.ClientSession() as session:
        existing_sha = await _get_existing_sha(session, path)

        url = f"{GITHUB_API_BASE}/repos/{DATA_REPO}/contents/{path}"
        payload = {
            "message": commit_message,
            "content": content_b64,
            "branch": DATA_BRANCH,
        }
        if existing_sha:
            payload["sha"] = existing_sha

        async with session.put(url, headers=_github_headers(), json=payload) as res:
            if res.status not in (200, 201):
                raise ReportCommitError(f"commitに失敗しました（status: {res.status}）: {await res.text()}")
