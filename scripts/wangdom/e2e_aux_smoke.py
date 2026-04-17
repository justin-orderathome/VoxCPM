#!/usr/bin/env python3
"""Auxiliary offline smoke for mode/error/status commands."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discord_voice_bot as botmod  # noqa: E402


class FakeCtx:
    def __init__(self):
        self.sent: list[str] = []
        self.voice_client = None
        self.guild = None

    async def send(self, text: str, **kwargs):
        self.sent.append(text)
        class _M:
            async def edit(self, *, content: str):
                pass
        return _M()


async def main():
    ctx = FakeCtx()

    # mode switch
    await botmod.cmd_mode.callback(ctx, mode="stream")
    await botmod.cmd_mode.callback(ctx, mode="file")
    await botmod.cmd_mode.callback(ctx, mode="auto")

    # error paths
    await botmod.cmd_play.callback(ctx, args="/not/exist.wav")
    await botmod.cmd_drama.callback(ctx, args="-f /not/exist.txt")
    await botmod.cmd_weekly.callback(ctx, args="-f /not/exist.txt")
    await botmod.cmd_stop.callback(ctx, task_id="")

    # info commands
    await botmod.cmd_voices.callback(ctx)
    await botmod.cmd_status.callback(ctx)

    checks = {
        "mode_switch": any("輸出模式已切換" in s for s in ctx.sent),
        "play_missing": any("檔案不存在" in s for s in ctx.sent),
        "drama_missing": any("劇本檔案不存在" in s for s in ctx.sent),
        "weekly_missing": any("週報檔案不存在" in s for s in ctx.sent),
        "stop_idle": any("目前沒有在播放" in s for s in ctx.sent),
        "voices": any("可用角色列表" in s for s in ctx.sent),
        "status": any("王朝語音 Bot 狀態" in s for s in ctx.sent),
    }

    bad = [k for k, v in checks.items() if not v]
    if bad:
        raise SystemExit(f"❌ aux smoke failed: {bad}")

    print("✅ aux smoke passed")
    print(checks)


if __name__ == "__main__":
    asyncio.run(main())
