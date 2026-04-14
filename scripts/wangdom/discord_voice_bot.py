#!/usr/bin/env python3
"""崴勝王朝 · 獨立語音 Bot

功能：
  - 連接 Discord 語音頻道，即時播放角色語音
  - 雙模式輸出：串流（語音頻道） / 檔案（文字頻道語音訊息）
  - 所有語音輸出同步發送文字版（耳機沒電也不漏訊）
  - 整合 voxcpm_skill.py（角色音色 + 情緒 + BGM 音場）

指令：
  !join              — 加入主公所在語音頻道
  !leave             — 離開語音頻道
  !say <角色> <台詞> — 即時生成並播放（預設串流，-f 走檔案）
  !play <檔案>       — 播放既有音頻檔（預設串流，-f 走檔案）
  !stop              — 停止目前播放
  !morning           — 播放當日早朝（預設串流，-f 走檔案）
  !greeting          — 播放節日問候（預設串流，-f 走檔案）
  !drama <劇本>      — 多人廣播劇（預設串流，-f 走檔案）
  !weekly            — 播放有聲週報（預設串流，-f 走檔案）
  !mode <auto|stream|file> — 切換全域輸出模式
  !voices            — 列出可用角色
  !status            — 顯示 Bot 狀態

環境變數：
  VOICE_BOT_TOKEN    — Discord Bot Token（必須）
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import discord
from discord import FFmpegPCMAudio, VoiceClient, TextChannel
from discord.ext import commands

# ---------------------------------------------------------------------------
# 路徑設定
# ---------------------------------------------------------------------------
BOT_DIR = Path(__file__).resolve().parent          # scripts/wangdom/
PROJECT_DIR = BOT_DIR.parent.parent                 # ~/projects/VoxCPM/
PROFILES_PATH = PROJECT_DIR / "profiles" / "voice_profiles.yaml"
OUTPUT_DIR = PROJECT_DIR / "test_output" / "voice_bot"

# 將 wangdom 目錄加入 path（方便 import 兄弟模組）
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from voxcpm_skill import VoxCpmSkill, VoiceProfileManager


# ---------------------------------------------------------------------------
# 輸出路由器
# ---------------------------------------------------------------------------
class OutputMode:
    STREAM = "stream"
    FILE = "file"
    AUTO = "auto"


class OutputRouter:
    """決定語音輸出走向：語音頻道串流 vs 文字頻道檔案"""

    def __init__(self, default_mode: str = OutputMode.AUTO):
        self.default_mode = default_mode

    def resolve(
        self,
        ctx: commands.Context,
        flag_file: bool = False,
        flag_stream: bool = False,
    ) -> str:
        """解析最終輸出模式

        優先級：-f / -s flag > 全域設定 > auto 偵測
        """
        if flag_file:
            return OutputMode.FILE
        if flag_stream:
            return OutputMode.STREAM

        mode = self.default_mode
        if mode == OutputMode.AUTO:
            # 主公在語音頻道 → 串流；不在 → 檔案
            if ctx.author.voice and ctx.author.voice.channel:
                return OutputMode.STREAM
            return OutputMode.FILE
        return mode


# ---------------------------------------------------------------------------
# 音頻管線工具
# ---------------------------------------------------------------------------
class AudioPipeline:
    """WAV → PCM s16le 48kHz stereo → Discord AudioSource"""

    DISCORD_SAMPLE_RATE = 48000
    DISCORD_CHANNELS = 2

    @staticmethod
    def wav_to_pcm(wav_path: str | Path) -> str:
        """ffmpeg 轉碼：WAV → PCM s16le 48kHz stereo（Discord 規格）"""
        pcm_path = str(wav_path) + ".pcm"
        cmd = [
            "ffmpeg", "-y", "-i", str(wav_path),
            "-f", "s16le",
            "-acodec", "pcm_s16le",
            "-ar", str(AudioPipeline.DISCORD_SAMPLE_RATE),
            "-ac", str(AudioPipeline.DISCORD_CHANNELS),
            pcm_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg PCM 轉碼失敗：{result.stderr[:300]}")
        return pcm_path

    @staticmethod
    def make_audio_source(pcm_path: str) -> FFmpegPCMAudio:
        """從 PCM 檔建立 Discord AudioSource"""
        return FFmpegPCMAudio(
            pcm_path,
            before_options="-f s16le -ar 48000 -ac 2",
        )

    @staticmethod
    def wav_to_mp3_buffer(wav_path: str | Path) -> discord.File:
        """WAV → MP3 轉檔，回傳 discord.File（用於檔案模式推送）"""
        mp3_path = str(wav_path).rsplit(".", 1)[0] + "_send.mp3"
        cmd = [
            "ffmpeg", "-y", "-i", str(wav_path),
            "-codec:a", "libmp3lame", "-b:a", "128k",
            mp3_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            # fallback：直接發 WAV
            return discord.File(str(wav_path), filename=Path(wav_path).name)
        return discord.File(mp3_path, filename=Path(mp3_path).name)


# ---------------------------------------------------------------------------
# Bot 主體
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# 全域狀態
_skill: Optional[VoxCpmSkill] = None
_router = OutputRouter(default_mode=OutputMode.AUTO)
_pipeline = AudioPipeline()


def _get_skill() -> VoxCpmSkill:
    """延遲載入 VoxCPM 模型（首次指令時才載入，避免啟動慢）"""
    global _skill
    if _skill is None:
        print("⏳ 載入 VoxCPM 模型（首次需 ~22s）...")
        _skill = VoxCpmSkill(profiles_path=str(PROFILES_PATH))
        print("✅ VoxCPM 模型已載入")
    return _skill


def _parse_args(text: str) -> dict:
    """解析指令參數：角色 + 台詞 + flags

    格式：[-f] [-s] [--mood MOOD] [--ambience AMBIENCE] <角色> <台詞...>
    """
    parts = text.strip().split()
    flags = {"file": False, "stream": False}
    named = {"mood": None, "ambience": "none"}
    positional = []

    i = 0
    while i < len(parts):
        p = parts[i]
        if p == "-f":
            flags["file"] = True
        elif p == "-s":
            flags["stream"] = True
        elif p == "--mood" and i + 1 < len(parts):
            named["mood"] = parts[i + 1]
            i += 1
        elif p == "--ambience" and i + 1 < len(parts):
            named["ambience"] = parts[i + 1]
            i += 1
        else:
            positional.append(p)
        i += 1

    character = positional[0] if len(positional) > 0 else ""
    line = " ".join(positional[1:]) if len(positional) > 1 else ""

    return {**flags, **named, "character": character, "text": line}


async def _send_text_sync(ctx: commands.Context, text: str, character: str):
    """所有語音輸出都同步發文字版"""
    await ctx.send(f"**{character}**：{text}")


async def _synthesize_and_play_stream(
    ctx: commands.Context,
    skill: VoxCpmSkill,
    character: str,
    text: str,
    mood: str = None,
    ambience: str = "none",
):
    """串流模式：語音頻道播放 + 文字頻道同步"""
    # 1. 檢查是否在語音頻道
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("⚠️ 主公不在語音頻道，請先加入語音頻道或使用 `-f` 走檔案模式")
        return

    # 2. Bot 加入語音頻道
    vc: VoiceClient = ctx.voice_client
    if vc is None:
        vc = await ctx.author.voice.channel.connect()
    elif vc.channel != ctx.author.voice.channel:
        await vc.move_to(ctx.author.voice.channel)

    # 3. 生成語音
    status_msg = await ctx.send(f"🎵 {character} 正在生成語音...")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time() * 1000)
    wav_path = OUTPUT_DIR / f"say_{ts}.wav"

    result = skill.synthesize(
        text=text,
        character=character,
        output_path=str(wav_path),
        mood=mood,
        ambience_profile=ambience,
        output_format="wav",  # 串流用 WAV，不轉 MP3
    )

    if not result.get("ok"):
        await status_msg.edit(content=f"❌ 語音生成失敗：{result.get('reason', '未知錯誤')}")
        return

    actual_wav = result["output_path"]

    # 4. 同步發文字
    await _send_text_sync(ctx, text, character)

    # 5. WAV → PCM → 播放
    try:
        pcm_path = _pipeline.wav_to_pcm(actual_wav)
        source = _pipeline.make_audio_source(pcm_path)

        def _after_play(err):
            # 播放完清理暫存
            for p in [actual_wav, pcm_path]:
                try:
                    Path(p).unlink(missing_ok=True)
                except Exception:
                    pass
            if err:
                print(f"⚠️ 播放錯誤：{err}")

        vc.play(source, after=_after_play)
        await status_msg.edit(content=f"🔊 {character} 正在播放...")
    except Exception as e:
        await status_msg.edit(content=f"❌ 播放失敗：{e}")


async def _synthesize_and_send_file(
    ctx: commands.Context,
    skill: VoxCpmSkill,
    character: str,
    text: str,
    mood: str = None,
    ambience: str = "none",
):
    """檔案模式：生成 MP3 發文字頻道 + 文字版"""
    status_msg = await ctx.send(f"🎵 {character} 正在生成語音...")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time() * 1000)
    out_path = OUTPUT_DIR / f"say_{ts}.mp3"

    result = skill.synthesize(
        text=text,
        character=character,
        output_path=str(out_path),
        mood=mood,
        ambience_profile=ambience,
        output_format="mp3",
    )

    if not result.get("ok"):
        await status_msg.edit(content=f"❌ 語音生成失敗：{result.get('reason', '未知錯誤')}")
        return

    actual_path = result["output_path"]
    audio_file = _pipeline.wav_to_mp3_buffer(actual_path)

    # 發送文字 + 語音
    await ctx.send(f"**{character}**：{text}", file=audio_file)
    await status_msg.delete()

    # 清理
    try:
        Path(actual_path).unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Bot 事件
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    print(f"✅ 王朝語音 Bot 已上線：{bot.user}")
    print(f"   伺服器：{[g.name for g in bot.guilds]}")
    print(f"   輸出模式：{_router.default_mode}")


# ---------------------------------------------------------------------------
# 指令
# ---------------------------------------------------------------------------
@bot.command(name="join")
async def cmd_join(ctx: commands.Context):
    """加入主公所在語音頻道"""
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("⚠️ 主公不在任何語音頻道")
        return
    channel = ctx.author.voice.channel
    if ctx.voice_client is not None:
        await ctx.voice_client.move_to(channel)
    else:
        await channel.connect()
    await ctx.send(f"✅ 已加入 **{channel.name}**")


@bot.command(name="leave")
async def cmd_leave(ctx: commands.Context):
    """離開語音頻道"""
    if ctx.voice_client is not None:
        await ctx.voice_client.disconnect()
        await ctx.send("👋 已離開語音頻道")
    else:
        await ctx.send("⚠️ 不在任何語音頻道")


@bot.command(name="say")
async def cmd_say(ctx: commands.Context, *, args: str = ""):
    """生成角色語音並播放/推送

    用法：
      !say 諸葛亮 稟主公
      !say -f 諸葛亮 稟主公        (檔案模式)
      !say -s 諸葛亮 稟主公        (串流模式)
      !say --mood 急切 魏徵 稟主公 (帶情緒)
      !say --ambience hall 劉邦 退朝 (帶音場)
    """
    if not args:
        await ctx.send("用法：`!say [-f|-s] [--mood 情緒] [--ambience 音場] <角色> <台詞>`")
        return

    parsed = _parse_args(args)
    character = parsed["character"]
    text = parsed["text"]

    if not character or not text:
        await ctx.send("⚠️ 請指定角色和台詞，例如：`!say 諸葛亮 稟主公`")
        return

    skill = _get_skill()

    # 解析輸出模式
    mode = _router.resolve(ctx, flag_file=parsed["file"], flag_stream=parsed["stream"])

    if mode == OutputMode.STREAM:
        await _synthesize_and_play_stream(
            ctx, skill, character, text,
            mood=parsed["mood"], ambience=parsed["ambience"],
        )
    else:
        await _synthesize_and_send_file(
            ctx, skill, character, text,
            mood=parsed["mood"], ambience=parsed["ambience"],
        )


@bot.command(name="play")
async def cmd_play(ctx: commands.Context, *, args: str = ""):
    """播放既有音頻檔

    用法：
      !play test_output/morning_court.mp3     (串流)
      !play -f test_output/morning_court.mp3  (檔案)
    """
    if not args:
        await ctx.send("用法：`!play [-f|-s] <檔案路徑>`")
        return

    parts = args.strip().split()
    flag_file = "-f" in parts
    flag_stream = "-s" in parts
    file_path = " ".join(p for p in parts if p not in {"-f", "-s"})

    if not Path(file_path).exists():
        await ctx.send(f"⚠️ 檔案不存在：`{file_path}`")
        return

    mode = _router.resolve(ctx, flag_file=flag_file, flag_stream=flag_stream)

    if mode == OutputMode.STREAM:
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("⚠️ 主公不在語音頻道，請先加入或使用 `-f`")
            return
        vc: VoiceClient = ctx.voice_client
        if vc is None:
            vc = await ctx.author.voice.channel.connect()
        elif vc.channel != ctx.author.voice.channel:
            await vc.move_to(ctx.author.voice.channel)

        pcm_path = _pipeline.wav_to_pcm(file_path)
        source = _pipeline.make_audio_source(pcm_path)
        vc.play(source)
        await ctx.send(f"🔊 正在播放：`{Path(file_path).name}`")
    else:
        audio_file = discord.File(file_path, filename=Path(file_path).name)
        await ctx.send(f"📎 語音檔案：`{Path(file_path).name}`", file=audio_file)


@bot.command(name="stop")
async def cmd_stop(ctx: commands.Context):
    """停止目前播放"""
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("⏹ 已停止播放")
    else:
        await ctx.send("⚠️ 目前沒有在播放")


@bot.command(name="mode")
async def cmd_mode(ctx: commands.Context, mode: str = ""):
    """切換全域輸出模式

    用法：
      !mode auto    — 智慧切換（在語音頻道→串流，不在→檔案）
      !mode stream  — 強制串流
      !mode file    — 強制檔案
    """
    valid = {OutputMode.AUTO, OutputMode.STREAM, OutputMode.FILE}
    if mode.lower() not in valid:
        await ctx.send("用法：`!mode auto|stream|file`\n目前模式：`{_router.default_mode}`")
        return
    _router.default_mode = mode.lower()
    labels = {OutputMode.AUTO: "智慧切換", OutputMode.STREAM: "串流", OutputMode.FILE: "檔案"}
    await ctx.send(f"✅ 輸出模式已切換為 **{labels[mode.lower()]}**")


@bot.command(name="voices")
async def cmd_voices(ctx: commands.Context):
    """列出可用角色"""
    mgr = VoiceProfileManager(PROFILES_PATH)
    profiles = mgr._profiles
    if not profiles:
        await ctx.send("⚠️ 沒有載入到任何角色設定")
        return

    lines = ["📜 **可用角色列表（17位）**："]
    for i, (name, p) in enumerate(profiles.items(), 1):
        mode_icon = "🎤" if p.mode == "clone" else "🔊"
        lines.append(f"  {i:02d}. {mode_icon} **{name}**（{p.mode}）")

    lines.append("\n用法：`!say <角色名> <台詞>`")
    text = "\n".join(lines)

    # Discord 訊息長度限制 2000
    if len(text) > 1900:
        # 分兩段
        mid = len(profiles) // 2
        first_half = list(profiles.items())[:mid]
        second_half = list(profiles.items())[mid:]
        t1 = "📜 **可用角色（1/2）**：\n" + "\n".join(
            f"  {i+1:02d}. 🎤 **{n}**" for i, (n, _) in enumerate(first_half)
        )
        t2 = "📜 **可用角色（2/2）**：\n" + "\n".join(
            f"  {i+mid+1:02d}. 🎤 **{n}**" for i, (n, _) in enumerate(second_half)
        )
        await ctx.send(t1)
        await ctx.send(t2)
    else:
        await ctx.send(text)


@bot.command(name="status")
async def cmd_status(ctx: commands.Context):
    """顯示 Bot 狀態"""
    lines = [
        "**🏥 王朝語音 Bot 狀態**",
        f"  Bot：`{bot.user}`",
        f"  語音頻道：`{ctx.voice_client.channel.name if ctx.voice_client else '未連接'}`",
        f"  播放中：`{'是' if ctx.voice_client and ctx.voice_client.is_playing() else '否'}`",
        f"  輸出模式：`{_router.default_mode}`",
        f"  VoxCPM 模型：`{'已載入' if _skill else '未載入（首次 !say 時載入）'}`",
    ]

    # VRAM 資訊
    try:
        import torch
        if torch.cuda.is_available():
            free, total = torch.cuda.mem_get_info()
            used_gb = (total - free) / (1024**3)
            total_gb = total / (1024**3)
            lines.append(f"  VRAM：`{used_gb:.1f} / {total_gb:.1f} GB`")
    except Exception:
        pass

    await ctx.send("\n".join(lines))


# ---------------------------------------------------------------------------
# Phase 2 預留指令（框架先建，功能後補）
# ---------------------------------------------------------------------------
@bot.command(name="morning")
async def cmd_morning(ctx: commands.Context, *, args: str = ""):
    """播放當日早朝

    用法：!morning / !morning -f
    """
    # 解析 flag
    flag_file = "-f" in args.split()
    flag_stream = "-s" in args.split()
    mode = _router.resolve(ctx, flag_file=flag_file, flag_stream=flag_stream)

    await ctx.send("⏳ 早朝語音生成中...（功能開發中，敬請期待）")
    # TODO: 整合 tianji-report.py → 生成語音 → 播放/推送


@bot.command(name="greeting")
async def cmd_greeting(ctx: commands.Context, *, args: str = ""):
    """播放節日問候

    用法：!greeting / !greeting -f
    """
    flag_file = "-f" in args.split()
    flag_stream = "-s" in args.split()
    mode = _router.resolve(ctx, flag_file=flag_file, flag_stream=flag_stream)

    await ctx.send("⏳ 節日問候生成中...（功能開發中，敬請期待）")
    # TODO: 整合司天監節慶判定 → 生成問候語音 → 播放/推送


@bot.command(name="drama")
async def cmd_drama(ctx: commands.Context, *, args: str = ""):
    """多人廣播劇

    用法：!drama 劇本.txt / !drama -f 劇本.txt
    """
    flag_file = "-f" in args.split()
    flag_stream = "-s" in args.split()
    file_path = " ".join(p for p in args.split() if p not in {"-f", "-s"})

    if not file_path:
        await ctx.send("用法：`!drama [-f|-s] <劇本路徑>`")
        return

    await ctx.send("⏳ 廣播劇生成中...（功能開發中，敬請期待）")
    # TODO: 整合 dialogue_parser + merger → 多人語音 → 播放/推送


@bot.command(name="weekly")
async def cmd_weekly(ctx: commands.Context, *, args: str = ""):
    """播放有聲週報

    用法：!weekly / !weekly -f
    """
    flag_file = "-f" in args.split()
    flag_stream = "-s" in args.split()
    mode = _router.resolve(ctx, flag_file=flag_file, flag_stream=flag_stream)

    await ctx.send("⏳ 有聲週報生成中...（功能開發中，敬請期待）")
    # TODO: 彙整 Vault 週變更 → 生成語音 → 播放/推送


# ---------------------------------------------------------------------------
# 啟動
# ---------------------------------------------------------------------------
def main():
    token = os.environ.get("VOICE_BOT_TOKEN")
    if not token:
        print("❌ 請設定環境變數 VOICE_BOT_TOKEN")
        print("   export VOICE_BOT_TOKEN='your-bot-token-here'")
        sys.exit(1)

    print("🚀 崴勝王朝語音 Bot 啟動中...")
    bot.run(token)


if __name__ == "__main__":
    main()
