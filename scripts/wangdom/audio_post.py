#!/usr/bin/env python3
"""王朝音訊後處理器

處理 VoxCPM 原生不支援的效果：
  - ambience_profile（環境音場）：reverb / EQ / BGM ducking
  - 音量正規化
  - 格式轉換（WAV → MP3 / OGG）

ambience_profile enum：
  - none     : 不處理（預設）
  - studio   : 輕微壓縮 + 提升，模擬錄音室乾淨聲
  - hall     : 殘響（reverb），模擬大殿/朝堂
  - battle   : 低頻增強 + 輕微失真，模擬戰場氛圍
  - rain     : 疊加雨聲（需素材或合成），ducking
  - cave     : 長殘響 + 低頻衰減，模擬洞穴/密室

實作策略：
  - 純 NumPy + SciPy 實作（無額外依賴）
  - Reverb 用簡單的 delay + decay 模型（Schroeder reverb 簡化版）
  - BGM ducking 暫用增益控制（無 BGM 素材時跳過）
"""

from __future__ import annotations

import argparse
import json
import subprocess
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import soundfile as sf


# ---------------------------------------------------------------------------
# 音量正規化
# ---------------------------------------------------------------------------

def normalize_rms(audio: np.ndarray, target_rms: float = 0.08) -> np.ndarray:
    """RMS 正規化，防削波"""
    if len(audio) == 0:
        return audio
    current_rms = np.sqrt(np.mean(audio ** 2))
    if current_rms < 1e-6:
        return audio
    gain = target_rms / current_rms
    peak = np.max(np.abs(audio))
    max_gain = 0.95 / peak if peak > 1e-6 else gain
    return audio * min(gain, max_gain)


# ---------------------------------------------------------------------------
# Reverb（Schroeder 簡化版）
# ---------------------------------------------------------------------------

def _apply_reverb(
    audio: np.ndarray,
    sample_rate: int,
    room_scale: float = 0.5,
    decay: float = 0.3,
    wet_mix: float = 0.3,
) -> np.ndarray:
    """簡易 reverb 效果

    Args:
        audio: 輸入音訊（float32 mono）
        sample_rate: 取樣率
        room_scale: 房間大小（0.0-1.0）→ 影響 delay 時間
        decay: 衰減量（0.0-1.0）→ 影響迴響長度
        wet_mix: 濕訊號混合比例（0.0-1.0）
    """
    if len(audio) == 0:
        return audio

    # 基於 room_scale 的 delay taps（毫秒）
    base_ms = 20 + room_scale * 80  # 20-100ms
    taps_ms = [base_ms, base_ms * 1.41, base_ms * 1.73, base_ms * 2.0]

    wet = np.zeros_like(audio)
    for i, tap_ms in enumerate(taps_ms):
        delay_samples = int(tap_ms * sample_rate / 1000)
        tap_decay = decay ** (i + 1)
        if delay_samples < len(audio):
            padded = np.pad(audio, (delay_samples, 0), mode='constant')[:len(audio)]
            wet += padded * tap_decay

    # 疊加濕訊號
    return audio * (1.0 - wet_mix) + wet * wet_mix


# ---------------------------------------------------------------------------
# EQ（簡易頻段增益）
# ---------------------------------------------------------------------------

def _apply_eq(
    audio: np.ndarray,
    sample_rate: int,
    low_gain: float = 1.0,
    mid_gain: float = 1.0,
    high_gain: float = 1.0,
) -> np.ndarray:
    """簡易三頻段增益（用於 ambience 模擬）

    透過 simple moving average 區分低/中/高頻。
    """
    if len(audio) == 0:
        return audio

    from scipy.signal import butter, sosfilt

    result = np.zeros_like(audio)

    # Low: 0-300Hz
    if low_gain != 1.0:
        sos = butter(2, 300 / (sample_rate / 2), btype='low', output='sos')
        result += sosfilt(sos, audio) * low_gain
    else:
        sos = butter(2, 300 / (sample_rate / 2), btype='low', output='sos')
        result += sosfilt(sos, audio)

    # Mid: 300-3000Hz
    sos = butter(2, [300 / (sample_rate / 2), 3000 / (sample_rate / 2)], btype='band', output='sos')
    result += sosfilt(sos, audio) * mid_gain

    # High: 3000Hz+
    if high_gain != 1.0:
        sos = butter(2, 3000 / (sample_rate / 2), btype='high', output='sos')
        result += sosfilt(sos, audio) * high_gain
    else:
        sos = butter(2, 3000 / (sample_rate / 2), btype='high', output='sos')
        result += sosfilt(sos, audio)

    # 防削波
    peak = np.max(np.abs(result))
    if peak > 0.95:
        result *= 0.95 / peak

    return result.astype(np.float32)


# ---------------------------------------------------------------------------
# Ambience Profiles
# ---------------------------------------------------------------------------

_AMBIENCE_CONFIGS = {
    "none": {},
    "studio": {
        "eq": {"low_gain": 0.9, "mid_gain": 1.1, "high_gain": 1.05},
    },
    "hall": {
        "reverb": {"room_scale": 0.8, "decay": 0.4, "wet_mix": 0.35},
        "eq": {"low_gain": 1.1, "mid_gain": 1.0, "high_gain": 0.85},
    },
    "battle": {
        "reverb": {"room_scale": 0.3, "decay": 0.2, "wet_mix": 0.15},
        "eq": {"low_gain": 1.3, "mid_gain": 1.1, "high_gain": 0.8},
    },
    "rain": {
        "reverb": {"room_scale": 0.2, "decay": 0.15, "wet_mix": 0.1},
        "eq": {"low_gain": 0.85, "mid_gain": 1.0, "high_gain": 0.9},
    },
    "cave": {
        "reverb": {"room_scale": 1.0, "decay": 0.6, "wet_mix": 0.45},
        "eq": {"low_gain": 1.2, "mid_gain": 0.9, "high_gain": 0.7},
    },
}


def apply_ambience(
    audio: np.ndarray,
    sample_rate: int,
    ambience_profile: str = "none",
) -> np.ndarray:
    """套用 ambience 後處理效果

    Args:
        audio: 輸入音訊（float32 mono）
        sample_rate: 取樣率
        ambience_profile: 環境音場設定（none/studio/hall/battle/rain/cave）
    """
    config = _AMBIENCE_CONFIGS.get(ambience_profile, {})
    if not config:
        return audio

    result = audio.copy()

    # 1. Reverb
    reverb_cfg = config.get("reverb")
    if reverb_cfg:
        result = _apply_reverb(result, sample_rate, **reverb_cfg)

    # 2. EQ
    eq_cfg = config.get("eq")
    if eq_cfg:
        result = _apply_eq(result, sample_rate, **eq_cfg)

    return result


# ---------------------------------------------------------------------------
# 格式轉換
# ---------------------------------------------------------------------------

def convert_format(input_path: str | Path, output_path: str | Path) -> Optional[str]:
    """使用 ffmpeg 轉換音訊格式

    Returns:
        成功時回傳 output_path 字串，失敗回傳 None
    """
    if not shutil.which("ffmpeg"):
        return None

    try:
        result = subprocess.run(
            ["ffmpeg", "-i", str(input_path), "-y", "-loglevel", "error", str(output_path)],
            capture_output=True, timeout=30,
        )
        if result.returncode == 0 and Path(output_path).exists():
            return str(output_path)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# 高階 API
# ---------------------------------------------------------------------------

def post_process(
    input_path: str | Path,
    output_path: str | Path,
    ambience_profile: str = "none",
    normalize: float = 0.08,
) -> Dict[str, Any]:
    """音訊後處理主流程

    Args:
        input_path: 輸入音檔路徑
        output_path: 輸出路徑
        ambience_profile: 環境音場
        normalize: 目標 RMS（0 = 不做正規化）

    Returns:
        結果 dict
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    audio, sr = sf.read(str(input_path), dtype="float32")
    if audio.ndim == 2:
        audio = audio.mean(axis=1)

    # 1. 音量正規化
    if normalize > 0:
        audio = normalize_rms(audio, normalize)

    # 2. Ambience 效果
    if ambience_profile != "none":
        audio = apply_ambience(audio, sr, ambience_profile)

    # 3. 寫出
    sf.write(str(output_path), audio, sr)

    return {
        "ok": True,
        "input_path": str(input_path),
        "output_path": str(output_path),
        "sample_rate": sr,
        "duration_sec": round(len(audio) / sr, 2),
        "ambience": ambience_profile,
        "normalized": normalize > 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="王朝音訊後處理器")
    parser.add_argument("input", help="輸入音檔路徑")
    parser.add_argument("--output", "-o", help="輸出路徑（預設覆蓋原檔）")
    parser.add_argument("--ambience", default="none",
                       choices=["none", "studio", "hall", "battle", "rain", "cave"],
                       help="環境音場設定")
    parser.add_argument("--normalize", type=float, default=0.08, help="目標 RMS（0=不做）")
    args = parser.parse_args()

    output = args.output or args.input
    result = post_process(args.input, output, args.ambience, args.normalize)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
