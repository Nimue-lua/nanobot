import asyncio
from types import SimpleNamespace
from urllib.parse import quote

from nanobot.bus.queue import MessageBus
from nanobot.bus.events import OutboundMessage
from nanobot.channels.discord import DiscordChannel
from nanobot.config.schema import DiscordConfig


class _Response:
    def __init__(self, status_code: int = 204, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


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


def test_message_create_accepts_bot_authors() -> None:
    async def _run() -> None:
        config = DiscordConfig(token="token", allow_from=["bot-user"], group_policy="open")
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
            "content": "hello from another bot",
            "author": {
                "id": "bot-user",
                "bot": True,
                "username": "helperbot",
                "discriminator": "0",
                "global_name": "Helper Bot",
            },
            "member": {},
            "attachments": [],
        }

        await channel._handle_message_create(payload)

        msg = await bus.consume_inbound()
        assert msg.content == "hello from another bot"
        assert msg.metadata["display_name"] == "Helper Bot"
        assert msg.metadata["tag"] == "@helperbot"

    asyncio.run(_run())


def test_message_create_ignores_self_messages() -> None:
    async def _run() -> None:
        config = DiscordConfig(token="token", allow_from=["bot-user"], group_policy="open")
        bus = MessageBus()
        channel = DiscordChannel(config, bus)
        channel._http = SimpleNamespace()
        channel._bot_user_id = "bot-user"

        async def _noop(_channel_id: str) -> None:
            return None

        channel._start_typing = _noop

        payload = {
            "id": "msg1",
            "channel_id": "chan1",
            "guild_id": "guild1",
            "content": "<@bot-user> hello again",
            "author": {
                "id": "bot-user",
                "bot": True,
                "username": "helperbot",
                "discriminator": "0",
                "global_name": "Helper Bot",
            },
            "mentions": [{"id": "bot-user"}],
            "member": {},
            "attachments": [],
        }

        await channel._handle_message_create(payload)

        assert bus.inbound_size == 0
        assert channel._get_recent_messages("chan1") == []

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
        assert msg.metadata["reply_message_id"] == "ref1"
        assert msg.metadata["reply_content"] == "Original question"
        assert msg.metadata["recent_messages"] == [
            {
                "display_name": "Bob Server",
                "tag": "bob#2222",
                "message_id": "msg0",
                "content": "Earlier context",
            }
        ]

    asyncio.run(_run())


def test_send_adds_discord_reaction_from_metadata() -> None:
    async def _run() -> None:
        config = DiscordConfig(token="token", allow_from=["user1"], group_policy="open")
        bus = MessageBus()
        channel = DiscordChannel(config, bus)

        put_calls: list[tuple[str, dict[str, str]]] = []

        async def _put(url: str, headers: dict[str, str]) -> _Response:
            put_calls.append((url, headers))
            return _Response()

        channel._http = SimpleNamespace(put=_put)

        await channel.send(
            OutboundMessage(
                channel="discord",
                chat_id="chan1",
                content="",
                metadata={
                    "discord_action": "add_reaction",
                    "message_id": "msg1",
                    "emoji": "👍",
                },
            )
        )

        assert put_calls == [
            (
                f"https://discord.com/api/v10/channels/chan1/messages/msg1/reactions/{quote('👍', safe='')}/@me",
                {"Authorization": "Bot token"},
            )
        ]

    asyncio.run(_run())


def test_send_posts_to_accessible_discord_channel() -> None:
    async def _run() -> None:
        config = DiscordConfig(token="token", allow_from=["user1"], group_policy="open")
        bus = MessageBus()
        channel = DiscordChannel(config, bus)

        get_calls: list[tuple[str, dict[str, str]]] = []
        post_calls: list[tuple[str, dict[str, str], dict | None]] = []

        async def _get(url: str, headers: dict[str, str]) -> _Response:
            get_calls.append((url, headers))
            return _Response(200, {"id": "chan1"})

        async def _post(
            url: str,
            headers: dict[str, str],
            json: dict | None = None,
            files: dict | None = None,
            data: dict | None = None,
        ) -> _Response:
            assert files is None
            assert data is None
            post_calls.append((url, headers, json))
            return _Response(200, {"id": "msg1"})

        channel._http = SimpleNamespace(get=_get, post=_post)

        await channel.send(
            OutboundMessage(channel="discord", chat_id="chan1", content="hello")
        )

        assert get_calls == [
            (
                "https://discord.com/api/v10/channels/chan1",
                {"Authorization": "Bot token"},
            )
        ]
        assert post_calls == [
            (
                "https://discord.com/api/v10/channels/chan1/messages",
                {"Authorization": "Bot token"},
                {"content": "hello"},
            )
        ]
        assert channel._get_recent_messages("chan1") == [
            {
                "display_name": "Discord",
                "content": "hello",
            }
        ]

    asyncio.run(_run())


def test_send_creates_dm_channel_when_target_is_user_id() -> None:
    async def _run() -> None:
        config = DiscordConfig(token="token", allow_from=["user1"], group_policy="open")
        bus = MessageBus()
        channel = DiscordChannel(config, bus)

        get_calls: list[tuple[str, dict[str, str]]] = []
        post_calls: list[tuple[str, dict[str, str], dict | None]] = []

        async def _get(url: str, headers: dict[str, str]) -> _Response:
            get_calls.append((url, headers))
            return _Response(404)

        async def _post(
            url: str,
            headers: dict[str, str],
            json: dict | None = None,
            files: dict | None = None,
            data: dict | None = None,
        ) -> _Response:
            assert files is None
            assert data is None
            post_calls.append((url, headers, json))
            if url.endswith("/users/@me/channels"):
                return _Response(200, {"id": "dm1"})
            return _Response(200, {"id": "msg1"})

        channel._http = SimpleNamespace(get=_get, post=_post)

        outbound = OutboundMessage(channel="discord", chat_id="user2", content="hello")
        await channel.send(outbound)

        assert get_calls == [
            (
                "https://discord.com/api/v10/channels/user2",
                {"Authorization": "Bot token"},
            )
        ]
        assert post_calls == [
            (
                "https://discord.com/api/v10/users/@me/channels",
                {"Authorization": "Bot token"},
                {"recipient_id": "user2"},
            ),
            (
                "https://discord.com/api/v10/channels/dm1/messages",
                {"Authorization": "Bot token"},
                {"content": "hello"},
            ),
        ]
        assert outbound.metadata["resolved_chat_id"] == "dm1"
        assert channel._get_recent_messages("dm1") == [
            {
                "display_name": "Discord",
                "content": "hello",
            }
        ]

    asyncio.run(_run())


def test_send_reuses_cached_dm_channel_from_inbound_dm() -> None:
    async def _run() -> None:
        config = DiscordConfig(token="token", allow_from=["user1"], group_policy="open")
        bus = MessageBus()
        channel = DiscordChannel(config, bus)

        get_calls: list[tuple[str, dict[str, str]]] = []
        post_calls: list[tuple[str, dict[str, str], dict | None]] = []

        async def _get(url: str, headers: dict[str, str]) -> _Response:
            get_calls.append((url, headers))
            return _Response(200, {})

        async def _post(
            url: str,
            headers: dict[str, str],
            json: dict | None = None,
            files: dict | None = None,
            data: dict | None = None,
        ) -> _Response:
            assert files is None
            assert data is None
            post_calls.append((url, headers, json))
            return _Response(200, {"id": "msg1"})

        async def _noop(_channel_id: str) -> None:
            return None

        channel._http = SimpleNamespace(get=_get, post=_post)
        channel._start_typing = _noop

        await channel._handle_message_create(
            {
                "id": "msg0",
                "channel_id": "dm1",
                "content": "hello from dm",
                "author": {
                    "id": "user1",
                    "username": "alice",
                    "discriminator": "1234",
                    "global_name": "Alice Global",
                },
                "attachments": [],
            }
        )
        await bus.consume_inbound()

        get_calls.clear()
        post_calls.clear()

        outbound = OutboundMessage(channel="discord", chat_id="user1", content="reply")
        await channel.send(outbound)

        assert get_calls == []
        assert post_calls == [
            (
                "https://discord.com/api/v10/channels/dm1/messages",
                {"Authorization": "Bot token"},
                {"content": "reply"},
            )
        ]
        assert outbound.metadata["resolved_chat_id"] == "dm1"
        assert channel._get_recent_messages("dm1")[-1] == {
            "display_name": "Discord",
            "content": "reply",
        }

    asyncio.run(_run())
