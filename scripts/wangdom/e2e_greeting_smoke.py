#!/usr/bin/env python3
"""Offline E2E smoke for !greeting command (file/stream mode)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discord_voice_bot as botmod  # noqa: E402


class FakeCtx:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text: str, **kwargs):
        self.sent.append(text)


async def run_case(flag: str, expected_mode: str):
    calls: list[tuple[str, str, str]] = []

    botmod._load_tianji_report = lambda: {
        "date": "2026-04-15",
        "weekday": "三",
        "today_festival": [],
        "solar_term": {"current_term": "清明", "next_term": "穀雨", "days_until_next": 5},
        "holiday_check": {"isHoliday": False},
    }
    botmod._router.resolve = lambda _ctx, flag_file=False, flag_stream=False: expected_mode

    async def fake_stream(ctx, character, text, mood=None, ambience="none"):
        calls.append(("stream", character, text))

    async def fake_file(ctx, character, text, mood=None, ambience="none"):
        calls.append(("file", character, text))

    botmod._synthesize_and_play_stream = fake_stream
    botmod._synthesize_and_send_file = fake_file

    ctx = FakeCtx()
    await botmod.cmd_greeting.callback(ctx, args=flag)

    if not calls:
        raise AssertionError("no synth calls were made")
    modes = {m for m, _, _ in calls}
    if modes != {expected_mode}:
        raise AssertionError(f"mode mismatch: got={modes}, expected={expected_mode}")
    if len(calls) != 3:
        raise AssertionError(f"expected 3 greeting lines, got {len(calls)}")
    if not any("節日問候播報完畢" in s for s in ctx.sent):
        raise AssertionError("missing completion message")

    return {
        "flag": flag,
        "mode": expected_mode,
        "line_count": len(calls),
        "first_line": calls[0][2],
    }


async def main():
    file_case = await run_case("-f", "file")
    stream_case = await run_case("-s", "stream")
    print("✅ greeting smoke passed")
    print(file_case)
    print(stream_case)


if __name__ == "__main__":
    asyncio.run(main())
