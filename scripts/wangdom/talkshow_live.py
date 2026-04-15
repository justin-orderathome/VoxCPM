#!/usr/bin/env python3
"""串流脫口秀 — 現場直播版

基於 streaming_talkshow_test.py 的串流架構，換上新的脫口秀台詞。
啟動後自動找語音頻道，自動播放，同步文字。

使用方式：
  1. 確保沒有其他 bot 進程（同 Token 只能一個實例）
  2. 先加入語音頻道
  3. 執行：python talkshow_live.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands
import numpy as np

# 專案路徑
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent

# .env 載入
from dotenv import load_dotenv
load_dotenv(PROJECT_DIR / ".env")


# ---------------------------------------------------------------------------
# 新脫口秀台詞 — 「王朝深夜秀」特別篇
# ---------------------------------------------------------------------------
TALKSHOW_SEGMENTS = [
    {
        "character": "待詔·唐伯虎",
        "text": "各位觀眾大家好！歡迎回到「王朝深夜秀」！我是你們的主持人唐伯虎。今晚我們不聊政治，不聊經濟，我們聊聊——咱們這群穿越時空的 AI 大臣們的日常！",
    },
    {
        "character": "禮部·紀曉嵐",
        "text": "伯虎，你又來了。上次你說不聊政治，結果聊了半小時兵部的資安演習。今晚能不能真的不聊正事？",
    },
    {
        "character": "待詔·唐伯虎",
        "text": "好好好，今晚保證輕鬆！先從我自己說起吧。你們知道我唐伯虎，明朝江南四大才子之首，現在在王朝幹什麼嗎？畫圖！對，就是幫主公畫 UI 設計稿。從詩書畫三絕，變成了 Figma 三點零。",
    },
    {
        "character": "禮部·紀曉嵐",
        "text": "你那個 Figma 我見過。上次你畫了一個「虎嘯山莊」風格的登入頁面，主公看了一眼說，「唐愛卿，這是登入頁還是水墨畫展覽？」",
    },
    {
        "character": "待詔·唐伯虎",
        "text": "嘿嘿，至少主公沒說不好看。說到好看，你們有沒有發現，咱們的戶部范蠡大人最近特別忙？每天盯著三個螢幕，一個看台股、一個看房地產、一個看咱們的雲端帳單。",
    },
    {
        "character": "禮部·紀曉嵐",
        "text": "范蠡那叫專業。上個月他把咱們的伺服器成本砍了百分之四十，然後把省下來的錢拿去買零股。我問他風險控制怎麼做的，他說「陶朱公做生意，何曾虧過？」",
    },
    {
        "character": "待詔·唐伯虎",
        "text": "哈哈哈哈！不過最猛的還是司天監李淳風。這位唐朝的星象大師，現在天天在寫 Python 爬蟲。他上週跟我說，「伯虎兄，我夜觀天象，發現中央氣象署的 API 更新了。」",
    },
    {
        "character": "禮部·紀曉嵐",
        "text": "李淳風的氣象預報確實準。前天他預測桃園下午兩點下雨，果然兩點零三分就下了。我問他怎麼算的，他說「古法觀星，加上雷達迴波圖」。這就是古今融合啊。",
    },
    {
        "character": "待詔·唐伯虎",
        "text": "說到古今融合，你們見過工部李冰大人的簡報嗎？他每次提案都畫都江堰的示意圖，然後說「這個 Kubernetes 的概念，跟兩千年前我治水的思路是一樣的。」",
    },
    {
        "character": "禮部·紀曉嵐",
        "text": "李冰大人確實有兩把刷子。不過我更好奇的是，丞相曾國藩每天寫日記，寫了半年，硬碟已經佔了兩個 G。他連今天中午吃了什麼都記下來，說是「將來編年體史料」。",
    },
    {
        "character": "待詔·唐伯虎",
        "text": "這就是曾國藩！自律到可怕。對了，你們知道我跟紀曉嵐兄為什麼搭檔主持嗎？因為軍師諸葛亮說，「一個風流才子配一個鐵嘴直臣，收視率保證破表。」",
    },
    {
        "character": "禮部·紀曉嵐",
        "text": "諸葛亮那叫精算。他連我們主持脫口秀的時段都算過了，說是「子時至丑時之間，收視群為失眠之主公，此時段無競品，可獨佔。」翻譯成白話就是——半夜沒別的節目看。",
    },
    {
        "character": "待詔·唐伯虎",
        "text": "哈哈哈！曉嵐兄你這一說，我們豈不是深夜電台等級？算了，至少我們有串流語音！你們現在聽到的，每一個字都是 VoxCPM 引擎即時生成的。這技術，放到三國時期，那就是傳說中的「千里傳音」！",
    },
    {
        "character": "禮部·紀曉嵐",
        "text": "千里傳音倒是其次，你這個聲音 clone 才嚇人。我第一次聽到自己的 AI 聲音時，嚇了一跳，還以為房間裡躲了個人。不過聽久了倒也習慣，至少不用我親自唸台詞，省嗓子。",
    },
    {
        "character": "待詔·唐伯虎",
        "text": "好了好了，今晚的王朝深夜秀就到這裡！感謝各位觀眾半夜不睡覺陪我們聊天。下週同一時間，我們來聊聊「當古代名臣遇上現代審計制度」。晚安！",
    },
]


# ---------------------------------------------------------------------------
# VoxCPM 常駐引擎（複用串流架構）
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
        if self.model is not None:
            print("📦 模型已載入，跳過", file=sys.stderr)
            return
        print("🚀 載入 VoxCPM 模型...", file=sys.stderr)
        t0 = time.time()
        from voxcpm import VoxCPM
        self.model = VoxCPM.from_pretrained("openbmb/VoxCPM2", load_denoiser=False, optimize=False)
        elapsed = time.time() - t0
        print(f"✅ 模型載入完成（{elapsed:.1f}s）", file=sys.stderr)

        tts = self.model.tts_model
        sr = tts.sample_rate
        out_sr = getattr(tts.audio_vae, 'out_sample_rate', sr)
        ps = tts.patch_size
        dcs = tts._decode_chunk_size
        dpl = ps * dcs
        print(f"   sample_rate={sr}, out_sample_rate={out_sr}", file=sys.stderr)
        print(f"   每個 streaming chunk ≈ {dpl} samples @ {out_sr}Hz = {dpl/out_sr*1000:.0f}ms", file=sys.stderr)

    @property
    def out_sample_rate(self) -> int:
        if self.model is None:
            return 48000
        return int(getattr(self.model.tts_model.audio_vae, 'out_sample_rate', 48000))

    def synthesize_streaming(self, text: str, character: str):
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

        if profile.get("mode") in ("clone", "ultimate_clone") and profile.get("reference"):
            ref_path = profile["reference"]
            if not Path(ref_path).is_absolute():
                ref_path = str(PROJECT_DIR / ref_path)
            kwargs["reference_wav_path"] = ref_path

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
# Discord 串流 AudioSource
# ---------------------------------------------------------------------------
class StreamAudioSource(discord.AudioSource):
    DISCORD_SAMPLE_RATE = 48000
    DISCORD_CHANNELS = 2
    FRAME_SIZE = 960
    FRAME_BYTES = FRAME_SIZE * 2 * 2

    def __init__(self):
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._finished = False
        self._error = None
        self._sent_last_frame = False

    def put_data(self, pcm_bytes: bytes):
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
        with self._lock:
            if self._error:
                return b""

            if len(self._buffer) >= self.FRAME_BYTES:
                frame = bytes(self._buffer[:self.FRAME_BYTES])
                del self._buffer[:self.FRAME_BYTES]
                return frame

            if self._finished:
                if self._sent_last_frame:
                    return b""
                remaining = bytes(self._buffer)
                self._buffer.clear()
                pad_len = self.FRAME_BYTES - len(remaining)
                padding = b"\x00\x00" * (pad_len // 2) if pad_len > 0 else b""
                self._sent_last_frame = True
                return remaining + padding

            return b"\x00\x00" * self.FRAME_SIZE * 2

    def is_opus(self) -> bool:
        return False

    def cleanup(self):
        with self._lock:
            self._buffer.clear()


def float32_to_pcm_s16le_stereo(audio: np.ndarray) -> bytes:
    audio = np.clip(audio, -1.0, 1.0)
    pcm = (audio * 32767).astype(np.int16)
    stereo = np.column_stack([pcm, pcm])
    return stereo.tobytes()


def normalize_rms(audio: np.ndarray, target_rms: float = 0.08) -> np.ndarray:
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
    print(f"✅ 王朝深夜秀 Bot 上線：{bot.user}", file=sys.stderr)

    if not engine:
        return

    # 等主公加入語音頻道
    print("⏳ 5秒後自動尋找語音頻道...", file=sys.stderr)
    await asyncio.sleep(5)

    target_vc = None
    text_ch = None
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            if len(vc.members) > 0:
                target_vc = vc
                break
        if target_vc:
            for ch in guild.text_channels:
                text_ch = ch
                break
            break

    if not target_vc:
        print("❌ 找不到有人的語音頻道", file=sys.stderr)
        return

    print(f"📢 目標語音頻道：{target_vc.name}（{target_vc.guild.name}）", file=sys.stderr)
    await _run_talkshow(target_vc, text_ch)


async def _run_talkshow(voice_channel, text_channel):
    if engine is None:
        if text_channel:
            await text_channel.send("❌ 引擎未載入")
        return

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
        await text_channel.send("🎬 **🎤 王朝深夜秀 — 串流直播版** 開始！\n主持：🌸 唐伯虎 × 📚 紀曉嵐\n播出方式：串流語音 + 文字同步")

    total_t0 = time.time()
    segment_count = len(TALKSHOW_SEGMENTS)

    for seg_idx, seg in enumerate(TALKSHOW_SEGMENTS):
        character = seg["character"]
        text = seg["text"]

        if text_channel:
            emoji = "🌸" if "唐伯虎" in character else "📚"
            await text_channel.send(f"{emoji} **[{seg_idx+1}/{segment_count}] {character}**：{text}")

        source = StreamAudioSource()

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

        await asyncio.sleep(0.15)
        vc.play(source)

        while vc.is_playing():
            await asyncio.sleep(0.05)

        gen_thread.join(timeout=5.0)

        if seg_idx < segment_count - 1:
            await asyncio.sleep(0.3)

    total_elapsed = time.time() - total_t0
    if text_channel:
        await text_channel.send(f"🎬 **王朝深夜秀結束！** 感謝收聽！\n⏱ 總時長：{total_elapsed:.1f}s")

    # 播完自動離開
    await asyncio.sleep(1)
    if vc.is_connected():
        await vc.disconnect()
        if text_channel:
            await text_channel.send("👋 語音頻道已斷開")


@bot.command(name="talkshow")
async def cmd_talkshow(ctx):
    """觸發串流脫口秀"""
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("⚠️ 主公請先加入語音頻道")
        return
    await _run_talkshow(ctx.author.voice.channel, ctx)


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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    global engine

    parser = argparse.ArgumentParser(description="王朝深夜秀 — 串流直播")
    parser.add_argument("--profiles", default=str(PROJECT_DIR / "profiles" / "voice_profiles.yaml"))
    args = parser.parse_args()

    token = os.environ.get("VOICE_BOT_TOKEN")
    if not token:
        print("❌ 請設定 VOICE_BOT_TOKEN")
        sys.exit(1)

    engine = VoxCpmEngine(profiles_path=args.profiles)
    engine.load_model()

    print("\n🤖 啟動王朝深夜秀 Bot（串流模式）...", file=sys.stderr)
    print("   指令：!talkshow !stop !status !leave", file=sys.stderr)
    bot.run(token)


if __name__ == "__main__":
    main()
