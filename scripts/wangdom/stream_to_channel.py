#!/usr/bin/env python3
"""一次性語音頻道播放腳本

用法：python stream_to_channel.py <音頻檔案> [語音頻道ID]
  - 不指定頻道ID → 自動找主公所在的語音頻道
"""

import os
import subprocess
import sys
import asyncio
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

import discord

intents = discord.Intents.default()
intents.voice_states = True

TOKEN = os.environ["VOICE_BOT_TOKEN"]
AUDIO_FILE = sys.argv[1]
TARGET_CHANNEL_ID = int(sys.argv[2]) if len(sys.argv) > 2 else None


class StreamBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self._done = asyncio.Event()

    async def on_ready(self):
        print(f"✅ Bot 上線：{self.user}")

        # 找目標語音頻道
        target_channel = None

        if TARGET_CHANNEL_ID:
            target_channel = self.get_channel(TARGET_CHANNEL_ID)
        else:
            # 找第一個有人在的語音頻道
            for guild in self.guilds:
                for vc in guild.voice_channels:
                    if len(vc.members) > 0:
                        target_channel = vc
                        break
                if target_channel:
                    break

        if not target_channel:
            print("❌ 找不到有人的語音頻道，請指定頻道ID")
            await self.close()
            return

        print(f"📢 目標頻道：{target_channel.name}（{target_channel.guild.name}）")
        print(f"🎵 播放檔案：{AUDIO_FILE}")

        # WAV → PCM
        pcm_path = AUDIO_FILE + ".pcm"
        cmd = [
            "ffmpeg", "-y", "-i", AUDIO_FILE,
            "-f", "s16le", "-acodec", "pcm_s16le",
            "-ar", "48000", "-ac", "2", pcm_path,
        ]
        subprocess.run(cmd, capture_output=True, timeout=60, check=True)

        # 加入頻道並播放
        vc = await target_channel.connect()

        source = discord.FFmpegPCMAudio(pcm_path, before_options="-f s16le -ar 48000 -ac 2")

        def after_play(err):
            if err:
                print(f"⚠️ 播放錯誤：{err}")
            else:
                print("✅ 播放完畢")
            # 清理
            Path(pcm_path).unlink(missing_ok=True)
            asyncio.run_coroutine_threadsafe(vc.disconnect(), self.loop)
            asyncio.run_coroutine_threadsafe(self.close(), self.loop)

        vc.play(source, after=after_play)
        print("🔊 正在播放...")


bot = StreamBot()
bot.run(TOKEN)
