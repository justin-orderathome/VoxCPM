#!/usr/bin/env python3
"""
Clone Reference RMS 正規化腳本
將所有 ref_*.wav 統一到 TARGET_RMS，自動備份原檔為 .orig.wav

用法：
  python normalize_clone_refs.py          # 預覽模式（不寫入）
  python normalize_clone_refs.py --apply  # 實際寫入
  python normalize_clone_refs.py --target-rms 0.04  # 自訂目標 RMS
"""
import soundfile as sf
import numpy as np
import os, glob, argparse, json, shutil

REF_DIR = "/home/hermes01/projects/VoxCPM/test_output/voice_profiles/clone_refs"
DEFAULT_TARGET_RMS = 0.04


def measure(filepath: str) -> dict:
    data, sr = sf.read(filepath)
    rms = float(np.sqrt(np.mean(data**2)))
    peak = float(np.max(np.abs(data)))
    dur = len(data) / sr
    return {"rms": rms, "peak": peak, "sr": sr, "dur": dur, "data": data}


def main():
    parser = argparse.ArgumentParser(description="Normalize clone ref WAV files to target RMS")
    parser.add_argument("--apply", action="store_true", help="Actually write files (default: dry-run)")
    parser.add_argument("--target-rms", type=float, default=DEFAULT_TARGET_RMS, help=f"Target RMS (default: {DEFAULT_TARGET_RMS})")
    args = parser.parse_args()

    target_rms = args.target_rms
    apply_mode = args.apply
    mode_label = "WRITE" if apply_mode else "DRY-RUN"

    files = sorted(glob.glob(os.path.join(REF_DIR, "ref_*.wav")))
    files = [f for f in files if ".bak" not in f and ".orig" not in f]

    if not files:
        print(f"No ref WAV files found in {REF_DIR}")
        return

    print(f"=== Clone Reference RMS Normalization ({mode_label}) ===")
    print(f"Target RMS: {target_rms}")
    print(f"Files found: {len(files)}")
    print()

    results = []
    for f in files:
        name = os.path.basename(f)
        info = measure(f)
        rms = info["rms"]
        peak = info["peak"]
        gain = target_rms / rms if rms > 0 else 0
        new_peak = peak * gain
        clip = new_peak > 0.98

        result = {
            "file": name,
            "old_rms": round(rms, 6),
            "old_peak": round(peak, 4),
            "gain": round(gain, 4),
            "new_peak": round(new_peak, 4),
            "clip": clip,
            "status": "SKIP" if clip else "OK",
        }
        results.append(result)

        clip_flag = " ⚠️ CLIP" if clip else ""
        print(f"  {name:30s}  RMS {rms:.6f} → {target_rms:.6f}  "
              f"Peak {peak:.4f} → {new_peak:.4f}  "
              f"Gain {gain:.3f}x{clip_flag}")

    # Check for any clipping
    clips = [r for r in results if r["clip"]]
    if clips:
        print(f"\n⚠️ {len(clips)} files would clip at target RMS {target_rms}")
        print("Aborting. Lower target RMS or fix source files first.")
        for c in clips:
            print(f"  {c['file']}: new peak = {c['new_peak']:.4f}")
        return

    if not apply_mode:
        print(f"\n[DRY-RUN] {len(results)} files would be normalized. Use --apply to write.")
        return

    # Apply normalization
    print(f"\nApplying normalization to {len(results)} files...")
    for r in results:
        filepath = os.path.join(REF_DIR, r["file"])
        backup = filepath.replace(".wav", ".orig.wav")

        # Backup original if not already backed up
        if not os.path.exists(backup):
            shutil.copy2(filepath, backup)
            print(f"  📦 Backed up → {os.path.basename(backup)}")

        # Read, normalize, write
        data, sr = sf.read(filepath)
        gain = r["gain"]
        normalized = data * gain

        # Safety: hard clip at ±0.98 just in case
        np.clip(normalized, -0.98, 0.98, out=normalized)

        sf.write(filepath, normalized, sr)
        print(f"  ✅ {r['file']} normalized (gain={gain:.3f}x)")

    print(f"\n✅ Done. {len(results)} files normalized to RMS={target_rms}")

    # Write report
    report_path = os.path.join(REF_DIR, "normalization_report.json")
    with open(report_path, "w", encoding="utf-8") as fp:
        json.dump({"target_rms": target_rms, "results": results}, fp, ensure_ascii=False, indent=2)
    print(f"📄 Report: {report_path}")


if __name__ == "__main__":
    main()
