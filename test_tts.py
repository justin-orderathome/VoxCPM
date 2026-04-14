#!/usr/bin/env python3
"""VoxCPM2 Phase 1 驗證測試腳本（T3-T7）"""
import time
import numpy as np
import torch
import soundfile as sf
from voxcpm import VoxCPM

SAMPLE_RATE = 24000

print("=" * 60)
print("VoxCPM2 Phase 1 驗證測試")
print("=" * 60)

# T3: 載入模型
print("\n[T3] 載入模型 openbmb/VoxCPM2 ...")
t0 = time.time()
model = VoxCPM.from_pretrained("openbmb/VoxCPM2", load_denoiser=False)
t_load = time.time() - t0
print(f"  模型載入完成，耗時 {t_load:.1f}s")
vram_used = torch.cuda.memory_allocated(0) / 1024**3
vram_reserved = torch.cuda.memory_reserved(0) / 1024**3
print(f"  VRAM used: {vram_used:.2f} GB, reserved: {vram_reserved:.2f} GB")

def save_wav(wav_data, path, sr=SAMPLE_RATE):
    """儲存音訊為 WAV"""
    if isinstance(wav_data, torch.Tensor):
        wav_data = wav_data.cpu().numpy()
    if wav_data.ndim == 2:
        wav_data = wav_data.squeeze(0)
    sf.write(path, wav_data, sr)
    return len(wav_data) / sr

def measure_rtf(wav_data, gen_time, sr=SAMPLE_RATE):
    if isinstance(wav_data, torch.Tensor):
        audio_len = wav_data.shape[-1]
    else:
        audio_len = wav_data.shape[-1]
    duration = audio_len / sr
    return gen_time / duration, duration

# T4: 中文 TTS 基礎測試 ×3
print("\n[T4] 中文 TTS 基礎測試")
test_texts = [
    "主公萬安，臣諸葛亮參見。",
    "今日天氣晴朗，適合出兵討伐曹賊。",
    "亮有一計，可令敵軍不戰而降。",
]

t4_results = []
for i, text in enumerate(test_texts):
    print(f"  測試 {i+1}: 「{text[:20]}...」")
    t0 = time.time()
    wav = model.generate(text=text, cfg_value=2.0, inference_timesteps=10)
    t_gen = time.time() - t0
    rtf, audio_dur = measure_rtf(wav, t_gen)
    out_path = f"test_output/t4_chinese_{i+1}.wav"
    save_wav(wav, out_path)
    vram_now = torch.cuda.memory_allocated(0) / 1024**3
    print(f"    音訊: {audio_dur:.1f}s | 生成: {t_gen:.2f}s | RTF: {rtf:.3f} | VRAM: {vram_now:.2f} GB")
    t4_results.append({"text": text, "rtf": rtf, "duration": audio_dur, "gen_time": t_gen})

# T5: Voice Design 測試 ×3
print("\n[T5] Voice Design 測試")
design_tests = [
    ("(儒雅清朗的青年男性，帶書卷氣)臣諸葛亮，參見主公。", "strategist"),
    ("(沉穩威嚴的中年男性，帝王之聲)眾卿平身，今日議事開始。", "emperor"),
    ("(溫婉細膩的年輕女性)主公，茶已備好，請慢用。", "court_lady"),
]

t5_results = []
for i, (text, label) in enumerate(design_tests):
    print(f"  測試 {i+1} ({label}): 「{text[:30]}...」")
    t0 = time.time()
    wav = model.generate(text=text, cfg_value=2.0, inference_timesteps=10)
    t_gen = time.time() - t0
    rtf, audio_dur = measure_rtf(wav, t_gen)
    out_path = f"test_output/t5_design_{i+1}_{label}.wav"
    save_wav(wav, out_path)
    vram_now = torch.cuda.memory_allocated(0) / 1024**3
    print(f"    音訊: {audio_dur:.1f}s | 生成: {t_gen:.2f}s | RTF: {rtf:.3f} | VRAM: {vram_now:.2f} GB")
    t5_results.append({"label": label, "rtf": rtf, "duration": audio_dur, "gen_time": t_gen})

# T6: Voice Clone 測試
print("\n[T6] Voice Clone 測試")
print("  步驟 1: 生成 reference audio ...")
ref_text = "(低沉穩重的中年男性)天地有正氣，雜然賦流形。"
ref_wav = model.generate(text=ref_text, cfg_value=2.0, inference_timesteps=10)
ref_path = "test_output/t6_reference.wav"
save_wav(ref_wav, ref_path)
print(f"    Reference 已儲存: {ref_path}")

clone_tests = [
    ("稟報主公，暗衛已完成任務。", "clone_report"),
    ("臣以為此計甚妙，但需從長計議。", "clone_plan"),
]

t6_results = []
for i, (text, label) in enumerate(clone_tests):
    print(f"  測試 {i+1} ({label}): 「{text}」")
    t0 = time.time()
    wav = model.generate(
        text=text,
        reference_wav_path=ref_path,
        cfg_value=2.0,
        inference_timesteps=10,
    )
    t_gen = time.time() - t0
    rtf, audio_dur = measure_rtf(wav, t_gen)
    out_path = f"test_output/t6_clone_{i+1}_{label}.wav"
    save_wav(wav, out_path)
    vram_now = torch.cuda.memory_allocated(0) / 1024**3
    print(f"    音訊: {audio_dur:.1f}s | 生成: {t_gen:.2f}s | RTF: {rtf:.3f} | VRAM: {vram_now:.2f} GB")
    t6_results.append({"label": label, "rtf": rtf, "duration": audio_dur, "gen_time": t_gen})

# T7: 效能報告
print("\n" + "=" * 60)
print("[T7] 效能報告")
print("=" * 60)

all_rtfs = [r["rtf"] for r in t4_results + t5_results + t6_results]
avg_rtf = sum(all_rtfs) / len(all_rtfs)
max_rtf = max(all_rtfs)
final_vram = torch.cuda.memory_allocated(0) / 1024**3

print(f"\n  模型載入: {t_load:.1f}s (含 warm-up)")
print(f"  VRAM 佔用: {final_vram:.2f} GB / 11.6 GB ({final_vram/11.6*100:.1f}%)")
print(f"  平均 RTF: {avg_rtf:.3f} (越低越快)")
print(f"  最高 RTF: {max_rtf:.3f}")
print(f"  測試音檔數: {len(t4_results) + len(t5_results) + len(t6_results)}")
print(f"  取樣率: {SAMPLE_RATE} Hz")

print(f"\n  驗收標準:")
print(f"    RTF < 0.5: {'✅ PASS' if avg_rtf < 0.5 else '❌ FAIL'} (actual: {avg_rtf:.3f})")
print(f"    VRAM < 10GB: {'✅ PASS' if final_vram < 10 else '❌ FAIL'} (actual: {final_vram:.2f} GB)")

print(f"\n  輸出檔案:")
import os
for f in sorted(os.listdir("test_output")):
    size_kb = os.path.getsize(f"test_output/{f}") / 1024
    print(f"    test_output/{f} ({size_kb:.0f} KB)")

print("\n✅ Phase 1 全部測試完成！")
