import asyncio
from types import SimpleNamespace

from nanobot.bus.queue import MessageBus
from nanobot.channels.discord import DiscordChannel
from nanobot.config.schema import DiscordConfig


def test_message_create_includes_author_tag_and_display_name() -> None:
    async def _run() -> None:
        config = DiscordConfig(token="token", allow_from=["user1"], group_policy="open")
        bus = MessageBus()
        channel = DiscordChannel(config, bus)
        channel._http = SimpleNamespace()

        async def _noop(_channel_id: str) -> None:
            return None

        channel._start_typing = _noop

        payload = {
            "id": "msg1",
            "channel_id": "chan1",
            "guild_id": "guild1",
            "content": "hello",
            "author": {
                "id": "user1",
                "username": "alice",
                "discriminator": "1234",
                "global_name": "Alice Global",
            },
            "member": {"nick": "Alice Server"},
            "attachments": [],
        }

        await channel._handle_message_create(payload)

        msg = await bus.consume_inbound()
        assert msg.metadata["tag"] == "alice#1234"
        assert msg.metadata["display_name"] == "Alice Server"
        assert msg.metadata["username"] == "alice"

    asyncio.run(_run())


def test_build_author_tag_supports_modern_discord_usernames() -> None:
    assert DiscordChannel._build_author_tag("alice", "0") == "@alice"
    assert DiscordChannel._build_author_tag("alice", None) == "@alice"


def test_message_create_includes_recent_messages_and_reply_context() -> None:
    async def _run() -> None:
        config = DiscordConfig(token="token", allow_from=["user1"], group_policy="open")
        bus = MessageBus()
        channel = DiscordChannel(config, bus)
        channel._http = SimpleNamespace()

        async def _noop(_channel_id: str) -> None:
            return None

        channel._start_typing = _noop

        await channel._handle_message_create(
            {
                "id": "msg0",
                "channel_id": "chan1",
                "guild_id": "guild1",
                "content": "Earlier context",
                "author": {
                    "id": "user2",
                    "username": "bob",
                    "discriminator": "2222",
                    "global_name": "Bob",
                },
                "member": {"nick": "Bob Server"},
                "attachments": [],
            }
        )

        await channel._handle_message_create(
            {
                "id": "msg1",
                "channel_id": "chan1",
                "guild_id": "guild1",
                "content": "Replying now",
                "author": {
                    "id": "user1",
                    "username": "alice",
                    "discriminator": "1234",
                    "global_name": "Alice Global",
                },
                "member": {"nick": "Alice Server"},
                "attachments": [],
                "referenced_message": {
                    "id": "ref1",
                    "content": "Original question",
                    "author": {
                        "id": "user3",
                        "username": "carol",
                        "discriminator": "0",
                        "global_name": "Carol Global",
                    },
                    "member": {"nick": "Carol Server"},
                },
            }
        )

        msg = await bus.consume_inbound()
        assert msg.metadata["reply_display_name"] == "Carol Server"
        assert msg.metadata["reply_tag"] == "@carol"
        assert msg.metadata["reply_content"] == "Original question"
        assert msg.metadata["recent_messages"] == [
            {
                "display_name": "Bob Server",
                "tag": "bob#2222",
                "content": "Earlier context",
            }
        ]

    asyncio.run(_run())
