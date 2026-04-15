#!/usr/bin/env python3
"""崴勝王朝 · 獨立語音 Bot（常駐模型 + 真串流）

功能：
  - Discord 語音頻道串流播放（generate_streaming 逐 chunk）
  - 檔案模式輸出（MP3/WAV）
  - 所有語音輸出同步發送文字版
  - VoxCPM 模型常駐（啟動載入一次）
  - GPU 互斥鎖：串流/檔案生成排隊，不互踩
  - !stop 可中斷串流並回收資源
"""

from __future__ import annotations

import asyncio
import itertools
import os
import subprocess
import sys
import threading
import time
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import soundfile as sf

try:
    from aiohttp import web as aioweb
    _HAS_AIOHTTP = True
except ImportError:
    _HAS_AIOHTTP = False

# 從專案根目錄 .env 載入環境變數
from dotenv import load_dotenv

_PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(_PROJECT_DIR / ".env")

import discord
from discord import app_commands
from discord.ext import commands

# 同目錄模組
_BOT_DIR = Path(__file__).resolve().parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

from voxcpm_skill import VoiceProfileManager
from style_compiler import compile_text
from audio_post import post_process
from dialogue_parser import DialogueParser


# ---------------------------------------------------------------------------
# 路徑設定
# ---------------------------------------------------------------------------
PROJECT_DIR = _PROJECT_DIR
PROFILES_PATH = PROJECT_DIR / "profiles" / "voice_profiles.yaml"
OUTPUT_DIR = PROJECT_DIR / "test_output" / "voice_bot"
MORNING_CONFIG_PATH = PROJECT_DIR / "profiles" / "morning_schedule.json"
TW_TZ = ZoneInfo("Asia/Taipei")
HEARTBEAT_PATH = PROJECT_DIR / "output" / "voice-bot-heartbeat.json"


# ---------------------------------------------------------------------------
# 輸出路由
# ---------------------------------------------------------------------------
class OutputMode:
    STREAM = "stream"
    FILE = "file"
    AUTO = "auto"


class OutputRouter:
    def __init__(self, default_mode: str = OutputMode.AUTO):
        self.default_mode = default_mode

    def resolve(self, ctx, flag_file: bool = False, flag_stream: bool = False) -> str:
        if flag_file:
            return OutputMode.FILE
        if flag_stream:
            return OutputMode.STREAM
        mode = self.default_mode
        if mode == OutputMode.AUTO:
            if ctx.author.voice and ctx.author.voice.channel:
                return OutputMode.STREAM
            return OutputMode.FILE
        return mode


# ---------------------------------------------------------------------------
# 音訊工具
# ---------------------------------------------------------------------------
def normalize_rms(audio: np.ndarray, target_rms: float = 0.08) -> np.ndarray:
    rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2))) if audio.size else 0.0
    if rms < 1e-8:
        return audio.astype(np.float32)
    gain = min(target_rms / rms, 10.0)
    return (audio.astype(np.float32) * gain).astype(np.float32)


def _resample_audio(audio: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    if src_sr == dst_sr:
        return audio
    n = len(audio)
    if n == 0:
        return audio
    new_n = int(round(n * dst_sr / src_sr))
    try:
        from scipy.signal import resample  # type: ignore

        return resample(audio, new_n).astype(np.float32)
    except Exception:
        x_old = np.linspace(0.0, 1.0, n, endpoint=False)
        x_new = np.linspace(0.0, 1.0, new_n, endpoint=False)
        return np.interp(x_new, x_old, audio).astype(np.float32)


def mono_float_to_pcm_s16le_stereo(audio: np.ndarray, sample_rate: int) -> bytes:
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    if sample_rate != 48000:
        audio = _resample_audio(audio, sample_rate, 48000)
    audio = np.clip(audio, -1.0, 1.0)
    pcm = (audio * 32767.0).astype(np.int16)
    stereo = np.column_stack([pcm, pcm])
    return stereo.tobytes()


def crossfade_chunks(prev: np.ndarray, curr: np.ndarray, overlap_samples: int) -> tuple[np.ndarray, np.ndarray]:
    """回傳 (可立即輸出的塊, 需暫存等待下一塊的尾巴)。"""
    if prev.size == 0:
        return np.zeros(0, dtype=np.float32), curr.astype(np.float32)
    if curr.size == 0:
        return prev.astype(np.float32), np.zeros(0, dtype=np.float32)

    o = int(max(0, min(overlap_samples, len(prev), len(curr))))
    if o == 0:
        return prev.astype(np.float32), curr.astype(np.float32)

    out = prev.astype(np.float32).copy()
    fade_in = np.linspace(0.0, 1.0, o, endpoint=False, dtype=np.float32)
    fade_out = 1.0 - fade_in
    out[-o:] = out[-o:] * fade_out + curr[:o].astype(np.float32) * fade_in

    tail = curr[o:].astype(np.float32)
    return out, tail


class StreamAmbience:
    """輕量即時 ambience（串流用）。"""

    PRESETS = {
        "none": (0.0, 0.0, 0),
        "studio": (0.10, 0.15, 25),
        "hall": (0.20, 0.30, 110),
        "cave": (0.28, 0.36, 180),
        "battle": (0.16, 0.22, 70),
        "rain": (0.14, 0.18, 85),
    }

    def __init__(self, ambience: str, sample_rate: int):
        self.ambience = ambience if ambience in self.PRESETS else "none"
        self.sample_rate = sample_rate
        mix, fb, delay_ms = self.PRESETS[self.ambience]
        self.mix = float(mix)
        self.feedback = float(fb)
        self.delay = max(1, int(self.sample_rate * delay_ms / 1000.0))
        self.buf = np.zeros(self.delay, dtype=np.float32)
        self.idx = 0

    def process(self, chunk: np.ndarray) -> np.ndarray:
        x = np.asarray(chunk, dtype=np.float32).reshape(-1)
        if self.ambience == "none" or x.size == 0:
            return x
        y = x.copy()
        for i in range(len(y)):
            d = self.buf[self.idx]
            y[i] = np.clip(y[i] + self.mix * d, -1.0, 1.0)
            self.buf[self.idx] = np.clip(x[i] + self.feedback * d, -1.0, 1.0)
            self.idx += 1
            if self.idx >= self.delay:
                self.idx = 0
        return y


# ---------------------------------------------------------------------------
# 常駐 VoxCPM 引擎
# ---------------------------------------------------------------------------
class VoxCpmEngine:
    def __init__(self, profiles_path: str | Path, model_id: str = "openbmb/VoxCPM2"):
        self.model_id = model_id
        self.profile_manager = VoiceProfileManager(profiles_path)
        self._model = None
        self._gpu_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._waiting_jobs = 0
        self._active_job: Optional[str] = None

    @property
    def model_loaded(self) -> bool:
        return self._model is not None

    @property
    def busy(self) -> bool:
        return self._gpu_lock.locked()

    def queue_snapshot(self) -> dict:
        with self._stats_lock:
            return {
                "busy": self._gpu_lock.locked(),
                "waiting": self._waiting_jobs,
                "active_job": self._active_job,
            }

    def _acquire_gpu(self, job_label: str):
        with self._stats_lock:
            self._waiting_jobs += 1
        self._gpu_lock.acquire()
        with self._stats_lock:
            self._waiting_jobs = max(0, self._waiting_jobs - 1)
            self._active_job = job_label

    def _release_gpu(self):
        with self._stats_lock:
            self._active_job = None
        if self._gpu_lock.locked():
            self._gpu_lock.release()

    @property
    def out_sample_rate(self) -> int:
        if self._model is None:
            return 48000
        tts = self._model.tts_model
        return int(getattr(getattr(tts, "audio_vae", None), "out_sample_rate", getattr(tts, "sample_rate", 48000)))

    def load_model(self) -> None:
        if self._model is not None:
            return
        from voxcpm import VoxCPM  # lazy import

        t0 = time.time()
        # optimize=False：避免多線程串流下 CUDA graph assertion
        self._model = VoxCPM.from_pretrained(self.model_id, load_denoiser=False, optimize=False)
        dt = time.time() - t0
        print(f"✅ VoxCPM 模型已載入（{dt:.1f}s）, out_sample_rate={self.out_sample_rate}Hz")

    def _resolve_reference(self, ref: Optional[str]) -> Optional[str]:
        if not ref:
            return None
        p = Path(ref)
        if not p.is_absolute():
            p = PROJECT_DIR / p
        return str(p)

    def _edge_voice_for_character(self, character: str) -> str:
        profile = self.profile_manager.get(character)
        return profile.edge_voice or "zh-TW-YunJheNeural"

    def _build_kwargs(self, text: str, character: str, mood: Optional[str], expression_tags: Optional[list[str]]) -> dict:
        profile = self.profile_manager.get(character)
        compiled = compile_text(
            text=text,
            mood=mood,
            expression_tags=expression_tags,
            profile_description=profile.description,
        )
        kwargs = {
            "text": compiled,
            "cfg_value": 2.0,
            "inference_timesteps": 10,
        }
        if profile.mode in {"clone", "ultimate_clone"} and profile.reference:
            ref = self._resolve_reference(profile.reference)
            if ref:
                kwargs["reference_wav_path"] = ref
        return kwargs

    def synthesize_with_edge(self, text: str, character: str, output_path: str | Path) -> dict:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        voice = self._edge_voice_for_character(character)
        try:
            import edge_tts  # type: ignore
        except Exception:
            return {
                "ok": False,
                "engine": "edge_tts",
                "reason": "edge_tts_not_installed",
                "hint": "pip install edge-tts",
            }

        async def _run() -> None:
            communicate = edge_tts.Communicate(text=text, voice=voice)
            await communicate.save(str(output_path))

        asyncio.run(_run())
        return {
            "ok": True,
            "engine": "edge_tts",
            "voice": voice,
            "output_path": str(output_path),
            "format": output_path.suffix.lstrip(".") or "mp3",
        }

    def synthesize_file(
        self,
        text: str,
        character: str,
        output_path: str | Path,
        mood: Optional[str] = None,
        expression_tags: Optional[list[str]] = None,
        ambience: str = "none",
        output_format: str = "mp3",
    ) -> dict:
        self.load_model()
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        self._acquire_gpu(f"file:{character}")
        try:
            kwargs = self._build_kwargs(text, character, mood, expression_tags)
            wav = self._model.generate(**kwargs)
            arr = wav.cpu().numpy() if hasattr(wav, "cpu") else wav
            arr = np.asarray(arr, dtype=np.float32)
            if arr.ndim == 2:
                arr = arr.squeeze(0)
            arr = normalize_rms(arr, target_rms=0.08)

            sr = self.out_sample_rate
            wav_path = output_path if output_path.suffix.lower() == ".wav" else output_path.with_suffix(".wav")
            sf.write(str(wav_path), arr, sr)

            final_path = wav_path
            if ambience and ambience != "none":
                post_path = wav_path.with_name(f"{wav_path.stem}.post.wav")
                post_process(wav_path, post_path, ambience_profile=ambience, normalize=0.08)
                wav_path.unlink(missing_ok=True)
                final_path = post_path

            if output_format == "mp3":
                mp3_path = output_path if output_path.suffix.lower() == ".mp3" else output_path.with_suffix(".mp3")
                cmd = [
                    "ffmpeg", "-y", "-i", str(final_path),
                    "-codec:a", "libmp3lame", "-b:a", "128k",
                    str(mp3_path),
                ]
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
                if r.returncode != 0:
                    return {
                        "ok": False,
                        "reason": f"ffmpeg 轉 MP3 失敗: {r.stderr[:300]}",
                        "output_path": str(final_path),
                        "format": "wav",
                    }
                final_path.unlink(missing_ok=True)
                final_path = mp3_path

            return {
                "ok": True,
                "engine": "voxcpm",
                "output_path": str(final_path),
                "format": output_format,
                "sample_rate": sr,
                "character": character,
            }
        except Exception as e:
            # 檔案模式自動 fallback 到 edge_tts，避免失聲
            edge_out = output_path if output_path.suffix.lower() == ".mp3" else output_path.with_suffix(".mp3")
            edge = self.synthesize_with_edge(text=text, character=character, output_path=edge_out)
            if edge.get("ok"):
                edge["fallback_from"] = f"voxcpm_error: {e}"
                return edge
            return {
                "ok": False,
                "reason": f"voxcpm_failed_and_edge_failed: {e}; {edge.get('reason', 'unknown')}",
            }
        finally:
            self._release_gpu()

    def stream_chunks(
        self,
        text: str,
        character: str,
        mood: Optional[str] = None,
        expression_tags: Optional[list[str]] = None,
        stop_event: Optional[threading.Event] = None,
        ambience: str = "none",
    ):
        """yield float32 mono chunks（crossfade 去邊界 click/pop + 即時 ambience）。"""
        self.load_model()

        self._acquire_gpu(f"stream:{character}")
        try:
            kwargs = self._build_kwargs(text, character, mood, expression_tags)
            sr = self.out_sample_rate
            overlap = max(1, int(sr * 0.006))  # 6ms
            ambience_fx = StreamAmbience(ambience, sr)

            prev: Optional[np.ndarray] = None
            for chunk in self._model.generate_streaming(**kwargs):
                if stop_event and stop_event.is_set():
                    break
                c = np.asarray(chunk, dtype=np.float32).reshape(-1)
                c = ambience_fx.process(c)
                c = normalize_rms(c, target_rms=0.08)

                if prev is None:
                    prev = c
                    continue

                out, tail = crossfade_chunks(prev, c, overlap)
                if out.size:
                    yield out
                prev = tail if tail.size else None

            if prev is not None and prev.size and not (stop_event and stop_event.is_set()):
                yield prev
        finally:
            self._release_gpu()


# ---------------------------------------------------------------------------
# Discord 串流 AudioSource
# ---------------------------------------------------------------------------
class StreamAudioSource(discord.AudioSource):
    DISCORD_SAMPLE_RATE = 48000
    DISCORD_CHANNELS = 2
    FRAME_SIZE = 960  # 20ms @ 48kHz
    FRAME_BYTES = FRAME_SIZE * DISCORD_CHANNELS * 2  # s16le

    def __init__(self):
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._finished = False
        self._error: Optional[str] = None
        self._sent_last_frame = False

    @property
    def error(self) -> Optional[str]:
        return self._error

    def put_data(self, pcm_bytes: bytes):
        with self._lock:
            self._buffer.extend(pcm_bytes)

    def mark_finished(self):
        with self._lock:
            self._finished = True

    def set_error(self, message: str):
        with self._lock:
            self._error = message
            self._finished = True

    def read(self) -> bytes:
        with self._lock:
            if self._error:
                return b""

            if len(self._buffer) >= self.FRAME_BYTES:
                frame = bytes(self._buffer[: self.FRAME_BYTES])
                del self._buffer[: self.FRAME_BYTES]
                return frame

            if self._finished:
                if self._sent_last_frame:
                    return b""
                remaining = bytes(self._buffer)
                self._buffer.clear()
                pad_len = max(0, self.FRAME_BYTES - len(remaining))
                self._sent_last_frame = True
                return remaining + (b"\x00" * pad_len)

            # underflow：短暫靜音墊片
            return b"\x00" * self.FRAME_BYTES

    def is_opus(self) -> bool:
        return False


@dataclass
class ActiveStream:
    task_id: str
    source: StreamAudioSource
    stop_event: threading.Event
    producer_thread: threading.Thread
    character: str


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix=commands.when_mentioned_or("!"), intents=intents)
_router = OutputRouter(default_mode=OutputMode.AUTO)
_engine = VoxCpmEngine(PROFILES_PATH)
_active_streams: dict[int, ActiveStream] = {}
_task_counter = itertools.count(1)
_morning_scheduler_task: Optional[asyncio.Task] = None


def _next_task_id(prefix: str = "T") -> str:
    return f"{prefix}{next(_task_counter):04d}"


# ---------------------------------------------------------------------------
# 工具函式
# ---------------------------------------------------------------------------
def _parse_args(text: str) -> dict:
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


def _get_active_stream(guild_id: int) -> Optional[ActiveStream]:
    return _active_streams.get(guild_id)


def _set_active_stream(guild_id: int, stream: ActiveStream):
    _active_streams[guild_id] = stream


def _clear_active_stream(guild_id: int):
    _active_streams.pop(guild_id, None)


async def _send_text_sync(ctx, text: str, character: str):
    await ctx.send(f"**{character}**：{text}")


def _parse_mode_flags(args: str) -> dict:
    parts = args.strip().split() if args else []
    return {
        "file": "-f" in parts,
        "stream": "-s" in parts,
    }


def _parse_hhmm(value: str) -> Optional[tuple[int, int]]:
    try:
        hh, mm = value.strip().split(":")
        h = int(hh)
        m = int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception:
        return None
    return None


def _default_morning_schedule() -> dict:
    return {
        "enabled": False,
        "time": "07:00",
        "guild_id": None,
        "channel_id": None,
        "voice_channel_id": None,
        "mode": "auto",  # auto | stream | file
        "mood": "沉穩",
        "ambience": "hall",
        "last_run_date": "",
    }


def _load_morning_schedule() -> dict:
    cfg = _default_morning_schedule()
    try:
        if MORNING_CONFIG_PATH.exists():
            raw = json.loads(MORNING_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg.update(raw)
    except Exception as e:
        print(f"⚠️ 讀取 morning 排程設定失敗：{e}")
    return cfg


def _save_morning_schedule(cfg: dict):
    MORNING_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    MORNING_CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def _run_morning_broadcast(
    channel,
    mode: str = "auto",
    mood: str = "沉穩",
    ambience: str = "hall",
    preferred_voice_channel_id: Optional[int] = None,
):
    try:
        await channel.send("🌅 早朝語音整備中，正在載入天機報...")
        report = await asyncio.get_running_loop().run_in_executor(None, _load_tianji_report)
        lines = _build_morning_lines(report)

        if report.get("error"):
            await channel.send(f"⚠️ 天機報載入異常：{report['error']}")

        effective_mode = mode
        if effective_mode == OutputMode.AUTO:
            if preferred_voice_channel_id:
                effective_mode = OutputMode.STREAM
            elif getattr(channel, "author", None) and getattr(channel.author, "voice", None):
                effective_mode = OutputMode.STREAM
            else:
                effective_mode = OutputMode.FILE

        degraded = False
        if effective_mode == OutputMode.STREAM and not preferred_voice_channel_id:
            author_vc = (
                getattr(channel, "author", None)
                and getattr(channel.author, "voice", None)
                and getattr(channel.author.voice, "channel", None)
            )
            if not author_vc:
                degraded = True
                effective_mode = OutputMode.FILE
                await channel.send("⚠️ 找不到可用語音頻道，自動降級為檔案模式")

        for idx, (character, text) in enumerate(lines, 1):
            await channel.send(f"🎭 [{idx}/{len(lines)}] {character} 發言中…")
            if effective_mode == OutputMode.STREAM:
                await _synthesize_and_play_stream(
                    channel,
                    character,
                    text,
                    mood=mood,
                    ambience=ambience,
                    preferred_voice_channel_id=preferred_voice_channel_id,
                )
            else:
                await _synthesize_and_send_file(channel, character, text, mood=mood, ambience=ambience)

        await channel.send("✅ 早朝語音播報完畢")
    except Exception as e:
        error_msg = f"❌ 早朝播報異常：{type(e).__name__}: {e}"
        print(error_msg)
        try:
            await channel.send(error_msg)
        except Exception:
            pass


async def _morning_scheduler_loop():
    await bot.wait_until_ready()
    print("⏰ Morning scheduler 已啟動")

    while not bot.is_closed():
        cfg = _load_morning_schedule()
        if not cfg.get("enabled"):
            await asyncio.sleep(10)
            continue

        schedule_time = str(cfg.get("time") or "07:00")
        parsed = _parse_hhmm(schedule_time)
        if not parsed:
            print(f"⚠️ 無效 morning 時間設定：{schedule_time}")
            await asyncio.sleep(60)
            continue

        now = datetime.now(TW_TZ)
        h, m = parsed
        today_str = now.strftime("%Y-%m-%d")
        should_run = (now.hour == h and now.minute == m and cfg.get("last_run_date") != today_str)

        if should_run:
            guild_id = cfg.get("guild_id")
            channel_id = cfg.get("channel_id")
            if not guild_id or not channel_id:
                print("⚠️ morning 排程已啟用但 guild_id/channel_id 未設定")
            else:
                guild = bot.get_guild(int(guild_id))
                channel = guild.get_channel(int(channel_id)) if guild else None
                if channel is None:
                    channel = bot.get_channel(int(channel_id))
                if channel is None:
                    print(f"⚠️ 找不到 morning 目標頻道：guild={guild_id}, channel={channel_id}")
                else:
                    mode = str(cfg.get("mode") or "auto").lower()
                    if mode not in {OutputMode.AUTO, OutputMode.STREAM, OutputMode.FILE}:
                        mode = OutputMode.AUTO
                    try:
                        await _run_morning_broadcast(
                            channel,
                            mode=mode,
                            mood=str(cfg.get("mood") or "沉穩"),
                            ambience=str(cfg.get("ambience") or "hall"),
                            preferred_voice_channel_id=(
                                int(cfg.get("voice_channel_id")) if cfg.get("voice_channel_id") else None
                            ),
                        )
                        cfg["last_run_date"] = today_str
                        _save_morning_schedule(cfg)
                        print(f"✅ morning 已執行：{today_str} {schedule_time}")
                    except Exception as e:
                        print(f"❌ morning 執行失敗：{e}")

        await asyncio.sleep(10)


def _load_tianji_report() -> dict:
    script = Path.home() / ".hermes" / "scripts" / "tianji-report.py"
    if not script.exists():
        return {"error": f"tianji script not found: {script}"}
    try:
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            timeout=40,
            check=False,
        )
        if result.returncode != 0:
            return {"error": f"tianji-report failed: {result.stderr.strip()[:200]}"}
        return json.loads(result.stdout)
    except Exception as e:
        return {"error": str(e)}


def _build_morning_lines(report: dict) -> list[tuple[str, str]]:
    if not report or report.get("error"):
        return [
            ("司天監·李淳風", "啟稟主公，今日天機報載入失敗，請准許以簡報模式先行。"),
            ("軍師·諸葛亮", "主公萬安。語音早朝系統已可用，請裁示是否改以文字早朝。"),
        ]

    date_str = report.get("date", "今日")
    weekday = report.get("weekday", "")
    lunar = report.get("lunar_date", {}).get("lunar_display", "")
    solar = report.get("solar_term", {})
    current_term = solar.get("current_term") or ""
    next_term = solar.get("next_term") or ""
    days_next = solar.get("days_until_next")

    holiday = report.get("holiday_check", {})
    is_holiday = bool(holiday.get("isHoliday"))
    holiday_text = "休沐日" if is_holiday else "工作日"

    weather = report.get("weather", {})
    weather_brief = weather.get("brief") if isinstance(weather, dict) else None
    if not weather_brief:
        weather_brief = "天候資料暫缺"

    todo = report.get("todo_status", {}).get("summary", {})
    pending = todo.get("pending", 0)
    delayed = todo.get("delayed", 0)
    suspected = todo.get("suspected_delay", 0)

    line1 = f"主公萬安。今日 {date_str} 星期{weekday}，農曆{lunar}。"
    if next_term and days_next is not None:
        line2 = f"當前節氣{current_term}，距{next_term}尚有{days_next}日；今日為{holiday_text}。"
    else:
        line2 = f"今日為{holiday_text}。"
    line3 = f"氣象簡報：{weather_brief}。"
    line4 = f"待辦總覽：待辦{pending}件，延遲{delayed}件，疑似延遲{suspected}件。"

    return [
        ("司天監·李淳風", line1),
        ("司天監·李淳風", line2),
        ("司天監·李淳風", line3),
        ("御史·魏徵", line4),
        ("軍師·諸葛亮", "請主公裁示今日首要政務，臣等即刻分辦。"),
    ]


def _build_greeting_lines(report: dict) -> list[tuple[str, str]]:
    if not report or report.get("error"):
        return [
            ("司天監·李淳風", "啟稟主公，今日節慶資料暫不可得，先行呈上日常問候。"),
            ("待詔·唐伯虎", "主公萬安，願今日諸事順意，心境清朗。"),
            ("軍師·諸葛亮", "若有要務，孔明隨時聽候調度。"),
        ]

    date_str = report.get("date", "今日")
    weekday = report.get("weekday", "")
    today_festivals = report.get("today_festival", []) or []
    solar = report.get("solar_term", {})
    current_term = solar.get("current_term") or ""
    next_term = solar.get("next_term") or ""
    days_next = solar.get("days_until_next")
    holiday = report.get("holiday_check", {})
    is_holiday = bool(holiday.get("isHoliday"))

    if today_festivals:
        ftxt = "、".join(today_festivals)
        line1 = f"主公萬安。今日 {date_str} 星期{weekday}，適逢{ftxt}。"
        line2 = "值此佳節，願主公福澤綿長、萬事亨通。"
        line3 = "孔明謹獻節日問候，祝王朝諸務昌隆。"
        return [
            ("司天監·李淳風", line1),
            ("待詔·唐伯虎", line2),
            ("軍師·諸葛亮", line3),
        ]

    if current_term:
        if next_term and days_next is not None:
            line1 = f"主公萬安。今日 {date_str} 星期{weekday}，當前節氣為{current_term}。"
            line2 = f"距{next_term}尚有{days_next}日，願主公順時養氣，諸事安泰。"
        else:
            line1 = f"主公萬安。今日 {date_str} 星期{weekday}，當前節氣為{current_term}。"
            line2 = "願主公應時而行，萬務皆得其宜。"
        line3 = "孔明謹以節氣問候，願王朝行穩致遠。"
        return [
            ("司天監·李淳風", line1),
            ("待詔·唐伯虎", line2),
            ("軍師·諸葛亮", line3),
        ]

    holiday_text = "休沐日" if is_holiday else "工作日"
    return [
        ("司天監·李淳風", f"主公萬安。今日 {date_str} 星期{weekday}，為{holiday_text}。"),
        ("待詔·唐伯虎", "願主公今日心神寧定，所行皆順。"),
        ("軍師·諸葛亮", "如需調度，孔明即刻承命。"),
    ]


def _build_drama_text_summary(result: dict) -> str:
    preview = result.get("preview", [])
    lines = [
        "🎭 **廣播劇摘要**",
        f"  劇本：`{result.get('script_path', '')}`",
        f"  段數：`{result.get('segments', 0)}`",
        f"  時長：`{result.get('duration_sec', 0):.1f}s`",
    ]
    if preview:
        lines.append("  片段預覽：")
        for row in preview:
            lines.append(f"  - **{row['speaker']}**：{row['text']}")
    return "\n".join(lines)


def _build_drama_transcript_chunks(result: dict, max_len: int = 1800) -> list[str]:
    lines = result.get("transcript_lines", []) or []
    if not lines:
        return []

    chunks: list[str] = []
    buf = "📝 **廣播劇文字同步**\n"
    for row in lines:
        one = f"**{row['speaker']}**：{row['text']}\n"
        if len(buf) + len(one) > max_len:
            chunks.append(buf.rstrip())
            buf = "📝 **廣播劇文字同步（續）**\n" + one
        else:
            buf += one
    if buf.strip():
        chunks.append(buf.rstrip())
    return chunks


async def _send_drama_transcript_timed(
    ctx,
    timeline: list[dict],
    speed: float = 1.0,
    lead_seconds: float = 0.55,
):
    """串流模式：依時間軸逐句同步文字（預設提前 0.55s）。"""
    if not timeline:
        return

    try:
        loop = asyncio.get_running_loop()
        t0 = loop.time()

        for row in timeline:
            start_at = float(row.get("start_sec", 0.0)) / max(speed, 1e-6)
            emit_at = max(0.0, start_at - lead_seconds)
            now = loop.time() - t0
            wait_s = emit_at - now
            if wait_s > 0:
                await asyncio.sleep(wait_s)
            await ctx.send(f"**{row['speaker']}**：{row['text']}")
    except Exception:
        # 不影響主流程播放
        return


def _convert_wav_to_mp3(wav_path: str, mp3_path: str) -> tuple[bool, str]:
    cmd = [
        "ffmpeg", "-y", "-i", wav_path,
        "-codec:a", "libmp3lame", "-b:a", "128k", mp3_path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
        if proc.returncode != 0:
            return False, (proc.stderr or proc.stdout or "ffmpeg failed")[:200]
        return True, ""
    except Exception as e:
        return False, str(e)


def _render_drama_audio(script_path: str, output_wav: str, max_segments: int = 30) -> dict:
    parser = DialogueParser()
    segments = parser.parse_file(script_path)

    if not segments:
        return {"ok": False, "reason": "劇本沒有可解析段落"}
    if len(segments) > max_segments:
        return {"ok": False, "reason": f"段數超限（{len(segments)} > {max_segments}）"}

    profiles = _engine.profile_manager._profiles
    unknown = sorted({seg.speaker for seg in segments if seg.speaker not in profiles})
    if unknown:
        return {"ok": False, "reason": f"角色未定義：{', '.join(unknown)}"}

    sr_target = _engine.out_sample_rate
    audios: list[np.ndarray] = []
    preview: list[dict] = []
    transcript_lines: list[dict] = []
    transcript_timeline: list[dict] = []
    cursor_sec = 0.0

    with tempfile.TemporaryDirectory(prefix="wangdom_drama_") as td:
        for i, seg in enumerate(segments):
            seg_path = Path(td) / f"seg_{i:04d}.wav"
            res = _engine.synthesize_file(
                text=seg.text,
                character=seg.speaker,
                output_path=seg_path,
                mood=seg.mood,
                ambience="none",
                output_format="wav",
            )
            if not res.get("ok"):
                return {"ok": False, "reason": f"第{i+1}段生成失敗：{res.get('reason', 'unknown')}"}

            audio, sr = sf.read(str(seg_path), dtype="float32")
            if audio.ndim == 2:
                audio = audio.mean(axis=1)
            if int(sr) != int(sr_target):
                audio = _resample_audio(audio, int(sr), int(sr_target))
            audio = normalize_rms(audio, target_rms=0.08)
            seg_duration = len(audio) / float(sr_target)
            audios.append(audio.astype(np.float32))

            pause_sec = float(getattr(seg, "pause_after", 0.5) or 0.5)
            if i < len(segments) - 1 and pause_sec > 0:
                audios.append(np.zeros(int(sr_target * pause_sec), dtype=np.float32))

            if len(preview) < 3:
                t = seg.text.replace("\n", " ").strip()
                preview.append({"speaker": seg.speaker, "text": (t[:42] + "…") if len(t) > 42 else t})

            transcript_lines.append({
                "speaker": seg.speaker,
                "text": seg.text.replace("\n", " ").strip(),
            })
            transcript_timeline.append({
                "speaker": seg.speaker,
                "text": seg.text.replace("\n", " ").strip(),
                "start_sec": round(cursor_sec, 3),
                "duration_sec": round(seg_duration, 3),
            })
            cursor_sec += seg_duration + (pause_sec if i < len(segments) - 1 and pause_sec > 0 else 0.0)

    merged = np.concatenate(audios) if audios else np.zeros(1, dtype=np.float32)
    sf.write(output_wav, merged, sr_target)

    return {
        "ok": True,
        "output_path": output_wav,
        "script_path": str(script_path),
        "segments": len(segments),
        "duration_sec": len(merged) / float(sr_target),
        "preview": preview,
        "transcript_lines": transcript_lines,
        "transcript_timeline": transcript_timeline,
    }


async def _ensure_voice_client(ctx, preferred_voice_channel_id: Optional[int] = None):
    guild = getattr(ctx, "guild", None)
    vc = getattr(ctx, "voice_client", None) or (guild.voice_client if guild else None)

    target_channel = None
    if preferred_voice_channel_id and guild:
        target_channel = guild.get_channel(int(preferred_voice_channel_id))

    if target_channel is None:
        author = getattr(ctx, "author", None)
        if author and getattr(author, "voice", None) and author.voice.channel:
            target_channel = author.voice.channel

    if target_channel is None:
        return None

    if vc is None:
        vc = await target_channel.connect()
    elif vc.channel != target_channel:
        await vc.move_to(target_channel)
    return vc


async def _synthesize_and_play_stream(
    ctx,
    character: str,
    text: str,
    mood=None,
    ambience="none",
    preferred_voice_channel_id: Optional[int] = None,
) -> bool:
    vc = await _ensure_voice_client(ctx, preferred_voice_channel_id=preferred_voice_channel_id)
    if vc is None:
        await ctx.send("⚠️ 找不到可用語音頻道，請先加入語音或於 morning 排程指定 voice_channel_id")
        return False

    guild_id = ctx.guild.id if ctx.guild else 0
    current = _get_active_stream(guild_id)
    if current:
        await ctx.send(f"⚠️ 目前已有串流任務進行中（{current.task_id}：{current.character}），請稍候或 `!stop`。")
        return False

    if vc.is_playing():
        vc.stop()

    task_id = _next_task_id("S")
    status_msg = await ctx.send(f"🎵 [{task_id}] {character} 正在串流生成...")
    await _send_text_sync(ctx, text, character)

    source = StreamAudioSource()
    stop_event = threading.Event()

    def _producer():
        try:
            for chunk in _engine.stream_chunks(
                text=text,
                character=character,
                mood=mood,
                expression_tags=None,
                stop_event=stop_event,
                ambience=ambience,
            ):
                if stop_event.is_set():
                    break
                pcm = mono_float_to_pcm_s16le_stereo(chunk, sample_rate=_engine.out_sample_rate)
                source.put_data(pcm)
        except Exception as e:
            source.set_error(str(e))
        finally:
            source.mark_finished()

    producer_thread = threading.Thread(target=_producer, daemon=True)
    producer_thread.start()
    _set_active_stream(
        guild_id,
        ActiveStream(
            task_id=task_id,
            source=source,
            stop_event=stop_event,
            producer_thread=producer_thread,
            character=character,
        ),
    )

    def _after_play(_err):
        stop_event.set()

    # 給一點初始 buffer
    await asyncio.sleep(0.12)
    vc.play(source, after=_after_play)
    await status_msg.edit(content=f"🔊 [{task_id}] {character} 正在播放...")

    try:
        while vc.is_playing():
            await asyncio.sleep(0.05)
        await asyncio.get_running_loop().run_in_executor(None, lambda: producer_thread.join(timeout=3.0))

        if source.error:
            await ctx.send(f"⚠️ [{task_id}] 串流失敗，改走 Edge fallback：{source.error[:120]}")
            fb_path = OUTPUT_DIR / f"fallback_{int(time.time()*1000)}.mp3"
            result = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: _engine.synthesize_with_edge(text=text, character=character, output_path=fb_path),
            )
            if result.get("ok") and Path(result["output_path"]).exists():
                fallback_source = discord.FFmpegPCMAudio(
                    str(result["output_path"]),
                    options="-vn -f s16le -ar 48000 -ac 2",
                )
                vc.play(fallback_source)
                await ctx.send(f"🔁 [{task_id}] 已切換 Edge fallback 續播")
            else:
                await ctx.send(f"❌ [{task_id}] fallback 失敗：{result.get('reason', '未知')}" )
    finally:
        stop_event.set()
        _clear_active_stream(guild_id)

    return True


async def _synthesize_and_send_file(ctx, character: str, text: str, mood=None, ambience="none"):
    status_msg = await ctx.send(f"🎵 {character} 正在生成語音檔...（若串流中將自動排隊）")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time() * 1000)
    out_path = OUTPUT_DIR / f"say_{ts}.mp3"

    result = await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: _engine.synthesize_file(
            text=text,
            character=character,
            output_path=out_path,
            mood=mood,
            ambience=ambience,
            output_format="mp3",
        ),
    )

    if not result.get("ok"):
        await status_msg.edit(content=f"❌ 語音生成失敗：{result.get('reason', '未知')}" )
        return

    actual_path = result["output_path"]
    if not Path(actual_path).exists():
        await status_msg.edit(content="❌ 語音檔案未產生")
        return

    audio_file = discord.File(actual_path, filename=Path(actual_path).name)
    await ctx.send(f"**{character}**：{text}", file=audio_file)
    await status_msg.delete()

    try:
        Path(actual_path).unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 事件
# ---------------------------------------------------------------------------
async def _heartbeat_loop():
    """每 30 秒寫入心跳檔，供 healthcheck 偵測 Discord 連線存活"""
    HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    while not bot.is_closed():
        try:
            payload = {
                "timestamp": datetime.now(TW_TZ).isoformat(),
                "latency_ms": round(bot.latency * 1000, 1),
                "guilds": len(bot.guilds),
                "user": str(bot.user),
            }
            HEARTBEAT_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        await asyncio.sleep(30)


@bot.event
async def on_ready():
    global _morning_scheduler_task

    print(f"✅ 王朝語音 Bot 已上線：{bot.user}")
    print(f"   輸出模式：{_router.default_mode}")
    print("   VRAM 策略：常駐模型（啟動載入一次）")
    try:
        synced = await bot.tree.sync()
        print(f"   Slash 指令已同步：{len(synced)}")
    except Exception as e:
        print(f"   ⚠️ Slash 同步失敗：{e}")

    if _morning_scheduler_task is None or _morning_scheduler_task.done():
        _morning_scheduler_task = asyncio.create_task(_morning_scheduler_loop())
        print("   ⏰ morning scheduler background task 已建立")

    # Start heartbeat
    asyncio.create_task(_heartbeat_loop())

    # Start internal API server (if enabled)
    if _API_ENABLED:
        await _start_api_server()


# ---------------------------------------------------------------------------
# 全域錯誤處理
# ---------------------------------------------------------------------------
@bot.event
async def on_command_error(ctx, error):
    """全域指令錯誤處理 — 避免異常靜默吞掉"""
    if isinstance(error, commands.CommandNotFound):
        # 只在 guild 頻道回覆，DM 不打擾
        if ctx.guild:
            await ctx.send(f"⚠️ 未知指令：`{ctx.invoked_with}`。輸入 `!status` 查看可用指令。")
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"⚠️ 缺少參數：`{error.param.name}`")
        return
    # 其他錯誤
    error_msg = f"❌ 指令執行異常：{type(error).__name__}: {error}"
    print(f"[on_command_error] {error_msg}")
    try:
        await ctx.send(error_msg)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 指令
# ---------------------------------------------------------------------------
@bot.command(name="join")
async def cmd_join(ctx):
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("⚠️ 主公不在任何語音頻道")
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
        guild_id = ctx.guild.id if ctx.guild else 0
        active = _get_active_stream(guild_id)
        if active:
            active.stop_event.set()
        await ctx.voice_client.disconnect()
        _clear_active_stream(guild_id)
        await ctx.send("👋 已離開語音頻道")
    else:
        await ctx.send("⚠️ 不在任何語音頻道")


@bot.command(name="say")
async def cmd_say(ctx, *, args: str = ""):
    """生成角色語音：!say [-f|-s] [--mood 情緒] [--ambience 音場] <角色> <台詞>"""
    if not args:
        await ctx.send("用法：`!say [-f|-s] [--mood 情緒] [--ambience 音場] <角色> <台詞>`")
        return

    parsed = _parse_args(args)
    character = parsed["character"]
    text = parsed["text"]

    if not character or not text:
        await ctx.send("⚠️ 請指定角色和台詞，例如：`!say 軍師·諸葛亮 稟主公`")
        return

    mode = _router.resolve(ctx, flag_file=parsed["file"], flag_stream=parsed["stream"])

    if mode == OutputMode.STREAM:
        await _synthesize_and_play_stream(
            ctx, character, text,
            mood=parsed["mood"], ambience=parsed["ambience"],
        )
    else:
        await _synthesize_and_send_file(
            ctx, character, text,
            mood=parsed["mood"], ambience=parsed["ambience"],
        )


@bot.command(name="play")
async def cmd_play(ctx, *, args: str = ""):
    """播放既有音頻檔：!play [-f|-s] <檔案路徑>"""
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
        vc = await _ensure_voice_client(ctx)
        if vc is None:
            await ctx.send("⚠️ 主公不在語音頻道，請先加入或使用 `-f`")
            return

        # 用 FFmpegPCMAudio 直接播既有音檔
        source = discord.FFmpegPCMAudio(
            str(file_path),
            options="-vn -f s16le -ar 48000 -ac 2",
        )
        vc.play(source)
        await ctx.send(f"🔊 正在播放：`{Path(file_path).name}`")
    else:
        audio_file = discord.File(file_path, filename=Path(file_path).name)
        await ctx.send(f"📎 語音檔案：`{Path(file_path).name}`", file=audio_file)


@bot.command(name="stop")
async def cmd_stop(ctx, task_id: str = ""):
    guild_id = ctx.guild.id if ctx.guild else 0
    active = _get_active_stream(guild_id)

    if active and task_id and active.task_id != task_id:
        await ctx.send(f"⚠️ 目前執行中任務為 `{active.task_id}`，未匹配 `{task_id}`")
        return

    if active:
        active.stop_event.set()
        active.source.mark_finished()

    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()

    if active:
        await asyncio.get_running_loop().run_in_executor(None, lambda: active.producer_thread.join(timeout=2.0))
        _clear_active_stream(guild_id)
        await ctx.send(f"⏹ 已停止任務 `{active.task_id}` 並回收串流資源")
    else:
        await ctx.send("⚠️ 目前沒有在播放")


@bot.command(name="queue")
async def cmd_queue(ctx):
    guild_id = ctx.guild.id if ctx.guild else 0
    active = _get_active_stream(guild_id)
    snap = _engine.queue_snapshot()
    lines = [
        "**📋 語音任務佇列**",
        f"  引擎狀態：`{'忙碌' if snap['busy'] else '空閒'}`",
        f"  等待中：`{snap['waiting']}`",
        f"  執行中：`{snap['active_job'] or '無'}`",
    ]
    if active:
        lines.append(f"  當前串流：`{active.task_id}` / `{active.character}`")
    else:
        lines.append("  當前串流：`無`")
    await ctx.send("\n".join(lines))


@bot.command(name="cancel")
async def cmd_cancel(ctx, task_id: str = ""):
    if not task_id:
        await ctx.send("用法：`!cancel <task_id>`（可用 `!queue` 查看）")
        return
    await cmd_stop(ctx, task_id=task_id)


@bot.command(name="mode")
async def cmd_mode(ctx, mode: str = ""):
    valid = {OutputMode.AUTO, OutputMode.STREAM, OutputMode.FILE}
    if mode.lower() not in valid:
        await ctx.send(f"用法：`!mode auto|stream|file`\n目前模式：`{_router.default_mode}`")
        return
    _router.default_mode = mode.lower()
    labels = {OutputMode.AUTO: "智慧切換", OutputMode.STREAM: "串流", OutputMode.FILE: "檔案"}
    await ctx.send(f"✅ 輸出模式已切換為 **{labels[mode.lower()]}**")


@bot.command(name="voices")
async def cmd_voices(ctx):
    profiles = _engine.profile_manager._profiles
    if not profiles:
        await ctx.send("⚠️ 沒有載入到任何角色設定")
        return

    lines = ["📜 **可用角色列表（17位）**："]
    for i, (name, _p) in enumerate(profiles.items(), 1):
        lines.append(f"  {i:02d}. 🎤 **{name}**")

    text = "\n".join(lines)
    if len(text) > 1900:
        mid = len(profiles) // 2
        first = list(profiles.items())[:mid]
        second = list(profiles.items())[mid:]
        t1 = "📜 **可用角色（1/2）**：\n" + "\n".join(
            f"  {i+1:02d}. 🎤 **{n}**" for i, (n, _) in enumerate(first)
        )
        t2 = "📜 **可用角色（2/2）**：\n" + "\n".join(
            f"  {i+mid+1:02d}. 🎤 **{n}**" for i, (n, _) in enumerate(second)
        )
        await ctx.send(t1)
        await ctx.send(t2)
    else:
        await ctx.send(text)


@bot.command(name="status")
async def cmd_status(ctx):
    guild_id = ctx.guild.id if ctx.guild else 0
    active = _get_active_stream(guild_id)
    snap = _engine.queue_snapshot()

    lines = [
        "**🏥 王朝語音 Bot 狀態**",
        f"  Bot：`{bot.user}`",
        f"  語音頻道：`{ctx.voice_client.channel.name if ctx.voice_client else '未連接'}`",
        f"  播放中：`{'是' if ctx.voice_client and ctx.voice_client.is_playing() else '否'}`",
        f"  輸出模式：`{_router.default_mode}`",
        f"  引擎：`常駐 VoxCPM + Edge fallback`",
        f"  模型載入：`{'是' if _engine.model_loaded else '否'}`",
        f"  佇列：`busy={snap['busy']}, waiting={snap['waiting']}`",
        f"  執行中：`{snap['active_job'] or '無'}`",
        f"  當前串流：`{(active.task_id + ' / ' + active.character) if active else '無'}`",
    ]

    try:
        import torch

        if torch.cuda.is_available():
            free, total = torch.cuda.mem_get_info()
            used_gb = (total - free) / (1024 ** 3)
            total_gb = total / (1024 ** 3)
            lines.append(f"  VRAM：`{used_gb:.1f} / {total_gb:.1f} GB`")
    except Exception:
        pass

    await ctx.send("\n".join(lines))


# Slash 指令（message content intent 失效時的保險）
@bot.tree.command(name="join", description="加入你目前所在的語音頻道")
async def slash_join(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("⚠️ 請在伺服器頻道使用 /join", ephemeral=True)
        return
    user = interaction.user
    voice_state = getattr(user, "voice", None)
    if not voice_state or not voice_state.channel:
        await interaction.response.send_message("⚠️ 你目前不在語音頻道", ephemeral=True)
        return
    channel = voice_state.channel
    vc = interaction.guild.voice_client
    if vc:
        await vc.move_to(channel)
    else:
        await channel.connect()
    await interaction.response.send_message(f"✅ 已加入 **{channel.name}**")


@bot.tree.command(name="leave", description="離開語音頻道")
async def slash_leave(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("⚠️ 請在伺服器頻道使用 /leave", ephemeral=True)
        return
    vc = interaction.guild.voice_client
    if vc:
        await vc.disconnect()
        await interaction.response.send_message("👋 已離開語音頻道")
    else:
        await interaction.response.send_message("⚠️ 目前不在語音頻道", ephemeral=True)


@bot.tree.command(name="status", description="查看語音 Bot 狀態")
async def slash_status(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("⚠️ 請在伺服器頻道使用 /status", ephemeral=True)
        return
    snap = _engine.queue_snapshot()
    vc = interaction.guild.voice_client
    lines = [
        "**🏥 王朝語音 Bot 狀態**",
        f"  Bot：`{bot.user}`",
        f"  語音頻道：`{vc.channel.name if vc else '未連接'}`",
        f"  播放中：`{'是' if vc and vc.is_playing() else '否'}`",
        f"  模型載入：`{'是' if _engine.model_loaded else '否'}`",
        f"  佇列：`busy={snap['busy']}, waiting={snap['waiting']}`",
        f"  執行中：`{snap['active_job'] or '無'}`",
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


# Phase 2 預留
@bot.command(name="morning")
async def cmd_morning(ctx, *, args: str = ""):
    if not ctx.guild:
        await ctx.send("⚠️ 請在伺服器頻道中使用 !morning")
        return
    flags = _parse_mode_flags(args)
    mode = _router.resolve(ctx, flag_file=flags["file"], flag_stream=flags["stream"])
    await _run_morning_broadcast(ctx, mode=mode, mood="沉穩", ambience="hall")


@bot.command(name="morningcron")
async def cmd_morningcron(ctx, *, args: str = ""):
    """設定早朝排程：!morningcron [on|off|status] [HH:MM] [mode] [voice_channel_id]"""
    parts = args.strip().split() if args else []
    action = parts[0].lower() if parts else "status"

    cfg = _load_morning_schedule()

    if action == "status":
        await ctx.send(
            "\n".join(
                [
                    "**⏰ 早朝排程狀態**",
                    f"  啟用：`{'是' if cfg.get('enabled') else '否'}`",
                    f"  時間：`{cfg.get('time', '07:00')} (Asia/Taipei)`",
                    f"  模式：`{cfg.get('mode', 'auto')}`",
                    f"  目標 guild：`{cfg.get('guild_id') or '未設定'}`",
                    f"  目標文字頻道：`{cfg.get('channel_id') or '未設定'}`",
                    f"  目標語音頻道：`{cfg.get('voice_channel_id') or '未設定'}`",
                    f"  上次執行：`{cfg.get('last_run_date') or '無'}`",
                ]
            )
        )
        return

    if action == "off":
        cfg["enabled"] = False
        _save_morning_schedule(cfg)
        await ctx.send("🛑 早朝排程已停用")
        return

    if action != "on":
        await ctx.send("用法：`!morningcron [on|off|status] [HH:MM] [auto|stream|file] [voice_channel_id]`")
        return

    schedule_time = parts[1] if len(parts) >= 2 else str(cfg.get("time") or "07:00")
    parsed = _parse_hhmm(schedule_time)
    if not parsed:
        await ctx.send("⚠️ 時間格式錯誤，請用 HH:MM（例：07:00）")
        return

    mode = parts[2].lower() if len(parts) >= 3 else str(cfg.get("mode") or "auto").lower()
    if mode not in {OutputMode.AUTO, OutputMode.STREAM, OutputMode.FILE}:
        await ctx.send("⚠️ mode 僅支援 auto / stream / file")
        return

    if not ctx.guild:
        await ctx.send("⚠️ 請在伺服器文字頻道設定 morning 排程")
        return

    explicit_voice_channel_id = None
    if len(parts) >= 4:
        token = parts[3].strip()
        digits = "".join(ch for ch in token if ch.isdigit())
        if not digits:
            await ctx.send("⚠️ voice_channel_id 格式錯誤，請輸入頻道 ID 或 <#channel>")
            return
        explicit_voice_channel_id = int(digits)

    resolved_voice_channel_id = explicit_voice_channel_id
    if mode == OutputMode.STREAM and resolved_voice_channel_id is None:
        if ctx.author and getattr(ctx.author, "voice", None) and ctx.author.voice and ctx.author.voice.channel:
            resolved_voice_channel_id = int(ctx.author.voice.channel.id)
        elif cfg.get("voice_channel_id"):
            resolved_voice_channel_id = int(cfg.get("voice_channel_id"))
        else:
            await ctx.send("⚠️ stream 模式需指定語音頻道：請加入語音後再下指令，或附上 voice_channel_id")
            return

    if resolved_voice_channel_id is not None:
        vc_obj = ctx.guild.get_channel(int(resolved_voice_channel_id))
        if vc_obj is None or not isinstance(vc_obj, discord.VoiceChannel):
            await ctx.send("⚠️ 指定的 voice_channel_id 不存在或不是語音頻道")
            return

    cfg.update(
        {
            "enabled": True,
            "time": schedule_time,
            "mode": mode,
            "guild_id": int(ctx.guild.id),
            "channel_id": int(ctx.channel.id),
            "voice_channel_id": resolved_voice_channel_id,
            "last_run_date": None,
        }
    )
    _save_morning_schedule(cfg)

    vc_line = f"\n🎧 語音頻道：{resolved_voice_channel_id}" if resolved_voice_channel_id else ""
    await ctx.send(
        f"✅ 早朝排程已啟用：每日 {schedule_time}（Asia/Taipei）\n"
        f"📍 目標：{ctx.guild.name} / #{ctx.channel.name}\n"
        f"🔊 模式：{mode}{vc_line}"
    )


@bot.command(name="greeting")
async def cmd_greeting(ctx, *, args: str = ""):
    if not ctx.guild:
        await ctx.send("⚠️ 請在伺服器頻道中使用 !greeting")
        return
    flags = _parse_mode_flags(args)
    mode = _router.resolve(ctx, flag_file=flags["file"], flag_stream=flags["stream"])

    try:
        await ctx.send("🎀 節日問候整備中，正在載入天機報...")
        report = await asyncio.get_running_loop().run_in_executor(None, _load_tianji_report)
        lines = _build_greeting_lines(report)

        if report.get("error"):
            await ctx.send(f"⚠️ 天機報載入異常：{report['error']}")

        for idx, (character, text) in enumerate(lines, 1):
            await ctx.send(f"🎀 [{idx}/{len(lines)}] {character} 發言中…")
            if mode == OutputMode.STREAM:
                await _synthesize_and_play_stream(ctx, character, text, mood="溫暖", ambience="hall")
            else:
                await _synthesize_and_send_file(ctx, character, text, mood="溫暖", ambience="hall")

        await ctx.send("✅ 節日問候播報完畢")
    except Exception as e:
        error_msg = f"❌ 節日問候異常：{type(e).__name__}: {e}"
        print(error_msg)
        try:
            await ctx.send(error_msg)
        except Exception:
            pass


@bot.command(name="drama")
async def cmd_drama(ctx, *, args: str = ""):
    """多人廣播劇：!drama [-f|-s] <script_path>"""
    if not ctx.guild:
        await ctx.send("⚠️ 請在伺服器頻道中使用 !drama")
        return
    if not args.strip():
        await ctx.send("用法：`!drama [-f|-s] <script_path>`")
        return

    parts = args.strip().split()
    flag_file = "-f" in parts
    flag_stream = "-s" in parts
    script_path = " ".join(p for p in parts if p not in {"-f", "-s"}).strip()

    if not script_path:
        await ctx.send("⚠️ 請提供劇本檔案路徑")
        return

    script_file = Path(script_path)
    if not script_file.exists():
        await ctx.send(f"⚠️ 劇本檔案不存在：`{script_path}`")
        return

    mode = _router.resolve(ctx, flag_file=flag_file, flag_stream=flag_stream)

    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        ts = int(time.time() * 1000)
        drama_wav = OUTPUT_DIR / f"drama_{ts}.wav"

        status_msg = await ctx.send("🎭 廣播劇生成中，請稍候...")
        result = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: _render_drama_audio(str(script_file), str(drama_wav), max_segments=30),
        )

        if not result.get("ok"):
            await status_msg.edit(content=f"❌ 廣播劇生成失敗：{result.get('reason', '未知錯誤')}")
            return

        summary = _build_drama_text_summary(result)
        transcript_chunks = _build_drama_transcript_chunks(result)
        transcript_timeline = result.get("transcript_timeline", []) or []

        if mode == OutputMode.STREAM:
            vc = await _ensure_voice_client(ctx)
            if vc is None:
                await status_msg.edit(content="⚠️ 主公不在語音頻道，請先加入或改用 `-f`")
                return

            if vc.is_playing():
                vc.stop()

            source = discord.FFmpegPCMAudio(
                str(drama_wav),
                options="-vn -f s16le -ar 48000 -ac 2",
            )
            vc.play(source)
            await status_msg.edit(content="🔊 廣播劇已開始播放")
            await ctx.send(summary)
            if transcript_timeline:
                await ctx.send("📝 **廣播劇文字同步（逐句）**")
                first = transcript_timeline[0]
                await ctx.send(f"**{first['speaker']}**：{first['text']}")
                if len(transcript_timeline) > 1:
                    shifted = []
                    base = float(transcript_timeline[1].get("start_sec", 0.0))
                    for row in transcript_timeline[1:]:
                        shifted.append({
                            "speaker": row["speaker"],
                            "text": row["text"],
                            "start_sec": max(0.0, float(row.get("start_sec", 0.0)) - base),
                        })
                    asyncio.create_task(_send_drama_transcript_timed(ctx, shifted))
        else:
            drama_mp3 = OUTPUT_DIR / f"drama_{ts}.mp3"
            ok, err = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: _convert_wav_to_mp3(str(drama_wav), str(drama_mp3)),
            )
            if not ok:
                await status_msg.edit(content=f"❌ MP3 轉檔失敗：{err}")
                return

            await status_msg.edit(content="✅ 廣播劇已生成")
            await ctx.send(summary)
            for chunk in transcript_chunks:
                await ctx.send(chunk)
            await ctx.send(
                "📎 廣播劇語音檔",
                file=discord.File(str(drama_mp3), filename=drama_mp3.name),
            )
    except Exception as e:
        error_msg = f"❌ 廣播劇異常：{type(e).__name__}: {e}"
        print(error_msg)
        try:
            await ctx.send(error_msg)
        except Exception:
            pass


def _split_weekly_paragraphs(text: str) -> list[str]:
    raw = text.replace("\r\n", "\n")
    blocks = [b.strip() for b in raw.split("\n\n") if b.strip()]
    if blocks:
        return blocks
    return [ln.strip() for ln in raw.split("\n") if ln.strip()]


def _build_weekly_segments(report_text: str) -> list[dict]:
    parts = _split_weekly_paragraphs(report_text)
    roles = ["軍師·諸葛亮", "丞相·曾國藩", "御史·魏徵", "軍師·諸葛亮"]
    heads = ["本週總覽", "里程碑進展", "風險與稽核", "下週計畫"]

    segs: list[dict] = []
    for i in range(4):
        content = parts[i] if i < len(parts) else "本段暫無資料。"
        content = content.strip()
        if not content:
            content = "本段暫無資料。"
        if heads[i] not in content:
            content = f"{heads[i]}：{content}"
        segs.append({"speaker": roles[i], "text": content})
    return segs


def _build_weekly_summary(result: dict) -> str:
    return "\n".join([
        "🗞️ **有聲週報摘要**",
        f"  來源：`{result.get('source_path', '')}`",
        f"  段數：`{result.get('segments', 0)}`",
        f"  時長：`{result.get('duration_sec', 0):.1f}s`",
    ])


def _build_weekly_transcript_chunks(result: dict, max_len: int = 1800) -> list[str]:
    lines = result.get("transcript_lines", []) or []
    if not lines:
        return []
    chunks: list[str] = []
    buf = "📝 **週報文字同步**\n"
    for row in lines:
        one = f"**{row['speaker']}**：{row['text']}\n"
        if len(buf) + len(one) > max_len:
            chunks.append(buf.rstrip())
            buf = "📝 **週報文字同步（續）**\n" + one
        else:
            buf += one
    if buf.strip():
        chunks.append(buf.rstrip())
    return chunks


def _render_weekly_audio(report_text: str, output_wav: str, source_path: str) -> dict:
    segments = _build_weekly_segments(report_text)

    profiles = _engine.profile_manager._profiles
    unknown = sorted({seg['speaker'] for seg in segments if seg['speaker'] not in profiles})
    if unknown:
        return {"ok": False, "reason": f"角色未定義：{', '.join(unknown)}"}

    sr_target = _engine.out_sample_rate
    audios: list[np.ndarray] = []
    transcript_lines: list[dict] = []
    transcript_timeline: list[dict] = []
    cursor_sec = 0.0

    with tempfile.TemporaryDirectory(prefix="wangdom_weekly_") as td:
        for i, seg in enumerate(segments):
            seg_path = Path(td) / f"weekly_{i:04d}.wav"
            res = _engine.synthesize_file(
                text=seg["text"],
                character=seg["speaker"],
                output_path=seg_path,
                mood="沉穩",
                ambience="none",
                output_format="wav",
            )
            if not res.get("ok"):
                return {"ok": False, "reason": f"第{i+1}段生成失敗：{res.get('reason', 'unknown')}"}

            audio, sr = sf.read(str(seg_path), dtype="float32")
            if audio.ndim == 2:
                audio = audio.mean(axis=1)
            if int(sr) != int(sr_target):
                audio = _resample_audio(audio, int(sr), int(sr_target))
            audio = normalize_rms(audio, target_rms=0.08)
            seg_duration = len(audio) / float(sr_target)
            audios.append(audio.astype(np.float32))

            transcript_lines.append({"speaker": seg["speaker"], "text": seg["text"]})
            transcript_timeline.append({
                "speaker": seg["speaker"],
                "text": seg["text"],
                "start_sec": round(cursor_sec, 3),
                "duration_sec": round(seg_duration, 3),
            })

            pause_sec = 0.6 if i < len(segments) - 1 else 0.0
            cursor_sec += seg_duration + pause_sec
            if pause_sec > 0:
                audios.append(np.zeros(int(sr_target * pause_sec), dtype=np.float32))

    merged = np.concatenate(audios) if audios else np.zeros(1, dtype=np.float32)
    sf.write(output_wav, merged, sr_target)

    return {
        "ok": True,
        "output_path": output_wav,
        "source_path": source_path,
        "segments": len(segments),
        "duration_sec": len(merged) / float(sr_target),
        "transcript_lines": transcript_lines,
        "transcript_timeline": transcript_timeline,
    }


@bot.command(name="weekly")
async def cmd_weekly(ctx, *, args: str = ""):
    """有聲週報：!weekly [-f|-s] <weekly_txt_path>"""
    if not ctx.guild:
        await ctx.send("⚠️ 請在伺服器頻道中使用 !weekly")
        return
    if not args.strip():
        await ctx.send("用法：`!weekly [-f|-s] <weekly_txt_path>`")
        return

    parts = args.strip().split()
    flag_file = "-f" in parts
    flag_stream = "-s" in parts
    report_path = " ".join(p for p in parts if p not in {"-f", "-s"}).strip()

    if not report_path:
        await ctx.send("⚠️ 請提供週報文字檔路徑")
        return

    report_file = Path(report_path)
    if not report_file.exists():
        await ctx.send(f"⚠️ 週報檔案不存在：`{report_path}`")
        return

    report_text = report_file.read_text(encoding="utf-8").strip()
    if not report_text:
        await ctx.send("⚠️ 週報內容為空")
        return

    mode = _router.resolve(ctx, flag_file=flag_file, flag_stream=flag_stream)

    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        ts = int(time.time() * 1000)
        weekly_wav = OUTPUT_DIR / f"weekly_{ts}.wav"

        status_msg = await ctx.send("🗞️ 週報生成中，請稍候...")
        result = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: _render_weekly_audio(report_text, str(weekly_wav), str(report_file)),
        )

        if not result.get("ok"):
            await status_msg.edit(content=f"❌ 週報生成失敗：{result.get('reason', '未知錯誤')}")
            return

        summary = _build_weekly_summary(result)
        transcript_chunks = _build_weekly_transcript_chunks(result)
        transcript_timeline = result.get("transcript_timeline", []) or []

        if mode == OutputMode.STREAM:
            vc = await _ensure_voice_client(ctx)
            if vc is None:
                await status_msg.edit(content="⚠️ 主公不在語音頻道，請先加入或改用 `-f`")
                return
            if vc.is_playing():
                vc.stop()

            source = discord.FFmpegPCMAudio(
                str(weekly_wav),
                options="-vn -f s16le -ar 48000 -ac 2",
            )
            vc.play(source)
            await status_msg.edit(content="🔊 有聲週報已開始播放")
            await ctx.send(summary)
            if transcript_timeline:
                await ctx.send("📝 **週報文字同步（逐句）**")
                first = transcript_timeline[0]
                await ctx.send(f"**{first['speaker']}**：{first['text']}")
                if len(transcript_timeline) > 1:
                    shifted = []
                    base = float(transcript_timeline[1].get("start_sec", 0.0))
                    for row in transcript_timeline[1:]:
                        shifted.append({
                            "speaker": row["speaker"],
                            "text": row["text"],
                            "start_sec": max(0.0, float(row.get("start_sec", 0.0)) - base),
                        })
                    asyncio.create_task(_send_drama_transcript_timed(ctx, shifted))
        else:
            weekly_mp3 = OUTPUT_DIR / f"weekly_{ts}.mp3"
            ok, err = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: _convert_wav_to_mp3(str(weekly_wav), str(weekly_mp3)),
            )
            if not ok:
                await status_msg.edit(content=f"❌ MP3 轉檔失敗：{err}")
                return

            await status_msg.edit(content="✅ 有聲週報已生成")
            await ctx.send(summary)
            for chunk in transcript_chunks:
                await ctx.send(chunk)
            await ctx.send("📎 有聲週報語音檔", file=discord.File(str(weekly_mp3), filename=weekly_mp3.name))
    except Exception as e:
        error_msg = f"❌ 有聲週報異常：{type(e).__name__}: {e}"
        print(error_msg)
        try:
            await ctx.send(error_msg)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 內部 API Server（Hermes ↔ 語音 Bot 互通）
# ---------------------------------------------------------------------------

_API_PORT = int(os.environ.get("VOICE_BOT_API_PORT", "8890"))
_API_ENABLED = os.environ.get("VOICE_BOT_API_ENABLED", "").lower() in ("true", "1", "yes")
_ALLOWED_SCRIPT_DIRS: list[Path] = [
    Path("/tmp"),
    OUTPUT_DIR,
    PROJECT_DIR / "scripts",
    PROJECT_DIR / "test_output",
]
_MAX_TEXT_LEN = 2000
_MAX_BODY_SIZE = 1_048_576  # 1 MB


@dataclass
class TaskRecord:
    task_id: str
    status: str            # queued | running | done | failed
    mode: str              # stream | file
    progress: str
    request_id: Optional[str]
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    output_path: Optional[str] = None
    duration_sec: Optional[float] = None
    transcript: Optional[str] = None
    segments: Optional[int] = None
    error: Optional[str] = None


_task_registry: dict[str, TaskRecord] = {}
_api_task_counter = itertools.count(1)


def _next_api_task_id() -> str:
    return f"T-{datetime.now(TW_TZ).strftime('%Y%m%d')}-{next(_api_task_counter):04d}"


def _get_primary_guild() -> Optional[discord.Guild]:
    return bot.guilds[0] if bot.guilds else None


def _validate_script_path(path_str: str) -> tuple[bool, str]:
    p = Path(path_str).resolve()
    for allowed in _ALLOWED_SCRIPT_DIRS:
        try:
            p.relative_to(allowed.resolve())
            return True, ""
        except ValueError:
            continue
    return False, f"路徑不在白名單內：{path_str}"


class _ApiCtx:
    """Minimal ctx-like adapter so API handlers can reuse existing service functions."""

    def __init__(self, guild: discord.Guild, text_channel: discord.TextChannel):
        self.guild = guild
        self._text_channel = text_channel
        self.author = None

    @property
    def voice_client(self):
        return self.guild.voice_client

    async def send(self, content=None, **kwargs):
        return await self._text_channel.send(content, **kwargs)


# ── API Handlers ─────────────────────────────────────────────────────────


async def _api_health(_request):
    """GET /status — Bot 全域健康"""
    return aioweb.json_response({
        "ok": True,
        "model_loaded": _engine.model_loaded,
        "busy": _engine.busy,
        "queue": _engine.queue_snapshot(),
        "active_streams": {
            str(gid): {"task_id": s.task_id, "character": s.character}
            for gid, s in _active_streams.items()
        },
    })


async def _api_task_status(request):
    """GET /status/{task_id} — 單一任務輪詢"""
    task_id = request.match_info["task_id"]
    rec = _task_registry.get(task_id)
    if not rec:
        return aioweb.json_response(
            {"ok": False, "error": f"task_id '{task_id}' not found"}, status=404,
        )
    resp: dict = {
        "ok": True,
        "task_id": rec.task_id,
        "status": rec.status,
        "progress": rec.progress,
        "started_at": rec.started_at,
        "finished_at": rec.finished_at,
        "error": rec.error,
    }
    if rec.status in ("done", "failed"):
        resp["duration_sec"] = rec.duration_sec
        resp["transcript"] = rec.transcript
        if rec.output_path:
            resp["output_path"] = rec.output_path
        if rec.segments is not None:
            resp["segments"] = rec.segments
    return aioweb.json_response(resp)


async def _api_say(request):
    """POST /say — 單句語音"""
    try:
        body = await request.json()
    except Exception:
        return aioweb.json_response({"ok": False, "error": "invalid JSON"}, status=400)

    mode = body.get("mode", "file")
    character = body.get("character", "")
    text = body.get("text", "")
    text_ch_id = body.get("text_channel_id")
    voice_ch_id = body.get("voice_channel_id")
    mood = body.get("mood")
    ambience = body.get("ambience", "none")
    request_id = body.get("request_id")

    if not character or not text:
        return aioweb.json_response({"ok": False, "error": "缺少 character 或 text"}, status=400)
    if len(text) > _MAX_TEXT_LEN:
        return aioweb.json_response(
            {"ok": False, "error": f"文字超過 {_MAX_TEXT_LEN} 字上限"}, status=400,
        )

    guild = _get_primary_guild()
    if not guild:
        return aioweb.json_response({"ok": False, "error": "Bot 不在任何伺服器"}, status=503)
    text_ch = bot.get_channel(int(text_ch_id)) if text_ch_id else None
    if not text_ch:
        return aioweb.json_response({"ok": False, "error": "找不到 text_channel_id"}, status=400)

    ctx = _ApiCtx(guild, text_ch)
    task_id = _next_api_task_id()
    now = datetime.now(TW_TZ).isoformat()

    if mode == "stream":
        rec = TaskRecord(
            task_id=task_id, status="queued", mode="stream",
            progress="0/1", request_id=request_id,
            created_at=now, transcript=text,
        )
        _task_registry[task_id] = rec

        async def _run():
            rec.status = "running"
            rec.started_at = datetime.now(TW_TZ).isoformat()
            try:
                ok = await _synthesize_and_play_stream(
                    ctx, character, text, mood=mood, ambience=ambience,
                    preferred_voice_channel_id=int(voice_ch_id) if voice_ch_id else None,
                )
                if ok:
                    rec.status = "done"
                    rec.progress = "1/1"
                else:
                    rec.status = "failed"
                    rec.error = "找不到可用語音頻道或已有串流進行中"
            except Exception as e:
                rec.status = "failed"
                rec.error = str(e)
            finally:
                rec.finished_at = datetime.now(TW_TZ).isoformat()

        asyncio.create_task(_run())
        return aioweb.json_response({
            "ok": True, "mode": "stream", "task_id": task_id,
            "status": "queued", "request_id": request_id, "error": None,
        })

    # ── file mode (synchronous) ──
    rec = TaskRecord(
        task_id=task_id, status="running", mode="file",
        progress="generating", request_id=request_id,
        created_at=now, started_at=now, transcript=text,
    )
    _task_registry[task_id] = rec

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time() * 1000)
    out_path = OUTPUT_DIR / f"say_api_{ts}.mp3"

    result = await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: _engine.synthesize_file(
            text=text, character=character, output_path=out_path,
            mood=mood, ambience=ambience, output_format="mp3",
        ),
    )

    if not result.get("ok"):
        rec.status = "failed"
        rec.error = result.get("reason", "未知錯誤")
        rec.finished_at = datetime.now(TW_TZ).isoformat()
        return aioweb.json_response(
            {"ok": False, "task_id": task_id, "error": rec.error}, status=500,
        )

    rec.status = "done"
    rec.progress = "1/1"
    rec.output_path = result["output_path"]
    rec.duration_sec = result.get("duration_sec")
    rec.segments = 1
    rec.finished_at = datetime.now(TW_TZ).isoformat()

    return aioweb.json_response({
        "ok": True, "mode": "file", "task_id": task_id, "status": "done",
        "request_id": request_id, "output_path": rec.output_path,
        "duration_sec": rec.duration_sec, "segments": 1,
        "transcript": text, "error": None,
    })


async def _api_drama(request):
    """POST /drama — 多角色廣播劇"""
    try:
        body = await request.json()
    except Exception:
        return aioweb.json_response({"ok": False, "error": "invalid JSON"}, status=400)

    mode = body.get("mode", "stream")
    script_path = body.get("script_path", "")
    text_ch_id = body.get("text_channel_id")
    voice_ch_id = body.get("voice_channel_id")
    request_id = body.get("request_id")

    if not script_path:
        return aioweb.json_response({"ok": False, "error": "缺少 script_path"}, status=400)

    ok_path, err_path = _validate_script_path(script_path)
    if not ok_path:
        return aioweb.json_response({"ok": False, "error": err_path}, status=403)

    script_file = Path(script_path)
    if not script_file.exists():
        return aioweb.json_response(
            {"ok": False, "error": f"劇本不存在：{script_path}"}, status=400,
        )

    guild = _get_primary_guild()
    if not guild:
        return aioweb.json_response({"ok": False, "error": "Bot 不在任何伺服器"}, status=503)
    text_ch = bot.get_channel(int(text_ch_id)) if text_ch_id else None
    if not text_ch:
        return aioweb.json_response({"ok": False, "error": "找不到 text_channel_id"}, status=400)

    ctx = _ApiCtx(guild, text_ch)
    task_id = _next_api_task_id()
    now = datetime.now(TW_TZ).isoformat()

    rec = TaskRecord(
        task_id=task_id, status="queued", mode=mode,
        progress="0/0", request_id=request_id, created_at=now,
    )
    _task_registry[task_id] = rec

    async def _run():
        rec.status = "running"
        rec.started_at = datetime.now(TW_TZ).isoformat()
        try:
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            ts = int(time.time() * 1000)
            drama_wav = OUTPUT_DIR / f"drama_api_{ts}.wav"

            result = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: _render_drama_audio(str(script_file), str(drama_wav), max_segments=30),
            )

            if not result.get("ok"):
                rec.status = "failed"
                rec.error = result.get("reason", "渲染失敗")
                rec.finished_at = datetime.now(TW_TZ).isoformat()
                return

            seg_count = result.get("total_segments", 0)
            rec.progress = f"{seg_count}/{seg_count}"
            rec.duration_sec = result.get("duration_sec")
            t_lines = result.get("transcript_lines", [])
            rec.transcript = "\n".join(
                f"{l['speaker']}：{l['text']}" if isinstance(l, dict) else str(l)
                for l in t_lines
            ) if t_lines else ""
            rec.segments = seg_count

            summary = _build_drama_text_summary(result)

            if mode == "stream":
                vc = await _ensure_voice_client(
                    ctx,
                    preferred_voice_channel_id=int(voice_ch_id) if voice_ch_id else None,
                )
                if vc is None:
                    rec.status = "failed"
                    rec.error = "找不到語音頻道"
                    rec.finished_at = datetime.now(TW_TZ).isoformat()
                    return
                if vc.is_playing():
                    vc.stop()
                source_audio = discord.FFmpegPCMAudio(
                    str(drama_wav), options="-vn -f s16le -ar 48000 -ac 2",
                )
                vc.play(source_audio)
                await ctx.send("🎭 廣播劇播放中（API 觸發）")
                await ctx.send(summary)
                while vc.is_playing():
                    await asyncio.sleep(0.5)
            else:
                drama_mp3 = OUTPUT_DIR / f"drama_api_{ts}.mp3"
                ok_conv, err_conv = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: _convert_wav_to_mp3(str(drama_wav), str(drama_mp3)),
                )
                if not ok_conv:
                    rec.status = "failed"
                    rec.error = f"MP3 轉檔失敗：{err_conv}"
                    rec.finished_at = datetime.now(TW_TZ).isoformat()
                    return
                rec.output_path = str(drama_mp3)
                await ctx.send(summary)

            rec.status = "done"
            rec.finished_at = datetime.now(TW_TZ).isoformat()

        except Exception as e:
            rec.status = "failed"
            rec.error = str(e)
            rec.finished_at = datetime.now(TW_TZ).isoformat()

    asyncio.create_task(_run())
    return aioweb.json_response({
        "ok": True, "mode": mode, "task_id": task_id,
        "status": "queued", "request_id": request_id, "error": None,
    })


async def _api_stop(request):
    """POST /stop — 中止指定或當前任務"""
    try:
        body = await request.json()
    except Exception:
        body = {}

    task_id = body.get("task_id", "")
    guild = _get_primary_guild()
    if not guild:
        return aioweb.json_response({"ok": False, "error": "Bot 不在任何伺服器"}, status=503)

    for gid, stream in _active_streams.items():
        if not task_id or stream.task_id == task_id:
            stream.stop_event.set()
            rec = _task_registry.get(stream.task_id)
            if rec:
                rec.status = "failed"
                rec.error = "手動中止（API /stop）"
                rec.finished_at = datetime.now(TW_TZ).isoformat()
            return aioweb.json_response({"ok": True, "stopped": stream.task_id})

    return aioweb.json_response(
        {"ok": False, "error": f"找不到進行中的任務：{task_id}"}, status=404,
    )


async def _api_morning(request):
    """POST /morning — 早朝廣播"""
    try:
        body = await request.json()
    except Exception:
        return aioweb.json_response({"ok": False, "error": "invalid JSON"}, status=400)

    mode = body.get("mode", "stream")
    text_ch_id = body.get("text_channel_id")
    voice_ch_id = body.get("voice_channel_id")
    request_id = body.get("request_id")

    guild = _get_primary_guild()
    if not guild:
        return aioweb.json_response({"ok": False, "error": "Bot 不在任何伺服器"}, status=503)
    text_ch = bot.get_channel(int(text_ch_id)) if text_ch_id else None
    if not text_ch:
        return aioweb.json_response({"ok": False, "error": "找不到 text_channel_id"}, status=400)

    task_id = _next_api_task_id()
    now = datetime.now(TW_TZ).isoformat()
    rec = TaskRecord(
        task_id=task_id, status="queued", mode=mode,
        progress="0/0", request_id=request_id, created_at=now,
    )
    _task_registry[task_id] = rec

    async def _run():
        rec.status = "running"
        rec.started_at = datetime.now(TW_TZ).isoformat()
        try:
            await _run_morning_broadcast(
                _ApiCtx(guild, text_ch), mode=mode, mood="沉穩", ambience="hall",
                preferred_voice_channel_id=int(voice_ch_id) if voice_ch_id else None,
            )
            rec.status = "done"
        except Exception as e:
            rec.status = "failed"
            rec.error = str(e)
        finally:
            rec.finished_at = datetime.now(TW_TZ).isoformat()

    asyncio.create_task(_run())
    return aioweb.json_response({
        "ok": True, "task_id": task_id, "status": "queued",
        "request_id": request_id, "error": None,
    })


async def _api_greeting(request):
    """POST /greeting — 節慶問候"""
    try:
        body = await request.json()
    except Exception:
        return aioweb.json_response({"ok": False, "error": "invalid JSON"}, status=400)

    mode = body.get("mode", "stream")
    text_ch_id = body.get("text_channel_id")
    voice_ch_id = body.get("voice_channel_id")
    request_id = body.get("request_id")

    guild = _get_primary_guild()
    if not guild:
        return aioweb.json_response({"ok": False, "error": "Bot 不在任何伺服器"}, status=503)
    text_ch = bot.get_channel(int(text_ch_id)) if text_ch_id else None
    if not text_ch:
        return aioweb.json_response({"ok": False, "error": "找不到 text_channel_id"}, status=400)

    ctx = _ApiCtx(guild, text_ch)
    task_id = _next_api_task_id()
    now = datetime.now(TW_TZ).isoformat()
    rec = TaskRecord(
        task_id=task_id, status="queued", mode=mode,
        progress="0/0", request_id=request_id, created_at=now,
    )
    _task_registry[task_id] = rec

    async def _run():
        rec.status = "running"
        rec.started_at = datetime.now(TW_TZ).isoformat()
        try:
            await ctx.send("🎀 節日問候整備中（API 觸發）...")
            report = await asyncio.get_running_loop().run_in_executor(None, _load_tianji_report)
            lines = _build_greeting_lines(report)
            for idx, (character, text) in enumerate(lines, 1):
                rec.progress = f"{idx}/{len(lines)}"
                if mode == "stream":
                    await _synthesize_and_play_stream(
                        ctx, character, text, mood="溫暖", ambience="hall",
                        preferred_voice_channel_id=int(voice_ch_id) if voice_ch_id else None,
                    )
                else:
                    await _synthesize_and_send_file(ctx, character, text, mood="溫暖", ambience="hall")
            await ctx.send("✅ 節日問候播報完畢")
            rec.status = "done"
        except Exception as e:
            rec.status = "failed"
            rec.error = str(e)
        finally:
            rec.finished_at = datetime.now(TW_TZ).isoformat()

    asyncio.create_task(_run())
    return aioweb.json_response({
        "ok": True, "task_id": task_id, "status": "queued",
        "request_id": request_id, "error": None,
    })


async def _api_weekly(request):
    """POST /weekly — 週報播報"""
    try:
        body = await request.json()
    except Exception:
        return aioweb.json_response({"ok": False, "error": "invalid JSON"}, status=400)

    mode = body.get("mode", "stream")
    text_ch_id = body.get("text_channel_id")
    voice_ch_id = body.get("voice_channel_id")
    source_path = body.get("source_path", "")
    request_id = body.get("request_id")

    if not source_path:
        return aioweb.json_response({"ok": False, "error": "缺少 source_path"}, status=400)

    guild = _get_primary_guild()
    if not guild:
        return aioweb.json_response({"ok": False, "error": "Bot 不在任何伺服器"}, status=503)
    text_ch = bot.get_channel(int(text_ch_id)) if text_ch_id else None
    if not text_ch:
        return aioweb.json_response({"ok": False, "error": "找不到 text_channel_id"}, status=400)

    ctx = _ApiCtx(guild, text_ch)
    task_id = _next_api_task_id()
    now = datetime.now(TW_TZ).isoformat()
    rec = TaskRecord(
        task_id=task_id, status="queued", mode=mode,
        progress="0/0", request_id=request_id, created_at=now,
    )
    _task_registry[task_id] = rec

    async def _run():
        rec.status = "running"
        rec.started_at = datetime.now(TW_TZ).isoformat()
        try:
            source_file = Path(source_path)
            if not source_file.exists():
                rec.status = "failed"
                rec.error = f"來源檔案不存在：{source_path}"
                rec.finished_at = datetime.now(TW_TZ).isoformat()
                return

            report_text = source_file.read_text(encoding="utf-8")

            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            ts = int(time.time() * 1000)
            weekly_wav = OUTPUT_DIR / f"weekly_api_{ts}.wav"

            result = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: _render_weekly_audio(report_text, str(weekly_wav), source_path),
            )

            if not result.get("ok"):
                rec.status = "failed"
                rec.error = result.get("reason", "渲染失敗")
                rec.finished_at = datetime.now(TW_TZ).isoformat()
                return

            seg_count = len(result.get("segments", []))
            rec.progress = f"{seg_count}/{seg_count}"
            rec.duration_sec = result.get("duration_sec")
            rec.segments = seg_count

            summary = _build_weekly_summary(result)
            transcript_chunks = _build_weekly_transcript_chunks(result)
            rec.transcript = "\n".join(transcript_chunks)

            if mode == "stream":
                vc = await _ensure_voice_client(
                    ctx,
                    preferred_voice_channel_id=int(voice_ch_id) if voice_ch_id else None,
                )
                if vc is None:
                    rec.status = "failed"
                    rec.error = "找不到語音頻道"
                    rec.finished_at = datetime.now(TW_TZ).isoformat()
                    return
                if vc.is_playing():
                    vc.stop()
                source_audio = discord.FFmpegPCMAudio(
                    str(weekly_wav), options="-vn -f s16le -ar 48000 -ac 2",
                )
                vc.play(source_audio)
                await ctx.send("📰 週報播放中（API 觸發）")
                await ctx.send(summary)
                while vc.is_playing():
                    await asyncio.sleep(0.5)
            else:
                weekly_mp3 = OUTPUT_DIR / f"weekly_api_{ts}.mp3"
                ok_conv, err_conv = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: _convert_wav_to_mp3(str(weekly_wav), str(weekly_mp3)),
                )
                if not ok_conv:
                    rec.status = "failed"
                    rec.error = f"MP3 轉檔失敗：{err_conv}"
                    rec.finished_at = datetime.now(TW_TZ).isoformat()
                    return
                rec.output_path = str(weekly_mp3)
                await ctx.send(summary)

            rec.status = "done"
            rec.finished_at = datetime.now(TW_TZ).isoformat()

        except Exception as e:
            rec.status = "failed"
            rec.error = str(e)
            rec.finished_at = datetime.now(TW_TZ).isoformat()

    asyncio.create_task(_run())
    return aioweb.json_response({
        "ok": True, "task_id": task_id, "status": "queued",
        "request_id": request_id, "error": None,
    })


# ── API App Factory & Startup ────────────────────────────────────────────


def _create_api_app():
    """Build the aiohttp Application with all routes."""
    app = aioweb.Application(client_max_size=_MAX_BODY_SIZE)
    app.router.add_get("/status", _api_health)
    app.router.add_get("/status/{task_id}", _api_task_status)
    app.router.add_post("/say", _api_say)
    app.router.add_post("/drama", _api_drama)
    app.router.add_post("/morning", _api_morning)
    app.router.add_post("/greeting", _api_greeting)
    app.router.add_post("/weekly", _api_weekly)
    app.router.add_post("/stop", _api_stop)
    return app


async def _start_api_server():
    """Start the internal API server (called from on_ready)."""
    if not _HAS_AIOHTTP:
        print("   ⚠️ aiohttp 未安裝，內部 API 已跳過")
        return
    app = _create_api_app()
    runner = aioweb.AppRunner(app)
    await runner.setup()
    site = aioweb.TCPSite(runner, "127.0.0.1", _API_PORT)
    await site.start()
    print(f"   🔌 內部 API 已啟動：http://127.0.0.1:{_API_PORT}")


# ---------------------------------------------------------------------------
# 啟動
# ---------------------------------------------------------------------------
def main():
    token = os.environ.get("VOICE_BOT_TOKEN")
    if not token:
        print("❌ 請設定 VOICE_BOT_TOKEN（.env 或環境變數）")
        sys.exit(1)

    print("🚀 崴勝王朝語音 Bot 啟動中...")
    print("📦 預先載入 VoxCPM 模型（常駐）...")
    _engine.load_model()
    bot.run(token)


if __name__ == "__main__":
    main()
