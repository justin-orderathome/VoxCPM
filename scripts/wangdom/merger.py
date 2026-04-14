#!/usr/bin/env python3
"""王朝多人語音合併器

接收 dialogue_parser 剖析出的 DialogueSegment 列表，
逐段呼叫 VoxCpmSkill 生成語音，加間隙靜音後合併為單一音檔。

流程：
  1. 讀取 segments（JSON 或直接傳入 list）
  2. 逐段生成 WAV（經由 voxcpm_skill.py 的 TTS Router）
  3. 段間插入靜音（pause_after 秒）
  4. 合併為單一輸出檔案

輸出格式：WAV（後續可由 audio_post.py 再處理）
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import soundfile as sf

# 同目錄 import
sys.path.insert(0, str(Path(__file__).parent))
from dialogue_parser import DialogueParser, DialogueSegment, segments_to_json
from voxcpm_skill import VoxCpmSkill, VoiceProfile


def _generate_silence(duration_sec: float, sample_rate: int) -> np.ndarray:
    """生成靜音段"""
    n_samples = int(sample_rate * duration_sec)
    return np.zeros(n_samples, dtype=np.float32)


def merge_dialogue(
    segments: List[DialogueSegment],
    output_path: str | Path,
    skill: VoxCpmSkill,
    sample_rate: int | None = None,
    normalize_rms: float = 0.08,
) -> Dict[str, Any]:
    """逐段生成語音並合併為單一音檔。

    Args:
        segments: 對話段列表
        output_path: 最終輸出路徑
        skill: VoxCpmSkill 實例（含 Router）
        sample_rate: 輸出取樣率（若 None，從模型取得）
        normalize_rms: 目標 RMS（音量正規化），0 表示不做

    Returns:
        結果 dict（含各段資訊 + 最終輸出路徑）
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # 確保模型已載入（取 sample_rate）
    if sample_rate is None:
        model = skill._load_model()
        sample_rate = int(model.tts_model.sample_rate)

    collected: List[np.ndarray] = []
    segment_info: List[Dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="wangdom_merge_") as tmp_dir:
        for i, seg in enumerate(segments):
            seg_path = Path(tmp_dir) / f"seg_{i:04d}.wav"

            # 委託 VoxCpmSkill 生成（含 Router + fallback）
            result = skill.synthesize(
                text=seg.text,
                character=seg.speaker,
                output_path=str(seg_path),
            )

            if not result.get("ok", False):
                segment_info.append({
                    "index": i,
                    "speaker": seg.speaker,
                    "status": "failed",
                    "error": result.get("reason", "unknown"),
                })
                # 插入 1 秒靜音代替失敗段
                collected.append(_generate_silence(1.0, sample_rate))
                continue

            # 讀取生成的音檔
            audio, sr = sf.read(str(seg_path), dtype="float32")

            # 音量正規化
            if normalize_rms > 0 and len(audio) > 0:
                current_rms = np.sqrt(np.mean(audio ** 2))
                if current_rms > 1e-6:
                    gain = normalize_rms / current_rms
                    peak = np.max(np.abs(audio))
                    max_gain = 0.95 / peak if peak > 1e-6 else gain
                    audio = audio * min(gain, max_gain)

            collected.append(audio)

            segment_info.append({
                "index": i,
                "speaker": seg.speaker,
                "status": "ok",
                "engine": result.get("engine"),
                "duration_sec": len(audio) / sample_rate,
            })

            # 段間靜音
            if seg.pause_after > 0 and i < len(segments) - 1:
                silence = _generate_silence(seg.pause_after, sample_rate)
                collected.append(silence)

    # 合併所有段
    if not collected:
        return {"ok": False, "error": "no audio segments generated"}

    merged = np.concatenate(collected)

    # 寫出最終檔案
    sf.write(str(output_path), merged, sample_rate)

    total_duration = len(merged) / sample_rate

    return {
        "ok": True,
        "output_path": str(output_path),
        "sample_rate": sample_rate,
        "total_duration_sec": round(total_duration, 2),
        "total_segments": len(segments),
        "successful_segments": sum(1 for s in segment_info if s["status"] == "ok"),
        "failed_segments": sum(1 for s in segment_info if s["status"] == "failed"),
        "segments": segment_info,
    }


def merge_from_script(
    script_path: str | Path,
    output_path: str | Path,
    profiles_path: str | Path = "profiles/voice_profiles.yaml",
) -> Dict[str, Any]:
    """從劇本檔案直接生成多人語音（一站式介面）

    Args:
        script_path: 劇本檔案路徑
        output_path: 輸出音檔路徑
        profiles_path: voice profiles 路徑
    """
    parser = DialogueParser()
    segments = parser.parse_file(script_path)

    if not segments:
        return {"ok": False, "error": "no dialogue segments found in script"}

    skill = VoxCpmSkill(profiles_path=profiles_path)
    return merge_dialogue(segments, output_path, skill)


def main() -> None:
    parser = argparse.ArgumentParser(description="王朝多人語音合併器")
    parser.add_argument("input", help="劇本檔案路徑")
    parser.add_argument("--output", "-o", required=True, help="輸出音檔路徑")
    parser.add_argument("--profiles", default="profiles/voice_profiles.yaml", help="voice profiles 路徑")
    args = parser.parse_args()

    result = merge_from_script(args.input, args.output, args.profiles)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
