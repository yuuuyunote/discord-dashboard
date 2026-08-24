import asyncio
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, ".")

import discord  # noqa: E402
from bot.commands.report import handle_report  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def make_interaction():
    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.user = MagicMock()
    return interaction


def make_attachment(content_type="image/png"):
    attachment = MagicMock()
    attachment.content_type = content_type
    return attachment


class TestHandleReport(unittest.TestCase):
    def test_rejects_non_image_attachment(self):
        interaction = make_interaction()
        run(handle_report(interaction, "123456789012345678", make_attachment("text/plain"), None, MagicMock()))
        interaction.response.send_message.assert_awaited_once()
        (msg,), _ = interaction.response.send_message.call_args
        self.assertIn("画像ファイル", msg)
        interaction.response.defer.assert_not_awaited()

    def test_rejects_invalid_user_id_format(self):
        interaction = make_interaction()
        run(handle_report(interaction, "not-a-snowflake", make_attachment(), None, MagicMock()))
        (msg,), _ = interaction.response.send_message.call_args
        self.assertIn("正しい形式ではありません", msg)
        interaction.response.defer.assert_not_awaited()

    def test_user_not_found(self):
        interaction = make_interaction()
        interaction.client.fetch_user = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "Unknown User"))
        run(handle_report(interaction, "123456789012345678", make_attachment(), None, MagicMock()))
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        (msg,), _ = interaction.followup.send.call_args
        self.assertIn("存在しないようです", msg)

    def test_valid_input_sends_category_view(self):
        interaction = make_interaction()
        fetched_user = MagicMock(id=123456789012345678, name="target_user")
        interaction.client.fetch_user = AsyncMock(return_value=fetched_user)
        run(handle_report(interaction, "123456789012345678", make_attachment(), "note text", MagicMock()))
        interaction.followup.send.assert_awaited_once()
        _, kwargs = interaction.followup.send.call_args
        self.assertTrue(kwargs["ephemeral"])
        self.assertIn("view", kwargs)


if __name__ == "__main__":
    unittest.main()
