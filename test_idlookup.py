import asyncio
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, ".")

import discord  # noqa: E402
from bot.commands.idlookup import handle_idlookup  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def make_interaction(client):
    interaction = MagicMock()
    interaction.client = client
    interaction.response.send_message = AsyncMock()
    return interaction


class TestIdLookup(unittest.TestCase):
    def test_invalid_link_format(self):
        interaction = make_interaction(client=MagicMock())
        run(handle_idlookup(interaction, "not a link"))
        interaction.response.send_message.assert_awaited_once()
        (msg,), kwargs = interaction.response.send_message.call_args
        self.assertIn("認識できませんでした", msg)
        self.assertTrue(kwargs["ephemeral"])

    def test_success(self):
        author = types.SimpleNamespace(id=123456789012345678, name="example", global_name="Example")
        message = MagicMock()
        message.author = author
        channel = MagicMock()
        channel.fetch_message = AsyncMock(return_value=message)

        client = MagicMock()
        client.get_channel = MagicMock(return_value=channel)  # キャッシュヒット扱い

        interaction = make_interaction(client)
        run(handle_idlookup(interaction, "https://discord.com/channels/1/999/888"))

        interaction.response.send_message.assert_awaited_once()
        (msg,), kwargs = interaction.response.send_message.call_args
        self.assertIn("123456789012345678", msg)
        self.assertIn("example", msg)
        self.assertIn("Example", msg)
        self.assertTrue(kwargs["ephemeral"])

    def test_not_found(self):
        channel = MagicMock()
        channel.fetch_message = AsyncMock(
            side_effect=discord.NotFound(MagicMock(status=404), "Unknown Message")
        )
        client = MagicMock()
        client.get_channel = MagicMock(return_value=channel)

        interaction = make_interaction(client)
        run(handle_idlookup(interaction, "https://discord.com/channels/1/999/888"))

        (msg,), _ = interaction.response.send_message.call_args
        self.assertIn("見つかりませんでした", msg)

    def test_forbidden(self):
        client = MagicMock()
        client.get_channel = MagicMock(return_value=None)
        client.fetch_channel = AsyncMock(
            side_effect=discord.Forbidden(MagicMock(status=403), "Missing Access")
        )

        interaction = make_interaction(client)
        run(handle_idlookup(interaction, "https://discord.com/channels/1/999/888"))

        (msg,), _ = interaction.response.send_message.call_args
        self.assertIn("アクセスできない", msg)


if __name__ == "__main__":
    unittest.main()
