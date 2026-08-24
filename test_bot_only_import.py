import asyncio
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, ".")

import bot_only  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def make_member(id_, name, is_bot=False):
    m = MagicMock()
    m.id = id_
    m.bot = is_bot
    m.__str__.return_value = name
    m.created_at.isoformat.return_value = "2026-01-01T00:00:00+00:00"
    m.joined_at.isoformat.return_value = "2026-02-01T00:00:00+00:00"
    return m


class FakeAsyncMemberIterator:
    def __init__(self, members):
        self._members = list(members)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._members:
            raise StopAsyncIteration
        return self._members.pop(0)


class TestImportExistingMembers(unittest.TestCase):
    def test_single_bulk_call_regardless_of_member_count(self):
        members = [make_member(i, f"user{i}") for i in range(50)] + [make_member(999, "somebot", is_bot=True)]
        guild = MagicMock()
        guild.fetch_members = MagicMock(return_value=FakeAsyncMemberIterator(members))

        with patch("bot_only.database.insert_new_users_bulk", return_value=50) as mock_bulk:
            run(bot_only._import_existing_members(guild))

        mock_bulk.assert_called_once()
        rows = mock_bulk.call_args[0][0]
        self.assertEqual(len(rows), 50)  # bot は除外されている

    def test_no_call_when_no_members(self):
        guild = MagicMock()
        guild.fetch_members = MagicMock(return_value=FakeAsyncMemberIterator([]))

        with patch("bot_only.database.insert_new_users_bulk", return_value=0) as mock_bulk:
            run(bot_only._import_existing_members(guild))

        mock_bulk.assert_called_once_with([])


if __name__ == "__main__":
    unittest.main()
