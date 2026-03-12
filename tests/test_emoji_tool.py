import asyncio
from unittest.mock import AsyncMock

from nanobot.agent.tools.emoji import EmojiTool
from nanobot.bus.events import OutboundMessage


def test_emoji_tool_returns_error_without_discord_context() -> None:
    async def _run() -> None:
        tool = EmojiTool()
        result = await tool.execute(emoji="👍")
        assert result == "Error: Emoji tool currently only supports the discord channel"

    asyncio.run(_run())


def test_emoji_tool_dispatches_discord_reaction() -> None:
    async def _run() -> None:
        sent: list[OutboundMessage] = []
        tool = EmojiTool(send_callback=AsyncMock(side_effect=lambda m: sent.append(m)))
        tool.set_context("discord", "chan1", "msg1")

        result = await tool.execute(emoji="👀")

        assert result == "Emoji reaction added to discord:chan1"
        assert len(sent) == 1
        assert sent[0].channel == "discord"
        assert sent[0].chat_id == "chan1"
        assert sent[0].metadata == {
            "discord_action": "add_reaction",
            "message_id": "msg1",
            "emoji": "👀",
        }

    asyncio.run(_run())
