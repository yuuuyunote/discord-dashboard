import asyncio
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, ".")

import discord_rest  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def make_response(status_code, json_body=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.text = text
    return resp


class FakeAsyncClient:
    def __init__(self, get_responses=None, post_responses=None):
        self._get = list(get_responses or [])
        self._post = list(post_responses or [])

    async def get(self, url, headers=None):
        return self._get.pop(0)

    async def post(self, url, headers=None, json=None):
        return self._post.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class TestGetGuildMember(unittest.TestCase):
    def test_returns_member_on_200(self):
        client = FakeAsyncClient(get_responses=[make_response(200, {"roles": ["1", "2"]})])
        with patch("httpx.AsyncClient", return_value=client):
            member = run(discord_rest.get_guild_member("g1", "u1"))
        self.assertEqual(member["roles"], ["1", "2"])

    def test_returns_none_on_404(self):
        client = FakeAsyncClient(get_responses=[make_response(404)])
        with patch("httpx.AsyncClient", return_value=client):
            member = run(discord_rest.get_guild_member("g1", "u1"))
        self.assertIsNone(member)

    def test_raises_on_other_error(self):
        client = FakeAsyncClient(get_responses=[make_response(500, text="oops")])
        with patch("httpx.AsyncClient", return_value=client):
            with self.assertRaises(discord_rest.DiscordRestError):
                run(discord_rest.get_guild_member("g1", "u1"))


class TestSendDm(unittest.TestCase):
    def test_success_returns_message(self):
        client = FakeAsyncClient(
            post_responses=[make_response(200, {"id": "chan1"}), make_response(200, {"id": "msg1"})]
        )
        with patch("httpx.AsyncClient", return_value=client):
            sent = run(discord_rest.send_dm("u1", "hello"))
        self.assertEqual(sent["id"], "msg1")

    def test_dm_blocked_raises_403(self):
        client = FakeAsyncClient(
            post_responses=[make_response(200, {"id": "chan1"}), make_response(403, text="blocked")]
        )
        with patch("httpx.AsyncClient", return_value=client):
            with self.assertRaises(discord_rest.DiscordRestError) as ctx:
                run(discord_rest.send_dm("u1", "hello"))
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
