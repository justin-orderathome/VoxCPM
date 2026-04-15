#!/usr/bin/env python3
"""Offline E2E smoke for !drama command (file/stream mode)."""

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
    with tempfile.TemporaryDirectory(prefix="drama_smoke_") as td:
        td_path = Path(td)
        script = td_path / "scene.txt"
        script.write_text("軍師·諸葛亮：主公萬安\n待詔·唐伯虎：今日戲文已備妥\n", encoding="utf-8")

        out_wav = td_path / "drama.wav"
        out_wav.write_bytes(b"RIFFfake")
        out_mp3 = td_path / "drama.mp3"

        botmod._router.resolve = lambda _ctx, flag_file=False, flag_stream=False: expected_mode
        botmod.OUTPUT_DIR = td_path

        def fake_render(script_path, output_wav, max_segments=30):
            Path(output_wav).write_bytes(b"RIFFfake")
            return {
                "ok": True,
                "output_path": output_wav,
                "script_path": script_path,
                "segments": 2,
                "duration_sec": 12.5,
                "preview": [
                    {"speaker": "軍師·諸葛亮", "text": "主公萬安"},
                    {"speaker": "待詔·唐伯虎", "text": "今日戲文已備妥"},
                ],
                "transcript_lines": [
                    {"speaker": "軍師·諸葛亮", "text": "主公萬安"},
                    {"speaker": "待詔·唐伯虎", "text": "今日戲文已備妥"},
                ],
                "transcript_timeline": [
                    {"speaker": "軍師·諸葛亮", "text": "主公萬安", "start_sec": 0.0},
                    {"speaker": "待詔·唐伯虎", "text": "今日戲文已備妥", "start_sec": 1.2},
                ],
            }

        def fake_convert(wav, mp3):
            Path(mp3).write_bytes(b"ID3fake")
            return True, ""

        botmod._render_drama_audio = fake_render
        botmod._convert_wav_to_mp3 = fake_convert
        botmod.discord.FFmpegPCMAudio = lambda *a, **k: object()

        timed_called = {"ok": False}

        async def fake_send_timed(ctx, timeline, speed=1.0):
            timed_called["ok"] = True
            await ctx.send("📝 **廣播劇文字同步（逐句）**")

        botmod._send_drama_transcript_timed = fake_send_timed

        vc = FakeVoiceClient()

        async def fake_ensure_voice_client(_ctx):
            return vc

        botmod._ensure_voice_client = fake_ensure_voice_client

        ctx = FakeCtx()
        await botmod.cmd_drama.callback(ctx, args=f"{flag} {script}")
        await asyncio.sleep(0)

        if expected_mode == "file":
            if not any("廣播劇語音檔" in s for s in ctx.sent):
                raise AssertionError("file mode missing attachment message")
        else:
            if not vc.play_called:
                raise AssertionError("stream mode did not call voice_client.play")
            if not timed_called["ok"]:
                raise AssertionError("stream mode did not trigger timed transcript")

        if not any("廣播劇摘要" in s for s in ctx.sent):
            raise AssertionError("missing summary text")
        if not any("廣播劇文字同步" in s for s in ctx.sent):
            raise AssertionError("missing transcript sync text")

        return {
            "flag": flag,
            "mode": expected_mode,
            "messages": len(ctx.sent),
        }


async def main():
    file_case = await run_case("-f", "file")
    stream_case = await run_case("-s", "stream")
    print("✅ drama smoke passed")
    print(file_case)
    print(stream_case)


if __name__ == "__main__":
    asyncio.run(main())
