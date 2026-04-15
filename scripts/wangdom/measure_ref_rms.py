#!/usr/bin/env python3
"""Measure RMS of all clone references and compute normalization gains."""
import soundfile as sf
import numpy as np
import os, glob, json

REF_DIR = os.path.join(os.path.dirname(__file__), "..", "test_output", "voice_profiles", "clone_refs")
TARGET_RMS = 0.04

results = []
for f in sorted(glob.glob(os.path.join(REF_DIR, "ref_*.wav"))):
    if ".bak" in f:
        continue
    name = os.path.basename(f)
    data, sr = sf.read(f)
    rms = float(np.sqrt(np.mean(data**2)))
    peak = float(np.max(np.abs(data)))
    dur = len(data) / sr
    ratio = TARGET_RMS / rms if rms > 0 else float("inf")
    clip_risk = (peak * ratio) > 0.95
    results.append({
        "file": name,
        "rms": round(rms, 6),
        "peak": round(peak, 4),
        "duration_s": round(dur, 2),
        "gain_ratio": round(ratio, 3),
        "clip_risk": clip_risk,
    })

results.sort(key=lambda x: x["rms"])
print(f"TARGET_RMS = {TARGET_RMS}")
print(f"{'File':30s} {'RMS':>10s} {'Peak':>8s} {'Dur':>7s} {'Gain':>7s} {'Clip?':>6s} Note")
print("-" * 85)
for r in results:
    flag = "LOW" if r["rms"] < 0.02 else ("HIGH" if r["rms"] > 0.06 else "")
    clip = "YES" if r["clip_risk"] else ""
    print(f"{r['file']:30s} {r['rms']:10.6f} {r['peak']:8.4f} {r['duration_s']:6.2f}s {r['gain_ratio']:6.3f}x {clip:>6s} {flag}")

print("\n---JSON---")
print(json.dumps(results, ensure_ascii=False, indent=2))
