#!/usr/bin/env python3
"""全員脫口秀 — 17 角色 clone 合成 + 合併"""

import sys, time, json
import numpy as np
import soundfile as sf
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from voxcpm_skill import VoxCpmSkill

SCRIPT = Path(__file__).parent.parent.parent / "scripts/wangdom/talkshow_script.txt"
OUT_DIR = Path(__file__).parent.parent.parent / "test_output/talkshow"
OUT_DIR.mkdir(parents=True, exist_ok=True)

def parse_script(path):
    """Parse talkshow script into segments"""
    segments = []
    current_speaker = None
    current_lines = []
    
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("【"):
            # Save previous segment
            if current_speaker and current_lines:
                segments.append({
                    "speaker": current_speaker,
                    "text": "".join(current_lines)
                })
                current_lines = []
            
            if line.startswith("【"):
                # Extract speaker name between 【】
                end = line.index("】")
                current_speaker = line[1:end]
        else:
            current_lines.append(line)
    
    # Don't forget the last segment
    if current_speaker and current_lines:
        segments.append({
            "speaker": current_speaker,
            "text": "".join(current_lines)
        })
    
    return segments

def main():
    print("🎭 崴勝王朝脫口秀 — 全員合成\n")
    
    segments = parse_script(SCRIPT)
    print(f"📖 劇本載入：{len(segments)} 段台詞\n")
    
    skill = VoxCpmSkill(profiles_path="profiles/voice_profiles.yaml")
    model = skill._load_model()
    sr = int(model.tts_model.sample_rate)
    
    # Load voice profiles
    import yaml
    with open("profiles/voice_profiles.yaml") as f:
        profiles = yaml.safe_load(f)["voice_profiles"]
    
    all_audio = []
    SILENCE_GAP = np.zeros(int(sr * 0.5), dtype=np.float32)  # 0.5s gap
    MANIFEST = []
    
    for i, seg in enumerate(segments):
        speaker = seg["speaker"]
        text = seg["text"]
        
        # Find matching profile
        profile = None
        for key in profiles:
            if speaker in key or key.split("·")[-1] in speaker:
                profile = profiles[key]
                profile_key = key
                break
        
        if not profile:
            print(f"  ⚠️ [{i+1:02d}] {speaker} — 找不到 profile，跳過")
            continue
        
        ref_path = profile.get("reference")
        desc = profile.get("description", "")
        
        # Build full text with description for style control
        full_text = f"{desc}{text}" if desc else text
        
        out_path = OUT_DIR / f"{i+1:02d}_{speaker.replace('·','_')}.wav"
        
        print(f"  [{i+1:02d}/{len(segments)}] {speaker}（{len(text)}字）...", end="", flush=True)
        t0 = time.time()
        
        try:
            wav = model.generate(
                text=full_text,
                reference_wav_path=ref_path,
                cfg_value=2.0,
                inference_timesteps=10,
            )
            
            arr = wav.cpu().numpy() if hasattr(wav, "cpu") else wav
            if arr.ndim == 2:
                arr = arr.squeeze(0)
            arr = arr.astype(np.float32)
            
            # RMS normalize
            rms = np.sqrt(np.mean(arr ** 2))
            if rms > 1e-6:
                gain = min(0.08 / rms, 0.95 / np.max(np.abs(arr)))
                arr = arr * gain
            
            sf.write(str(out_path), arr, sr)
            elapsed = time.time() - t0
            duration = len(arr) / sr
            print(f" ✅ {duration:.1f}s（{elapsed:.1f}s）")
            
            all_audio.append(arr)
            all_audio.append(SILENCE_GAP)
            
            MANIFEST.append({
                "seq": i+1,
                "speaker": speaker,
                "text": text,
                "duration": round(duration, 1),
                "gen_time": round(elapsed, 1),
                "file": str(out_path.name),
            })
            
        except Exception as e:
            print(f" ❌ {e}")
    
    # Merge all segments
    if all_audio:
        merged = np.concatenate(all_audio)
        merged_path = OUT_DIR / "talkshow_full.wav"
        sf.write(str(merged_path), merged, sr)
        total_duration = len(merged) / sr
        print(f"\n✅ 合併完成：{merged_path}")
        print(f"   總時長：{total_duration:.1f}s（{total_duration/60:.1f}分鐘）")
        print(f"   段數：{len(MANIFEST)}")
        
        # Save manifest
        manifest_path = OUT_DIR / "manifest.json"
        manifest_path.write_text(json.dumps(MANIFEST, ensure_ascii=False, indent=2))
        print(f"   清單：{manifest_path}")
        
        return str(merged_path)

if __name__ == "__main__":
    main()
