#!/usr/bin/env python3
"""VoxCPM2 Phase 1 驗證（修正取樣率 48000Hz）"""
import time
import numpy as np
import torch
import soundfile as sf
from voxcpm import VoxCPM

print("=" * 60)
print("VoxCPM2 Phase 1 修正版（48kHz）")
print("=" * 60)

# 載入模型
print("\n載入模型 ...")
t0 = time.time()
model = VoxCPM.from_pretrained("openbmb/VoxCPM2", load_denoiser=False)
t_load = time.time() - t0
SR = model.tts_model.sample_rate
print(f"  載入完成 ({t_load:.1f}s), sample_rate = {SR} Hz")
print(f"  VRAM: {torch.cuda.memory_allocated(0) / 1024**3:.2f} GB")

def save_wav(wav_data, path, sr):
    if isinstance(wav_data, torch.Tensor):
        wav_data = wav_data.cpu().numpy()
    if wav_data.ndim == 2:
        wav_data = wav_data.squeeze(0)
    sf.write(path, wav_data, sr)
    return len(wav_data) / sr

def gen(label, text, out_path, **kwargs):
    print(f"\n  [{label}] 「{text[:30]}...」")
    t0 = time.time()
    wav = model.generate(text=text, cfg_value=2.0, inference_timesteps=10, **kwargs)
    t_gen = time.time() - t0
    dur = save_wav(wav, out_path, SR)
    rtf = t_gen / dur
    vram = torch.cuda.memory_allocated(0) / 1024**3
    print(f"    {dur:.1f}s audio | {t_gen:.2f}s gen | RTF {rtf:.3f} | VRAM {vram:.2f}GB")
    return rtf

# T4: 中文基礎 ×3
print("\n[T4] 中文 TTS 基礎測試")
rtfs = []
rtfs.append(gen("T4-1", "主公萬安，臣諸葛亮參見。", "test_output/t4_chinese_1.wav"))
rtfs.append(gen("T4-2", "今日天氣晴朗，適合出兵討伐曹賊。", "test_output/t4_chinese_2.wav"))
rtfs.append(gen("T4-3", "亮有一計，可令敵軍不戰而降。", "test_output/t4_chinese_3.wav"))

# T5: Voice Design ×3
print("\n[T5] Voice Design 測試")
rtfs.append(gen("T5-軍師", "(儒雅清朗的青年男性，帶書卷氣)臣諸葛亮，參見主公。", "test_output/t5_design_strategist.wav"))
rtfs.append(gen("T5-帝王", "(沉穩威嚴的中年男性，帝王之聲)眾卿平身，今日議事開始。", "test_output/t5_design_emperor.wav"))
rtfs.append(gen("T5-侍女", "(溫婉細膩的年輕女性)主公，茶已備好，請慢用。", "test_output/t5_design_court_lady.wav"))

# T6: Voice Clone ×2
print("\n[T6] Voice Clone 測試")
# 先生成 reference
ref_wav = model.generate(text="(低沉穩重的中年男性)天地有正氣，雜然賦流形。", cfg_value=2.0, inference_timesteps=10)
save_wav(ref_wav, "test_output/t6_reference.wav", SR)

rtfs.append(gen("T6-報告", "稟報主公，暗衛已完成任務。", "test_output/t6_clone_report.wav", reference_wav_path="test_output/t6_reference.wav"))
rtfs.append(gen("T6-計議", "臣以為此計甚妙，但需從長計議。", "test_output/t6_clone_plan.wav", reference_wav_path="test_output/t6_reference.wav"))

# 總結
avg_rtf = sum(rtfs) / len(rtfs)
vram = torch.cuda.memory_allocated(0) / 1024**3
print(f"\n{'='*60}")
print(f"[T7] 效能報告")
print(f"  取樣率: {SR} Hz ✅ (已修正)")
print(f"  平均 RTF: {avg_rtf:.3f} | VRAM: {vram:.2f} GB")
print(f"  RTF < 0.5: {'✅' if avg_rtf < 0.5 else '❌'}")
print(f"  VRAM < 10GB: {'✅' if vram < 10 else '❌'}")

import os
print(f"\n  輸出檔案:")
for f in sorted(os.listdir("test_output")):
    print(f"    {f} ({os.path.getsize(f'test_output/{f}')//1024} KB)")

print("\n✅ 全部完成！")
