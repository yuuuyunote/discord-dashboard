import asyncio
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, ".")

from bot.reports.github import (  # noqa: E402
    ReportCommitError,
    SchemaValidationError,
    commit_user_record,
    get_existing_record,
    merge_records,
    validate_user_record,
)

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "username", "categories", "note", "status", "report_count", "added_at", "updated_at"],
    "properties": {
        "$schema": {"type": "string"},
        "id": {"type": "string", "pattern": "^[0-9]{17,20}$"},
        "username": {"type": "string", "minLength": 1, "maxLength": 32},
        "display_name": {"type": "string", "minLength": 1, "maxLength": 32},
        "categories": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {
                "enum": [
                    "scam", "phishing", "impersonation", "raid-spam", "dm-solicitation",
                    "harassment", "doxxing", "hate-speech", "bot-abuse", "other",
                ]
            },
        },
        "note": {"type": "string", "minLength": 1, "maxLength": 1000},
        "status": {"enum": ["listed", "delisted"]},
        "username_history": {"type": "array", "maxItems": 50, "items": {"type": "string"}},
        "report_count": {"type": "integer", "minimum": 1},
        "added_at": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
        "updated_at": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
    },
}

VALID_RECORD = {
    "id": "123456789012345678",
    "username": "example",
    "categories": ["scam"],
    "note": "test note",
    "status": "listed",
    "report_count": 1,
    "added_at": "2026-08-21",
    "updated_at": "2026-08-21",
}


def run(coro):
    return asyncio.run(coro)


class FakeResponse:
    def __init__(self, status: int, json_body=None, text_body: str = ""):
        self.status = status
        self._json_body = json_body
        self._text_body = text_body

    async def json(self, content_type=None):
        return self._json_body

    async def text(self):
        return self._text_body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """get/put呼び出しを事前登録した応答キューで返す簡易フェイク。"""

    def __init__(self, get_responses=None, put_responses=None):
        self._get_responses = list(get_responses or [])
        self._put_responses = list(put_responses or [])
        self.put_calls = []

    def get(self, url, headers=None, params=None):
        return self._get_responses.pop(0)

    def put(self, url, headers=None, json=None):
        self.put_calls.append({"url": url, "json": json})
        return self._put_responses.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class TestValidateUserRecord(unittest.TestCase):
    def test_valid_record_passes(self):
        with patch("bot.reports.github._schema_cache.get", new=AsyncMock(return_value=SCHEMA)):
            run(validate_user_record(VALID_RECORD))  # raises on failure

    def test_invalid_category_raises(self):
        bad = {**VALID_RECORD, "categories": ["not-a-category"]}
        with patch("bot.reports.github._schema_cache.get", new=AsyncMock(return_value=SCHEMA)):
            with self.assertRaises(SchemaValidationError):
                run(validate_user_record(bad))

    def test_missing_required_field_raises(self):
        bad = {k: v for k, v in VALID_RECORD.items() if k != "note"}
        with patch("bot.reports.github._schema_cache.get", new=AsyncMock(return_value=SCHEMA)):
            with self.assertRaises(SchemaValidationError):
                run(validate_user_record(bad))


class TestMergeRecords(unittest.TestCase):
    def test_new_record_when_no_existing(self):
        merged = merge_records(
            None,
            target_id="123456789012345678",
            target_username="example",
            categories=["scam"],
            note="first note",
            today="2026-08-21",
        )
        self.assertEqual(merged["report_count"], 1)
        self.assertEqual(merged["categories"], ["scam"])
        self.assertEqual(merged["note"], "first note")
        self.assertEqual(merged["added_at"], "2026-08-21")

    def test_note_defaults_when_none(self):
        merged = merge_records(
            None,
            target_id="123456789012345678",
            target_username="example",
            categories=["scam"],
            note=None,
            today="2026-08-21",
        )
        self.assertTrue(merged["note"])

    def test_existing_record_merges_categories_and_bumps_count(self):
        existing = {**VALID_RECORD, "categories": ["scam"], "report_count": 2, "added_at": "2026-08-01"}
        merged = merge_records(
            existing,
            target_id="123456789012345678",
            target_username="example_renamed",
            categories=["phishing", "scam"],
            note="second report's note (ignored)",
            today="2026-08-22",
        )
        self.assertEqual(merged["report_count"], 3)
        self.assertEqual(merged["categories"], ["phishing", "scam"])
        self.assertEqual(merged["note"], "test note")  # 元のnoteを維持
        self.assertEqual(merged["username"], "example_renamed")
        self.assertEqual(merged["added_at"], "2026-08-01")  # 変わらない
        self.assertEqual(merged["updated_at"], "2026-08-22")

    def test_delisted_becomes_listed_again_on_new_report(self):
        existing = {**VALID_RECORD, "status": "delisted"}
        merged = merge_records(
            existing,
            target_id="123456789012345678",
            target_username="example",
            categories=["scam"],
            note=None,
            today="2026-08-22",
        )
        self.assertEqual(merged["status"], "listed")


class TestGetExistingRecord(unittest.TestCase):
    def test_returns_none_on_404(self):
        session = FakeSession(get_responses=[FakeResponse(404)])
        with patch("bot.reports.github.DATA_REPO", "someone/discord-reports"):
            with patch("bot.reports.github.GITHUB_TOKEN", "dummy"):
                with patch("aiohttp.ClientSession", return_value=session):
                    result = run(get_existing_record("123456789012345678"))
        self.assertIsNone(result)

    def test_decodes_existing_content(self):
        import base64
        import json

        content_b64 = base64.b64encode(json.dumps(VALID_RECORD).encode()).decode()
        session = FakeSession(get_responses=[FakeResponse(200, {"sha": "abc123", "content": content_b64})])
        with patch("bot.reports.github.DATA_REPO", "someone/discord-reports"):
            with patch("bot.reports.github.GITHUB_TOKEN", "dummy"):
                with patch("aiohttp.ClientSession", return_value=session):
                    result = run(get_existing_record("123456789012345678"))
        self.assertEqual(result["id"], VALID_RECORD["id"])


class TestCommitUserRecord(unittest.TestCase):
    def test_creates_new_file_when_none_exists(self):
        session = FakeSession(get_responses=[FakeResponse(404)], put_responses=[FakeResponse(201)])

        with patch("bot.reports.github.DATA_REPO", "someone/discord-reports"):
            with patch("bot.reports.github.GITHUB_TOKEN", "dummy"):
                with patch("bot.reports.github._schema_cache.get", new=AsyncMock(return_value=SCHEMA)):
                    with patch("aiohttp.ClientSession", return_value=session):
                        run(commit_user_record(VALID_RECORD, commit_message="test"))

        self.assertEqual(len(session.put_calls), 1)
        self.assertNotIn("sha", session.put_calls[0]["json"])

    def test_updates_existing_file_with_sha(self):
        session = FakeSession(
            get_responses=[FakeResponse(200, {"sha": "existing-sha", "content": "e30="})],
            put_responses=[FakeResponse(200)],
        )

        with patch("bot.reports.github.DATA_REPO", "someone/discord-reports"):
            with patch("bot.reports.github.GITHUB_TOKEN", "dummy"):
                with patch("bot.reports.github._schema_cache.get", new=AsyncMock(return_value=SCHEMA)):
                    with patch("aiohttp.ClientSession", return_value=session):
                        run(commit_user_record(VALID_RECORD, commit_message="test"))

        self.assertEqual(session.put_calls[0]["json"]["sha"], "existing-sha")

    def test_invalid_record_never_reaches_github(self):
        bad = {**VALID_RECORD, "categories": []}  # minItems:1 違反
        with patch("bot.reports.github.DATA_REPO", "someone/discord-reports"):
            with patch("bot.reports.github.GITHUB_TOKEN", "dummy"):
                with patch("bot.reports.github._schema_cache.get", new=AsyncMock(return_value=SCHEMA)):
                    with patch("aiohttp.ClientSession") as session_ctor:
                        with self.assertRaises(SchemaValidationError):
                            run(commit_user_record(bad, commit_message="test"))
                        session_ctor.assert_not_called()

    def test_missing_token_raises_before_any_call(self):
        with patch("bot.reports.github.DATA_REPO", "someone/discord-reports"):
            with patch("bot.reports.github.GITHUB_TOKEN", ""):
                with patch("bot.reports.github._schema_cache.get", new=AsyncMock(return_value=SCHEMA)):
                    with self.assertRaises(ReportCommitError):
                        run(commit_user_record(VALID_RECORD, commit_message="test"))


if __name__ == "__main__":
    unittest.main()
