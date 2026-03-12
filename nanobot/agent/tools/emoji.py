"""Emoji tool for channel reactions."""

from typing import Any, Awaitable, Callable

from nanobot.agent.tools.base import Tool
from nanobot.bus.events import OutboundMessage


class EmojiTool(Tool):
    """Tool to react to Discord messages with an emoji."""

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
        default_message_id: str | None = None,
    ):
        self._send_callback = send_callback
        self._default_channel = default_channel
        self._default_chat_id = default_chat_id
        self._default_message_id = default_message_id

    def set_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """Set the current message context."""
        self._default_channel = channel
        self._default_chat_id = chat_id
        self._default_message_id = message_id

    def set_send_callback(self, callback: Callable[[OutboundMessage], Awaitable[None]]) -> None:
        """Set the callback used to dispatch emoji actions."""
        self._send_callback = callback

    @property
    def name(self) -> str:
        return "emoji"

    @property
    def description(self) -> str:
        return "Add an emoji reaction to a Discord message. Use this for lightweight acknowledgements."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "emoji": {
                    "type": "string",
                    "description": "Emoji to add as a reaction, like 👍, 👀, or party_parrot:1234567890",
                    "minLength": 1,
                },
                "channel": {
                    "type": "string",
                    "description": "Optional target channel; must be discord",
                },
                "chat_id": {
                    "type": "string",
                    "description": "Optional Discord channel ID; defaults to the current conversation",
                },
                "message_id": {
                    "type": "string",
                    "description": "Optional Discord message ID to react to; defaults to the triggering message",
                },
            },
            "required": ["emoji"],
        }

    async def execute(
        self,
        emoji: str,
        channel: str | None = None,
        chat_id: str | None = None,
        message_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        channel = channel or self._default_channel
        chat_id = chat_id or self._default_chat_id
        message_id = message_id or self._default_message_id

        if channel != "discord":
            return "Error: Emoji tool currently only supports the discord channel"
        if not chat_id:
            return "Error: No target Discord channel specified"
        if not message_id:
            return "Error: No target Discord message specified"
        if not self._send_callback:
            return "Error: Emoji sending not configured"

        msg = OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content="",
            metadata={
                "discord_action": "add_reaction",
                "message_id": message_id,
                "emoji": emoji,
            },
        )

        try:
            await self._send_callback(msg)
            return f"Emoji reaction added to discord:{chat_id}"
        except Exception as e:
            return f"Error sending emoji reaction: {str(e)}"
