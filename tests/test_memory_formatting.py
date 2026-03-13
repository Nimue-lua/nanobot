from nanobot.agent.memory import MemoryStore


def test_format_messages_includes_user_display_name_and_tag() -> None:
    formatted = MemoryStore._format_messages(
        [
            {
                "role": "user",
                "content": "hello",
                "timestamp": "2026-03-13T09:38:43.285631",
                "display_name": "Alice Server",
                "tag": "@alice",
            }
        ]
    )

    assert "USER Alice Server (@alice): hello" in formatted
