#!/usr/bin/env python3
"""串流脫口秀測試 — VoxCPM 常駐模型 + Discord 串流推播

架構：
  - VoxCPM 模型常駐 GPU（只載入一次）
  - generate_streaming() 在背景 thread 中逐 chunk 生成
  - 每個 chunk 即時轉為 PCM → 放入 AudioSource buffer
  - Discord voice client 每 20ms 消費一個 frame

使用方式：
  1. 先停掉舊的 discord_voice_bot.py
  2. 執行：python streaming_talkshow_test.py
  3. Bot 上線後，在語音頻道打 !streamtest
"""

from __future__ import annotations

import argparse
import asyncio
import os
import struct
import sys
import threading
import time
from pathlib import Path

import discord
from discord.ext import commands
from typing import Optional

# 專案路徑
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent

# .env 載入
from dotenv import load_dotenv
load_dotenv(PROJECT_DIR / ".env")

import numpy as np

# ---------------------------------------------------------------------------
# 脫口秀台詞
# ---------------------------------------------------------------------------
TALKSHOW_SEGMENTS = [
    {
        "character": "待詔·唐伯虎",
        "text": "各位觀眾晚安！歡迎來到「王朝夜總會」加長版！我是主持人待詔唐伯虎，旁邊這位是特別來賓，禮部尚書紀曉嵐！今晚我們要大聊特聊！",
    },
    {
        "character": "禮部·紀曉嵐",
        "text": "伯虎，你這「加長版」三個字，聽著就像我編四庫全書的時候，皇帝突然說「紀愛卿，再補個續編吧」。",
    },
    {
        "character": "待詔·唐伯虎",
        "text": "哈哈哈！曉嵐兄辛苦了。不過說真的，咱們王朝最近可是熱鬧非凡。你們知道工部李冰大人最近搞了什麼嗎？他把整個王朝的基礎建設都容器化了！",
    },
    {
        "character": "禮部·紀曉嵐",
        "text": "容器化？聽起來像是把磚頭裝進箱子裡。不過我聽說確實厲害，以前部署一個服務要三天，現在三分鐘就搞定。工部的人現在天天準時下班。",
    },
    {
        "character": "待詔·唐伯虎",
        "text": "說到下班，你們聽過兵部戚繼光將軍的資安演練嗎？他上週搞了一個紅隊演習，結果把自己人全駭了一遍。連丞相曾國藩的帳密都被破了！",
    },
    {
        "character": "禮部·紀曉嵐",
        "text": "這事我聽說了。曾國藩大人氣得在政事堂拍桌子，說「本相的密碼用的是孫子兵法，怎麼可能被破！」結果戚將軍說，密碼就是「孫子兵法」四個字。",
    },
    {
        "character": "待詔·唐伯虎",
        "text": "哈哈哈哈！太經典了！對了，說到軍師諸葛亮，你們知道他最近發明了什麼嗎？一個叫「暗衛」的系統！說是可以暗中派任務，神不知鬼不覺。",
    },
    {
        "character": "禮部·紀曉嵐",
        "text": "那個我見過。刑部狄仁傑大人操盤的。上次主公問「暗衛去查一下隔壁工作室的報價」，三秒鐘結果就回來了。比六部尚書開會還快。",
    },
    {
        "character": "待詔·唐伯虎",
        "text": "三秒鐘！我畫一幅畫要三個時辰呢！不過話說回來，鴻臚寺蘇秦大人的市場策略才叫厲害。他把咱們王朝的品牌定位成「AI時代的文武百官」，結果訂單接到手軟。",
    },
    {
        "character": "禮部·紀曉嵐",
        "text": "蘇秦那張嘴，合縱連橫的功夫用在行銷上，確實是降維打擊。不過我倒覺得戶部范蠡才是真厲害，預算控管到每一文錢都有去處。",
    },
    {
        "character": "待詔·唐伯虎",
        "text": "范蠡大人說了，什麼投資回報率、什麼成本效益分析，翻譯成白話就是「花一分錢要賺三分回來」。難怪他以前能把西施送到吳國，還能全身而退。",
    },
    {
        "character": "禮部·紀曉嵐",
        "text": "伯虎，你這段子要是被范蠡聽到了，你的繪畫預算恐怕就要被砍了。不過說真的，咱們王朝的語音系統才是今晚的主題。",
    },
    {
        "character": "待詔·唐伯虎",
        "text": "沒錯！你們現在聽到的就是最新的串流語音技術！以前每說一句話就要等半天載入模型，現在是即說即播，跟真人一樣！",
    },
    {
        "character": "禮部·紀曉嵐",
        "text": "即說即播倒是真的。不過唐伯虎的聲音怎麼比我還帥？這個 voice cloning 的參考音檔，到底是誰配的？",
    },
    {
        "character": "待詔·唐伯虎",
        "text": "曉嵐兄，這個嘛，商業機密！總之，今晚的王朝夜總會到此結束！感謝各位觀眾收聽，我們下次再見！晚安！",
    },
]


# ---------------------------------------------------------------------------
# VoxCPM 常駐引擎
# ---------------------------------------------------------------------------
class VoxCpmEngine:
    """常駐 VoxCPM 模型引擎，支援串流生成"""

    def __init__(self, profiles_path: str | Path):
        self.profiles_path = Path(profiles_path)
        self.model = None
        self._profiles = self._load_profiles()

    def _load_profiles(self) -> dict:
        import yaml
        with open(self.profiles_path) as f:
            data = yaml.safe_load(f)
        return data.get("voice_profiles", {})

    def load_model(self):
        """載入模型到 GPU（只做一次）"""
        if self.model is not None:
            print("📦 模型已載入，跳過", file=sys.stderr)
            return
        print("🚀 載入 VoxCPM 模型...", file=sys.stderr)
        t0 = time.time()
        from voxcpm import VoxCPM
        self.model = VoxCPM.from_pretrained("openbmb/VoxCPM2", load_denoiser=False, optimize=False)
        elapsed = time.time() - t0
        print(f"✅ 模型載入完成（{elapsed:.1f}s）", file=sys.stderr)

        # 輸出模型資訊
        tts = self.model.tts_model
        sr = tts.sample_rate
        out_sr = getattr(tts.audio_vae, 'out_sample_rate', sr)
        ps = tts.patch_size
        dcs = tts._decode_chunk_size
        dpl = ps * dcs
        print(f"   sample_rate={sr}, out_sample_rate={out_sr}", file=sys.stderr)
        print(f"   patch_size={ps}, decode_chunk_size={dcs}", file=sys.stderr)
        print(f"   每個 streaming chunk ≈ {dpl} samples @ {out_sr}Hz = {dpl/out_sr*1000:.0f}ms", file=sys.stderr)

    @property
    def out_sample_rate(self) -> int:
        if self.model is None:
            return 48000
        return int(getattr(self.model.tts_model.audio_vae, 'out_sample_rate', 48000))

    def synthesize_streaming(self, text: str, character: str):
        """串流生成語音，yield 每個 chunk（float32 numpy 1D array @ out_sample_rate）"""
        if self.model is None:
            raise RuntimeError("模型未載入")

        profile = self._profiles.get(character)
        if not profile:
            raise ValueError(f"找不到角色：{character}")

        kwargs = {
            "text": text,
            "cfg_value": 2.0,
            "inference_timesteps": 10,
        }

        # clone 模式
        if profile.get("mode") in ("clone", "ultimate_clone") and profile.get("reference"):
            ref_path = profile["reference"]
            if not Path(ref_path).is_absolute():
                ref_path = str(PROJECT_DIR / ref_path)
            kwargs["reference_wav_path"] = ref_path

        # 加入 description 作為 style prompt
        desc = profile.get("description", "")
        if desc:
            kwargs["text"] = f"{desc}{text}"

        print(f"🎤 [{character}] 串流生成中...", file=sys.stderr)
        t0 = time.time()
        chunk_count = 0
        total_samples = 0

        for chunk in self.model.generate_streaming(**kwargs):
            chunk_count += 1
            total_samples += len(chunk)
            yield chunk

        elapsed = time.time() - t0
        duration = total_samples / self.out_sample_rate
        rtf = elapsed / duration if duration > 0 else 0
        print(f"   ✅ {chunk_count} chunks, {duration:.2f}s 音頻, {elapsed:.1f}s 生成, RTF={rtf:.2f}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Discord 串流 AudioSource — thread-safe buffer
# ---------------------------------------------------------------------------
class StreamAudioSource(discord.AudioSource):
    """Thread-safe PCM buffer → Discord voice client 每 20ms 消費一個 frame"""

    DISCORD_SAMPLE_RATE = 48000
    DISCORD_CHANNELS = 2
    FRAME_SIZE = 960  # samples per channel, 20ms @ 48kHz
    FRAME_BYTES = FRAME_SIZE * 2 * 2  # s16le, 2 channels = 3840 bytes

    def __init__(self):
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._finished = False
        self._error = None
        self._sent_last_frame = False

    def put_data(self, pcm_bytes: bytes):
        """Thread-safe: 放入 PCM bytes（s16le 48kHz stereo）"""
        with self._lock:
            self._buffer.extend(pcm_bytes)

    def mark_finished(self):
        with self._lock:
            self._finished = True

    def set_error(self, exc):
        with self._lock:
            self._error = exc
            self._finished = True

    def read(self) -> bytes:
        """Discord voice client 呼叫，每 20ms 一次。
        返回 FRAME_BYTES bytes = 繼續播放
        返回 b"" (空) = 播放結束，vc.is_playing() → False
        """
        with self._lock:
            if self._error:
                return b""  # 停止播放

            if len(self._buffer) >= self.FRAME_BYTES:
                frame = bytes(self._buffer[:self.FRAME_BYTES])
                del self._buffer[:self.FRAME_BYTES]
                return frame

            if self._finished:
                if self._sent_last_frame:
                    # 已經送完最後一幀，告訴 Discord 結束
                    return b""
                # 送最後一幀（含靜音填充）
                remaining = bytes(self._buffer)
                self._buffer.clear()
                pad_len = self.FRAME_BYTES - len(remaining)
                if pad_len > 0:
                    padding = b"\x00\x00" * (pad_len // 2)
                else:
                    padding = b""
                self._sent_last_frame = True
                return remaining + padding

            # 還在等數據，返回靜音（短暫緩衝）
            return b"\x00\x00" * self.FRAME_SIZE * 2

    def is_opus(self) -> bool:
        return False

    def cleanup(self):
        with self._lock:
            self._buffer.clear()


def float32_to_pcm_s16le_stereo(audio: np.ndarray) -> bytes:
    """float32 mono numpy array → s16le stereo bytes @ 48kHz"""
    audio = np.clip(audio, -1.0, 1.0)
    pcm = (audio * 32767).astype(np.int16)
    stereo = np.column_stack([pcm, pcm])
    return stereo.tobytes()


def normalize_rms(audio: np.ndarray, target_rms: float = 0.08) -> np.ndarray:
    """Per-chunk RMS normalize"""
    rms = np.sqrt(np.mean(audio ** 2))
    if rms < 1e-6:
        return audio
    gain = min(target_rms / rms, 10.0)
    return audio * gain


# ---------------------------------------------------------------------------
# Discord Bot
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
engine: Optional[VoxCpmEngine] = None


@bot.event
async def on_ready():
    print(f"✅ 串流脫口秀 Bot 上線：{bot.user}", file=sys.stderr)

    if not engine:
        return

    # 等一下讓主公有時間加入語音頻道
    print("⏳ 5秒後自動尋找語音頻道...", file=sys.stderr)
    await asyncio.sleep(5)

    # 找第一個有人的語音頻道
    target_vc = None
    text_ch = None
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            if len(vc.members) > 0:
                target_vc = vc
                break
        if target_vc:
            # 找第一個文字頻道用來發訊息
            for ch in guild.text_channels:
                text_ch = ch
                break
            break

    if not target_vc:
        print("❌ 找不到有人的語音頻道", file=sys.stderr)
        return

    print(f"📢 目標語音頻道：{target_vc.name}（{target_vc.guild.name}）", file=sys.stderr)
    await _run_streamtest(target_vc, text_ch)


async def _run_streamtest(voice_channel, text_channel):
    """核心串流脫口秀邏輯"""
    if engine is None:
        if text_channel:
            await text_channel.send("❌ 引擎未載入")
        return

    # 連接語音頻道
    # 找當前 bot 的 voice client
    vc = None
    for g in bot.guilds:
        if g.voice_client:
            vc = g.voice_client
            break

    if vc is None:
        vc = await voice_channel.connect()
    elif vc.channel != voice_channel:
        await vc.move_to(voice_channel)

    if text_channel:
        await text_channel.send("🎬 **王朝夜總會 — 串流直播版** 開始！")

    total_t0 = time.time()
    segment_count = len(TALKSHOW_SEGMENTS)

    for seg_idx, seg in enumerate(TALKSHOW_SEGMENTS):
        character = seg["character"]
        text = seg["text"]

        if text_channel:
            await text_channel.send(f"📢 **[{seg_idx+1}/{segment_count}] {character}**：{text}")

        # 建立 AudioSource
        source = StreamAudioSource()

        # 背景生成 thread
        def _bg_generate(src=source, txt=text, char=character):
            try:
                for chunk in engine.synthesize_streaming(txt, char):
                    chunk = normalize_rms(chunk, target_rms=0.08)
                    pcm = float32_to_pcm_s16le_stereo(chunk)
                    src.put_data(pcm)
                src.mark_finished()
            except Exception as e:
                print(f"❌ 串流生成錯誤：{e}", file=sys.stderr)
                import traceback
                traceback.print_exc(file=sys.stderr)
                src.set_error(e)

        gen_thread = threading.Thread(target=_bg_generate, daemon=True)
        gen_thread.start()

        # 給一點 initial buffer
        await asyncio.sleep(0.15)

        # 開始播放
        vc.play(source)

        # 等播放完畢
        while vc.is_playing():
            await asyncio.sleep(0.05)

        # 等生成 thread 結束
        gen_thread.join(timeout=5.0)

        # 段落間隔
        if seg_idx < segment_count - 1:
            await asyncio.sleep(0.3)

    total_elapsed = time.time() - total_t0
    if text_channel:
        await text_channel.send(f"🎬 **王朝夜總會結束！** 總時長：{total_elapsed:.1f}s")


@bot.command(name="streamtest")
async def cmd_streamtest(ctx):
    """觸發串流脫口秀測試"""
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("⚠️ 主公請先加入語音頻道")
        return
    await _run_streamtest(ctx.author.voice.channel, ctx)


@bot.command(name="join")
async def cmd_join(ctx):
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("⚠️ 主公不在語音頻道")
        return
    ch = ctx.author.voice.channel
    if ctx.voice_client:
        await ctx.voice_client.move_to(ch)
    else:
        await ch.connect()
    await ctx.send(f"✅ 已加入 **{ch.name}**")


@bot.command(name="leave")
async def cmd_leave(ctx):
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("👋 已離開")
    else:
        await ctx.send("⚠️ 不在語音頻道")


@bot.command(name="stop")
async def cmd_stop(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("⏹ 已停止")
    else:
        await ctx.send("⚠️ 沒在播放")


@bot.command(name="status")
async def cmd_status(ctx):
    import torch
    lines = [
        "**🏥 串流脫口秀 Bot 狀態**",
        f"  Bot：`{bot.user}`",
        f"  語音頻道：`{ctx.voice_client.channel.name if ctx.voice_client else '未連接'}`",
        f"  播放中：`{'是' if ctx.voice_client and ctx.voice_client.is_playing() else '否'}`",
        f"  模式：`常駐模型 + 串流推播`",
    ]
    if torch.cuda.is_available():
        free, total = torch.cuda.mem_get_info()
        used_gb = (total - free) / (1024**3)
        total_gb = total / (1024**3)
        lines.append(f"  VRAM：`{used_gb:.1f} / {total_gb:.1f} GB`")
    await ctx.send("\n".join(lines))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    global engine

    parser = argparse.ArgumentParser(description="串流脫口秀測試")
    parser.add_argument("--profiles", default=str(PROJECT_DIR / "profiles" / "voice_profiles.yaml"))
    args = parser.parse_args()

    token = os.environ.get("VOICE_BOT_TOKEN")
    if not token:
        print("❌ 請設定 VOICE_BOT_TOKEN")
        sys.exit(1)

    # 先載入模型
    engine = VoxCpmEngine(profiles_path=args.profiles)
    engine.load_model()

    # 啟動 bot
    print("\n🤖 啟動 Discord Bot（串流模式）...", file=sys.stderr)
    print("   指令：!join !streamtest !stop !status !leave", file=sys.stderr)
    bot.run(token)


if __name__ == "__main__":
    main()
