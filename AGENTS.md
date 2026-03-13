# Agent Instructions

Use the project virtualenv for Python tooling in this repo.

## Tests

Run tests with the venv interpreter:

```bash
venv/bin/python -m pytest -q
```

Run a focused file or test selection the same way, for example:

```bash
venv/bin/python -m pytest -q tests/test_loop_save_turn.py tests/test_memory_formatting.py tests/test_discord_channel.py
```
