import asyncio
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, ".")

from bot.commands.categories import label_for  # noqa: E402
from bot.commands.check import handle_check  # noqa: E402
from bot.data.blocklist import BlocklistCache, BlocklistFetchError  # noqa: E402


def run(coro):
    return asyncio.run(coro)


SAMPLE_ENTRY = {
    "id": "123456789012345678",
    "username": "example",
    "categories": ["scam", "phishing"],
    "note": "偽のNitroプレゼント企画",
    "status": "listed",
    "report_count": 3,
    "added_at": "2026-08-01",
    "updated_at": "2026-08-20",
}


class FakeResponse:
    def __init__(self, status: int, payload):
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, response: FakeResponse):
        self._response = response

    def get(self, url, timeout=None):
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class TestCategories(unittest.TestCase):
    def test_known_category(self):
        self.assertEqual(label_for("scam"), "詐欺")

    def test_unknown_category_falls_back_to_id(self):
        self.assertEqual(label_for("something-new"), "something-new")


class TestBlocklistCache(unittest.TestCase):
    def test_find_hit(self):
        cache = BlocklistCache()
        with patch("bot.data.blocklist.DATA_REPO", "someone/xgomi-discord"):
            with patch("aiohttp.ClientSession", return_value=FakeSession(FakeResponse(200, [SAMPLE_ENTRY]))):
                entry = run(cache.find("123456789012345678"))
        self.assertIsNotNone(entry)
        self.assertEqual(entry["username"], "example")

    def test_find_miss(self):
        cache = BlocklistCache()
        with patch("bot.data.blocklist.DATA_REPO", "someone/xgomi-discord"):
            with patch("aiohttp.ClientSession", return_value=FakeSession(FakeResponse(200, [SAMPLE_ENTRY]))):
                entry = run(cache.find("999999999999999999"))
        self.assertIsNone(entry)

    def test_non_200_raises(self):
        cache = BlocklistCache()
        with patch("bot.data.blocklist.DATA_REPO", "someone/xgomi-discord"):
            with patch("aiohttp.ClientSession", return_value=FakeSession(FakeResponse(500, None))):
                with self.assertRaises(BlocklistFetchError):
                    run(cache.get())

    def test_missing_repo_env_raises(self):
        cache = BlocklistCache()
        with patch("bot.data.blocklist.DATA_REPO", ""):
            with self.assertRaises(BlocklistFetchError):
                run(cache.get())

    def test_cache_is_reused_within_ttl(self):
        cache = BlocklistCache()
        session_factory = MagicMock(return_value=FakeSession(FakeResponse(200, [SAMPLE_ENTRY])))
        with patch("bot.data.blocklist.DATA_REPO", "someone/xgomi-discord"):
            with patch("aiohttp.ClientSession", session_factory):
                run(cache.get())
                run(cache.get())  # 2回目はキャッシュヒットのはず
        self.assertEqual(session_factory.call_count, 1)

    def test_force_refresh_bypasses_cache(self):
        cache = BlocklistCache()
        session_factory = MagicMock(return_value=FakeSession(FakeResponse(200, [SAMPLE_ENTRY])))
        with patch("bot.data.blocklist.DATA_REPO", "someone/xgomi-discord"):
            with patch("aiohttp.ClientSession", session_factory):
                run(cache.get())
                run(cache.get(force_refresh=True))
        self.assertEqual(session_factory.call_count, 2)


def make_interaction():
    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


class TestHandleCheck(unittest.TestCase):
    def test_neither_user_nor_id_given(self):
        interaction = make_interaction()
        run(handle_check(interaction, None, None))
        interaction.response.send_message.assert_awaited_once()
        interaction.response.defer.assert_not_awaited()

    def test_both_user_and_id_given(self):
        interaction = make_interaction()
        fake_user = MagicMock()
        run(handle_check(interaction, fake_user, "123456789012345678"))
        interaction.response.send_message.assert_awaited_once()
        interaction.response.defer.assert_not_awaited()

    def test_invalid_user_id_format(self):
        interaction = make_interaction()
        run(handle_check(interaction, None, "not-a-snowflake"))
        (msg,), _ = interaction.response.send_message.call_args
        self.assertIn("正しい形式ではありません", msg)
        interaction.response.defer.assert_not_awaited()

    def test_found_entry_reports_categories_and_note(self):
        interaction = make_interaction()
        with patch("bot.commands.check.blocklist_cache.find", new=AsyncMock(return_value=SAMPLE_ENTRY)):
            run(handle_check(interaction, None, "123456789012345678"))
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        (msg,), kwargs = interaction.followup.send.call_args
        self.assertIn("登録されています", msg)
        self.assertIn("詐欺", msg)
        self.assertIn("フィッシング", msg)
        self.assertTrue(kwargs["ephemeral"])

    def test_not_found_entry(self):
        interaction = make_interaction()
        with patch("bot.commands.check.blocklist_cache.find", new=AsyncMock(return_value=None)):
            run(handle_check(interaction, None, "999999999999999999"))
        (msg,), _ = interaction.followup.send.call_args
        self.assertIn("登録されていません", msg)

    def test_fetch_error_surfaces_as_message(self):
        interaction = make_interaction()
        with patch(
            "bot.commands.check.blocklist_cache.find",
            new=AsyncMock(side_effect=BlocklistFetchError("timeout")),
        ):
            run(handle_check(interaction, None, "123456789012345678"))
        (msg,), _ = interaction.followup.send.call_args
        self.assertIn("取得に失敗しました", msg)


if __name__ == "__main__":
    unittest.main()
