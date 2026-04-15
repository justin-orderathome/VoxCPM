#!/usr/bin/env python3
"""Offline E2E smoke for !weekly command (file/stream mode)."""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discord_voice_bot as botmod  # noqa: E402


class FakeMsg:
    def __init__(self, content: str = ""):
        self.content = content

    async def edit(self, *, content: str):
        self.content = content


class FakeVoiceClient:
    def __init__(self):
        self._playing = False
        self.play_called = False

    def is_playing(self):
        return self._playing

    def stop(self):
        self._playing = False

    def play(self, source):
        self.play_called = True
        self._playing = True


class FakeCtx:
    def __init__(self):
        self.sent: list[str] = []
        self.voice_client = None

    async def send(self, text: str, **kwargs):
        self.sent.append(text)
        return FakeMsg(text)


async def run_case(flag: str, expected_mode: str):
    with tempfile.TemporaryDirectory(prefix="weekly_smoke_") as td:
        td_path = Path(td)
        report = td_path / "weekly.txt"
        report.write_text(
            "本週完成三項交付，系統穩定。\n\n"
            "里程碑：morning/greeting/drama 上線。\n\n"
            "風險：串流延遲已修補。\n\n"
            "下週：推進 weekly 自動化。\n",
            encoding="utf-8",
        )

        botmod._router.resolve = lambda _ctx, flag_file=False, flag_stream=False: expected_mode
        botmod.OUTPUT_DIR = td_path

        def fake_render(report_text, output_wav, source_path):
            Path(output_wav).write_bytes(b"RIFFfake")
            return {
                "ok": True,
                "output_path": output_wav,
                "source_path": source_path,
                "segments": 4,
                "duration_sec": 18.2,
                "transcript_lines": [
                    {"speaker": "軍師·諸葛亮", "text": "本週總覽：完成三項交付。"},
                    {"speaker": "丞相·曾國藩", "text": "里程碑進展：三項功能上線。"},
                ],
                "transcript_timeline": [
                    {"speaker": "軍師·諸葛亮", "text": "本週總覽：完成三項交付。", "start_sec": 0.0},
                    {"speaker": "丞相·曾國藩", "text": "里程碑進展：三項功能上線。", "start_sec": 1.1},
                ],
            }

        def fake_convert(wav, mp3):
            Path(mp3).write_bytes(b"ID3fake")
            return True, ""

        botmod._render_weekly_audio = fake_render
        botmod._convert_wav_to_mp3 = fake_convert
        botmod.discord.FFmpegPCMAudio = lambda *a, **k: object()

        timed_called = {"ok": False}

        async def fake_send_timed(ctx, timeline, speed=1.0, lead_seconds=0.55):
            timed_called["ok"] = True
            await ctx.send("**丞相·曾國藩**：里程碑進展：三項功能上線。")

        botmod._send_drama_transcript_timed = fake_send_timed

        vc = FakeVoiceClient()

        async def fake_ensure_voice_client(_ctx):
            return vc

        botmod._ensure_voice_client = fake_ensure_voice_client

        ctx = FakeCtx()
        await botmod.cmd_weekly.callback(ctx, args=f"{flag} {report}")
        await asyncio.sleep(0)

        if expected_mode == "file":
            if not any("有聲週報語音檔" in s for s in ctx.sent):
                raise AssertionError("file mode missing attachment message")
        else:
            if not vc.play_called:
                raise AssertionError("stream mode did not call voice_client.play")
            if not timed_called["ok"]:
                raise AssertionError("stream mode did not trigger timed transcript")

        if not any("有聲週報摘要" in s for s in ctx.sent):
            raise AssertionError("missing weekly summary")
        if not any("週報文字同步" in s for s in ctx.sent):
            raise AssertionError("missing weekly transcript")

        return {"flag": flag, "mode": expected_mode, "messages": len(ctx.sent)}


async def main():
    file_case = await run_case("-f", "file")
    stream_case = await run_case("-s", "stream")
    print("✅ weekly smoke passed")
    print(file_case)
    print(stream_case)


if __name__ == "__main__":
    asyncio.run(main())
