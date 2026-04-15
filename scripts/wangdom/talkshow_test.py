#!/usr/bin/env python3
"""脫口秀語音生成 + 合併為單一 WAV
用法：python talkshow_test.py
輸出：/tmp/wangdom_talkshow.wav
"""
import sys
import time
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

# 同目錄 import
sys.path.insert(0, str(Path(__file__).parent))
from voxcpm_skill import VoxCpmSkill

# 脫口秀腳本
SEGMENTS = [
    {"speaker": "待詔·唐伯虎", "text": "各位觀眾！今晚伯虎給大家說段新鮮的。咱們王朝啊，最近搞了個語音系統，說是每個大臣都有了自己的專屬聲音！"},
    {"speaker": "禮部·紀曉嵐", "text": "嗯……說得好像很厲害。"},
    {"speaker": "待詔·唐伯虎", "text": "厲害是厲害！就是每次跑起來，那顯卡風扇呼呼地轉，我以為工部李冰大人在後院開飛機呢！"},
    {"speaker": "禮部·紀曉嵐", "text": "呵，你那叫藝術？包大人上次查案，聞到焦味以為機房著火了，結果是你讓軍師一口氣跑了十七個角色的語音。"},
    {"speaker": "待詔·唐伯虎", "text": "十七個！一個晚上搞定！你紀曉嵐的聲音也在裡面，你知道嗎？"},
    {"speaker": "禮部·紀曉嵐", "text": "我當然知道，clone 得還挺像。現在連我自己都分不清，到底哪個是本尊了。"},
    {"speaker": "待詔·唐伯虎", "text": "哈哈哈哈！好了好了，今天測試串流播放，不囉嗦了。主公，語音送上，祝您今晚愉快！"},
]

OUTPUT = "/tmp/wangdom_talkshow.wav"
PAUSE_SEC = 0.6  # 段間靜音

def main():
    t0 = time.time()
    print(f"🚀 初始化 VoxCpmSkill ...")
    skill = VoxCpmSkill()

    sample_rate = None
    collected = []
    seg_info = []

    with tempfile.TemporaryDirectory(prefix="talkshow_") as tmp:
        for i, seg in enumerate(SEGMENTS):
            # 用 .wav 副檔名 + format wav 確保輸出 WAV
            out_path = str(Path(tmp) / f"seg_{i:03d}.wav")
            print(f"  [{i+1}/{len(SEGMENTS)}] {seg['speaker']}: {seg['text'][:30]}...")
            t1 = time.time()

            result = skill.synthesize(
                text=seg["text"],
                character=seg["speaker"],
                output_path=out_path,
                output_format="wav",  # 強制 WAV 輸出
            )

            dt = time.time() - t1
            ok = result.get("ok", False)
            engine = result.get("engine", "?")
            actual_path = result.get("output_path", out_path)
            print(f"    → ok={ok} ({engine}, {dt:.1f}s) path={actual_path}")

            if not ok:
                print(f"    ⚠️ 失敗: {result}")
                continue

            # 讀取生成的音頻
            if not Path(actual_path).exists():
                # 可能是 mp3，嘗試轉換
                mp3_path = actual_path.replace(".wav", ".mp3")
                if Path(mp3_path).exists():
                    import subprocess
                    subprocess.run(["ffmpeg", "-y", "-i", mp3_path, actual_path],
                                   capture_output=True, timeout=30, check=True)
                else:
                    print(f"    ⚠️ 找不到輸出檔案: {actual_path}")
                    continue

            data, sr = sf.read(actual_path)
            if sample_rate is None:
                sample_rate = sr
            if data.ndim > 1:
                data = data.mean(axis=1)

            collected.append(data)
            seg_info.append({
                "index": i,
                "speaker": seg["speaker"],
                "duration": len(data) / sr,
                "engine": engine,
            })

            # 段間靜音
            if i < len(SEGMENTS) - 1:
                silence = np.zeros(int(sr * PAUSE_SEC), dtype=np.float32)
                collected.append(silence)

    if not collected:
        print("❌ 所有段落生成失敗！")
        sys.exit(1)

    # 合併
    merged = np.concatenate(collected)

    # 音量正規化 (target RMS = 0.08)
    rms = np.sqrt(np.mean(merged ** 2))
    if rms > 0:
        gain = min(0.08 / rms, 0.95 / np.max(np.abs(merged)))
        merged = (merged * gain).astype(np.float32)

    sf.write(OUTPUT, merged, sample_rate)
    total_dur = len(merged) / sample_rate
    elapsed = time.time() - t0

    print(f"\n✅ 完成！")
    print(f"  輸出: {OUTPUT}")
    print(f"  總時長: {total_dur:.1f}s")
    print(f"  取樣率: {sample_rate}Hz")
    print(f"  耗時: {elapsed:.1f}s")
    for s in seg_info:
        print(f"    段{s['index']+1}: {s['speaker']} ({s['duration']:.1f}s, {s['engine']})")

if __name__ == "__main__":
    main()
