"""Discord channel implementation using Discord Gateway websocket."""

import asyncio
import json
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import websockets
from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.paths import get_media_dir
from nanobot.config.schema import DiscordConfig
from nanobot.utils.helpers import split_message

DISCORD_API_BASE = "https://discord.com/api/v10"
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024  # 20MB
MAX_MESSAGE_LEN = 2000  # Discord message character limit
RECENT_CONTEXT_MESSAGES = 8
RECENT_CONTEXT_CHARS = 240


class DiscordChannel(BaseChannel):
    """Discord channel using Gateway websocket."""

    name = "discord"

    def __init__(self, config: DiscordConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: DiscordConfig = config
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._seq: int | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._typing_tasks: dict[str, asyncio.Task] = {}
        self._http: httpx.AsyncClient | None = None
        self._bot_user_id: str | None = None
        self._recent_messages: dict[str, deque[dict[str, str]]] = {}

    async def start(self) -> None:
        """Start the Discord gateway connection."""
        if not self.config.token:
            logger.error("Discord bot token not configured")
            return

        self._running = True
        self._http = httpx.AsyncClient(timeout=30.0)

        while self._running:
            try:
                logger.info("Connecting to Discord gateway...")
                async with websockets.connect(self.config.gateway_url) as ws:
                    self._ws = ws
                    await self._gateway_loop()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Discord gateway error: {}", e)
                if self._running:
                    logger.info("Reconnecting to Discord gateway in 5 seconds...")
                    await asyncio.sleep(5)

    async def stop(self) -> None:
        """Stop the Discord channel."""
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        for task in self._typing_tasks.values():
            task.cancel()
        self._typing_tasks.clear()
        if self._ws:
            await self._ws.close()
            self._ws = None
        if self._http:
            await self._http.aclose()
            self._http = None

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Discord REST API, including file attachments."""
        if not self._http:
            logger.warning("Discord HTTP client not initialized")
            return

        if msg.metadata.get("discord_action") == "add_reaction":
            message_id = msg.metadata.get("message_id")
            emoji = msg.metadata.get("emoji")
            if not isinstance(message_id, str) or not message_id:
                logger.warning("Discord reaction missing message_id")
                return
            if not isinstance(emoji, str) or not emoji:
                logger.warning("Discord reaction missing emoji")
                return
            await self._add_reaction(msg.chat_id, message_id, emoji)
            return

        url = f"{DISCORD_API_BASE}/channels/{msg.chat_id}/messages"
        headers = {"Authorization": f"Bot {self.config.token}"}

        try:
            sent_media = False
            failed_media: list[str] = []

            # Send file attachments first
            for media_path in msg.media or []:
                if await self._send_file(url, headers, media_path, reply_to=msg.reply_to):
                    sent_media = True
                else:
                    failed_media.append(Path(media_path).name)

            # Send text content
            chunks = split_message(msg.content or "", MAX_MESSAGE_LEN)
            if not chunks and failed_media and not sent_media:
                chunks = split_message(
                    "\n".join(f"[attachment: {name} - send failed]" for name in failed_media),
                    MAX_MESSAGE_LEN,
                )
            if not chunks:
                return

            for i, chunk in enumerate(chunks):
                payload: dict[str, Any] = {"content": chunk}

                # Let the first successful attachment carry the reply if present.
                if i == 0 and msg.reply_to and not sent_media:
                    payload["message_reference"] = {"message_id": msg.reply_to}
                    payload["allowed_mentions"] = {"replied_user": False}

                if not await self._send_payload(url, headers, payload):
                    break  # Abort remaining chunks on failure
        finally:
            await self._stop_typing(msg.chat_id)

    async def _send_payload(
        self, url: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> bool:
        """Send a single Discord API payload with retry on rate-limit. Returns True on success."""
        for attempt in range(3):
            try:
                response = await self._http.post(url, headers=headers, json=payload)
                if response.status_code == 429:
                    data = response.json()
                    retry_after = float(data.get("retry_after", 1.0))
                    logger.warning("Discord rate limited, retrying in {}s", retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                response.raise_for_status()
                return True
            except Exception as e:
                if attempt == 2:
                    logger.error("Error sending Discord message: {}", e)
                else:
                    await asyncio.sleep(1)
        return False

    async def _add_reaction(self, channel_id: str, message_id: str, emoji: str) -> bool:
        """Add a reaction to a Discord message via REST API."""
        if not self._http:
            logger.warning("Discord HTTP client not initialized")
            return False

        encoded_emoji = quote(emoji, safe="")
        url = (
            f"{DISCORD_API_BASE}/channels/{channel_id}/messages/"
            f"{message_id}/reactions/{encoded_emoji}/@me"
        )
        headers = {"Authorization": f"Bot {self.config.token}"}

        for attempt in range(3):
            try:
                response = await self._http.put(url, headers=headers)
                if response.status_code == 429:
                    data = response.json()
                    retry_after = float(data.get("retry_after", 1.0))
                    logger.warning("Discord rate limited, retrying reaction in {}s", retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                response.raise_for_status()
                logger.debug("Discord reaction added: {} -> {}", emoji, message_id)
                return True
            except Exception as e:
                if attempt == 2:
                    logger.error("Error adding Discord reaction {} to {}: {}", emoji, message_id, e)
                else:
                    await asyncio.sleep(1)
        return False

    async def _send_file(
        self,
        url: str,
        headers: dict[str, str],
        file_path: str,
        reply_to: str | None = None,
    ) -> bool:
        """Send a file attachment via Discord REST API using multipart/form-data."""
        path = Path(file_path)
        if not path.is_file():
            logger.warning("Discord file not found, skipping: {}", file_path)
            return False

        if path.stat().st_size > MAX_ATTACHMENT_BYTES:
            logger.warning("Discord file too large (>20MB), skipping: {}", path.name)
            return False

        payload_json: dict[str, Any] = {}
        if reply_to:
            payload_json["message_reference"] = {"message_id": reply_to}
            payload_json["allowed_mentions"] = {"replied_user": False}

        for attempt in range(3):
            try:
                with open(path, "rb") as f:
                    files = {"files[0]": (path.name, f, "application/octet-stream")}
                    data: dict[str, Any] = {}
                    if payload_json:
                        data["payload_json"] = json.dumps(payload_json)
                    response = await self._http.post(
                        url, headers=headers, files=files, data=data
                    )
                if response.status_code == 429:
                    resp_data = response.json()
                    retry_after = float(resp_data.get("retry_after", 1.0))
                    logger.warning("Discord rate limited, retrying in {}s", retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                response.raise_for_status()
                logger.info("Discord file sent: {}", path.name)
                return True
            except Exception as e:
                if attempt == 2:
                    logger.error("Error sending Discord file {}: {}", path.name, e)
                else:
                    await asyncio.sleep(1)
        return False

    async def _gateway_loop(self) -> None:
        """Main gateway loop: identify, heartbeat, dispatch events."""
        if not self._ws:
            return

        async for raw in self._ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Invalid JSON from Discord gateway: {}", raw[:100])
                continue

            op = data.get("op")
            event_type = data.get("t")
            seq = data.get("s")
            payload = data.get("d")

            if seq is not None:
                self._seq = seq

            if op == 10:
                # HELLO: start heartbeat and identify
                interval_ms = payload.get("heartbeat_interval", 45000)
                await self._start_heartbeat(interval_ms / 1000)
                await self._identify()
            elif op == 0 and event_type == "READY":
                logger.info("Discord gateway READY")
                # Capture bot user ID for mention detection
                user_data = payload.get("user") or {}
                self._bot_user_id = user_data.get("id")
                logger.info("Discord bot connected as user {}", self._bot_user_id)
            elif op == 0 and event_type == "MESSAGE_CREATE":
                await self._handle_message_create(payload)
            elif op == 7:
                # RECONNECT: exit loop to reconnect
                logger.info("Discord gateway requested reconnect")
                break
            elif op == 9:
                # INVALID_SESSION: reconnect
                logger.warning("Discord gateway invalid session")
                break

    async def _identify(self) -> None:
        """Send IDENTIFY payload."""
        if not self._ws:
            return

        identify = {
            "op": 2,
            "d": {
                "token": self.config.token,
                "intents": self.config.intents,
                "properties": {
                    "os": "nanobot",
                    "browser": "nanobot",
                    "device": "nanobot",
                },
            },
        }
        await self._ws.send(json.dumps(identify))

    async def _start_heartbeat(self, interval_s: float) -> None:
        """Start or restart the heartbeat loop."""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()

        async def heartbeat_loop() -> None:
            while self._running and self._ws:
                payload = {"op": 1, "d": self._seq}
                try:
                    await self._ws.send(json.dumps(payload))
                except Exception as e:
                    logger.warning("Discord heartbeat failed: {}", e)
                    break
                await asyncio.sleep(interval_s)

        self._heartbeat_task = asyncio.create_task(heartbeat_loop())

    async def _handle_message_create(self, payload: dict[str, Any]) -> None:
        """Handle incoming Discord messages."""
        author = payload.get("author") or {}
        member = payload.get("member") or {}
        if author.get("bot"):
            return

        sender_id = str(author.get("id", ""))
        channel_id = str(payload.get("channel_id", ""))
        content = payload.get("content") or ""
        guild_id = payload.get("guild_id")
        username = author.get("username")
        discriminator = author.get("discriminator")
        display_name = member.get("nick") or author.get("global_name") or username
        tag = self._build_author_tag(username, discriminator)
        context_preview = self._build_context_preview(payload)
        recent_messages = self._get_recent_messages(channel_id)
        reply_meta = self._build_reply_metadata(payload.get("referenced_message"))

        if not sender_id or not channel_id:
            return

        if not self.is_allowed(sender_id):
            self._remember_recent_message(channel_id, display_name, tag, context_preview, str(payload.get("id", "")))
            return

        # Check group channel policy (DMs always respond if is_allowed passes)
        if guild_id is not None:
            if not self._should_respond_in_group(payload, content):
                self._remember_recent_message(channel_id, display_name, tag, context_preview, str(payload.get("id", "")))
                return

        content_parts = [content] if content else []
        media_paths: list[str] = []
        media_dir = get_media_dir("discord")

        for attachment in payload.get("attachments") or []:
            url = attachment.get("url")
            filename = attachment.get("filename") or "attachment"
            size = attachment.get("size") or 0
            if not url or not self._http:
                continue
            if size and size > MAX_ATTACHMENT_BYTES:
                content_parts.append(f"[attachment: {filename} - too large]")
                continue
            try:
                media_dir.mkdir(parents=True, exist_ok=True)
                file_path = media_dir / f"{attachment.get('id', 'file')}_{filename.replace('/', '_')}"
                resp = await self._http.get(url)
                resp.raise_for_status()
                file_path.write_bytes(resp.content)
                media_paths.append(str(file_path))
                content_parts.append(f"[attachment: {file_path}]")
            except Exception as e:
                logger.warning("Failed to download Discord attachment: {}", e)
                content_parts.append(f"[attachment: {filename} - download failed]")

        reply_to = (payload.get("referenced_message") or {}).get("id")

        await self._start_typing(channel_id)

        await self._handle_message(
            sender_id=sender_id,
            chat_id=channel_id,
            content="\n".join(p for p in content_parts if p) or "[empty message]",
            media=media_paths,
            metadata={
                "message_id": str(payload.get("id", "")),
                "guild_id": guild_id,
                "reply_to": reply_to,
                "username": username,
                "display_name": display_name,
                "tag": tag,
                "recent_messages": recent_messages,
                **reply_meta,
            },
        )
        self._remember_recent_message(channel_id, display_name, tag, context_preview, str(payload.get("id", "")))

    @staticmethod
    def _build_author_tag(username: str | None, discriminator: str | None) -> str | None:
        """Build a Discord-style author tag from username and discriminator."""
        if not username:
            return None
        if discriminator and discriminator != "0":
            return f"{username}#{discriminator}"
        return f"@{username}"

    @staticmethod
    def _build_context_preview(payload: dict[str, Any]) -> str:
        """Build a text preview suitable for recent-conversation context."""
        parts: list[str] = []
        if content := (payload.get("content") or "").strip():
            parts.append(content)
        for attachment in payload.get("attachments") or []:
            filename = attachment.get("filename") or "attachment"
            parts.append(f"[attachment: {filename}]")
        preview = "\n".join(parts).strip() or "[empty message]"
        return preview[:RECENT_CONTEXT_CHARS]

    @staticmethod
    def _build_reply_metadata(referenced_message: dict[str, Any] | None) -> dict[str, str]:
        """Extract reply target metadata when Discord includes the referenced message."""
        if not isinstance(referenced_message, dict):
            return {}
        author = referenced_message.get("author") or {}
        member = referenced_message.get("member") or {}
        username = author.get("username")
        discriminator = author.get("discriminator")
        display_name = member.get("nick") or author.get("global_name") or username
        tag = DiscordChannel._build_author_tag(username, discriminator)
        metadata: dict[str, str] = {}
        reply_message_id = referenced_message.get("id")
        if isinstance(reply_message_id, str) and reply_message_id.strip():
            metadata["reply_message_id"] = reply_message_id.strip()
        if isinstance(display_name, str) and display_name.strip():
            metadata["reply_display_name"] = display_name.strip()
        if isinstance(tag, str) and tag.strip():
            metadata["reply_tag"] = tag.strip()
        content = DiscordChannel._build_context_preview(referenced_message)
        if content:
            metadata["reply_content"] = content
        return metadata

    def _get_recent_messages(self, channel_id: str) -> list[dict[str, str]]:
        """Return recent non-bot channel messages collected before the current turn."""
        if not channel_id:
            return []
        return list(self._recent_messages.get(channel_id, ()))

    def _remember_recent_message(
        self,
        channel_id: str,
        display_name: str | None,
        tag: str | None,
        content: str,
        message_id: str | None = None,
    ) -> None:
        """Store a short recent-message summary for later turns in the same channel."""
        if not channel_id or not content:
            return
        bucket = self._recent_messages.setdefault(
            channel_id, deque(maxlen=RECENT_CONTEXT_MESSAGES)
        )
        entry: dict[str, str] = {"content": content}
        if isinstance(display_name, str) and display_name.strip():
            entry["display_name"] = display_name.strip()
        if isinstance(tag, str) and tag.strip():
            entry["tag"] = tag.strip()
        if isinstance(message_id, str) and message_id.strip():
            entry["message_id"] = message_id.strip()
        bucket.append(entry)

    def _should_respond_in_group(self, payload: dict[str, Any], content: str) -> bool:
        """Check if bot should respond in a group channel based on policy."""
        if self.config.group_policy == "open":
            return True

        if self.config.group_policy == "mention":
            # Check if bot was mentioned in the message
            if self._bot_user_id:
                # Check mentions array
                mentions = payload.get("mentions") or []
                for mention in mentions:
                    if str(mention.get("id")) == self._bot_user_id:
                        return True
                # Also check content for mention format <@USER_ID>
                if f"<@{self._bot_user_id}>" in content or f"<@!{self._bot_user_id}>" in content:
                    return True
            logger.debug("Discord message in {} ignored (bot not mentioned)", payload.get("channel_id"))
            return False

        return True

    async def _start_typing(self, channel_id: str) -> None:
        """Start periodic typing indicator for a channel."""
        await self._stop_typing(channel_id)

        async def typing_loop() -> None:
            url = f"{DISCORD_API_BASE}/channels/{channel_id}/typing"
            headers = {"Authorization": f"Bot {self.config.token}"}
            while self._running:
                try:
                    await self._http.post(url, headers=headers)
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    logger.debug("Discord typing indicator failed for {}: {}", channel_id, e)
                    return
                await asyncio.sleep(8)

        self._typing_tasks[channel_id] = asyncio.create_task(typing_loop())

    async def _stop_typing(self, channel_id: str) -> None:
        """Stop typing indicator for a channel."""
        task = self._typing_tasks.pop(channel_id, None)
        if task:
            task.cancel()
