"""Tests for bot shutdown and task cancellation commands."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from nanobot.bus.events import InboundMessage


def _make_loop():
    """Create a minimal AgentLoop with mocked dependencies."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    workspace = MagicMock()
    workspace.__truediv__ = MagicMock(return_value=MagicMock())

    with patch("nanobot.agent.loop.ContextBuilder"), \
         patch("nanobot.agent.loop.SessionManager"), \
         patch("nanobot.agent.loop.SubagentManager") as mock_sub_mgr:
        mock_sub_mgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        loop = AgentLoop(bus=bus, provider=provider, workspace=workspace)
    return loop, bus


def test_stop_invokes_shutdown_callback() -> None:
    async def _run() -> None:
        loop, bus = _make_loop()
        stopped = asyncio.Event()

        async def _shutdown() -> None:
            stopped.set()

        loop.shutdown_callback = _shutdown

        msg = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="/stop")
        await loop._handle_shutdown(msg)

        assert stopped.is_set()
        out = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        assert out.content == "Stopping bot..."

    asyncio.run(_run())


def test_stop_without_callback_stops_loop() -> None:
    async def _run() -> None:
        loop, bus = _make_loop()
        loop._running = True

        msg = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="/stop")
        await loop._handle_shutdown(msg)

        assert loop._running is False
        out = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        assert out.content == "Stopping bot..."

    asyncio.run(_run())


def test_help_includes_stop_and_cancel_commands() -> None:
    async def _run() -> None:
        loop, _ = _make_loop()
        msg = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="/help")

        response = await loop._process_message(msg)

        assert response is not None
        assert "/cancel — Stop the current task" in response.content
        assert "/stop — Stop the bot" in response.content

    asyncio.run(_run())
