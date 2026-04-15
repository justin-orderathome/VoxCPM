#!/usr/bin/env python3
"""Offline E2E smoke for !morning command (file/stream mode).

This script does not hit Discord APIs. It validates command flow by mocking ctx and
internal synth functions.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import discord_voice_bot as botmod  # noqa: E402


class FakeCtx:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text: str, **kwargs):
        self.sent.append(text)


async def run_case(flag: str, expected_mode: str):
    calls: list[tuple[str, str, str]] = []

    # stable deterministic report
    botmod._load_tianji_report = lambda: {
        "date": "2026-04-15",
        "weekday": "三",
        "lunar_date": {"lunar_display": "丙午年二月廿八"},
        "solar_term": {"current_term": "清明", "next_term": "穀雨", "days_until_next": 5},
        "holiday_check": {"isHoliday": False},
        "weather": {"brief": "晴時多雲，22-28度"},
        "todo_status": {"summary": {"pending": 2, "delayed": 0, "suspected_delay": 1}},
    }

    # force mode routing to isolate branch
    botmod._router.resolve = lambda _ctx, flag_file=False, flag_stream=False: expected_mode

    async def fake_stream(ctx, character, text, mood=None, ambience="none"):
        calls.append(("stream", character, text))

    async def fake_file(ctx, character, text, mood=None, ambience="none"):
        calls.append(("file", character, text))

    botmod._synthesize_and_play_stream = fake_stream
    botmod._synthesize_and_send_file = fake_file

    ctx = FakeCtx()
    # command decorator returns Command object; actual function is .callback
    await botmod.cmd_morning.callback(ctx, args=flag)

    if not calls:
        raise AssertionError("no synth calls were made")

    modes = {m for m, _, _ in calls}
    if modes != {expected_mode}:
        raise AssertionError(f"mode mismatch: got={modes}, expected={expected_mode}")

    if len(calls) != 5:
        raise AssertionError(f"expected 5 morning lines, got {len(calls)}")

    if not any("早朝語音播報完畢" in s for s in ctx.sent):
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
    print("✅ morning smoke passed")
    print(file_case)
    print(stream_case)


if __name__ == "__main__":
    asyncio.run(main())
