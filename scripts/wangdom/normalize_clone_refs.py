#!/usr/bin/env python3
"""Batch-normalize clone reference audios for VoxCPM stability.

- Reads clone refs from profiles/voice_profiles.yaml
- Computes RMS in float scale
- Normalizes out-of-range files to target RMS
- Keeps .bak backup before first overwrite
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List

import numpy as np
import soundfile as sf
import yaml


DEFAULT_MIN_RMS = 0.02
DEFAULT_MAX_RMS = 0.05
DEFAULT_TARGET_RMS = 0.03
CLIP_HEADROOM = 0.98


def load_clone_refs(profiles_yaml: Path) -> List[Path]:
    data = yaml.safe_load(profiles_yaml.read_text(encoding="utf-8")) or {}
    profiles = data.get("voice_profiles", {})
    refs = []
    for _role, conf in profiles.items():
        if not isinstance(conf, dict):
            continue
        if conf.get("mode") != "clone":
            continue
        ref = conf.get("reference")
        if not ref:
            continue
        refs.append((profiles_yaml.parent.parent / ref).resolve())
    # unique + stable order
    uniq = []
    seen = set()
    for p in refs:
        s = str(p)
        if s not in seen:
            seen.add(s)
            uniq.append(p)
    return uniq


def compute_rms(audio: np.ndarray) -> float:
    if audio.ndim == 2:
        mono = audio.mean(axis=1)
    else:
        mono = audio
    return float(np.sqrt(np.mean(np.square(mono), dtype=np.float64)))


def normalize_audio(audio: np.ndarray, target_rms: float) -> np.ndarray:
    rms = compute_rms(audio)
    if rms <= 1e-9:
        return audio

    gain = target_rms / rms
    scaled = audio * gain

    peak = float(np.max(np.abs(scaled))) if scaled.size else 0.0
    if peak > CLIP_HEADROOM:
        scaled = scaled * (CLIP_HEADROOM / peak)

    return scaled.astype(np.float32)


def process_ref(path: Path, *, min_rms: float, max_rms: float, target_rms: float, dry_run: bool) -> Dict:
    row: Dict = {"path": str(path), "exists": path.exists()}
    if not path.exists():
        row["status"] = "missing"
        return row

    audio, sr = sf.read(str(path), always_2d=False)
    audio = audio.astype(np.float32)

    rms_before = compute_rms(audio)
    row["sample_rate"] = int(sr)
    row["rms_before"] = round(rms_before, 6)

    if min_rms <= rms_before <= max_rms:
        row["status"] = "ok"
        row["changed"] = False
        return row

    normalized = normalize_audio(audio, target_rms)
    rms_after = compute_rms(normalized)

    row["status"] = "normalized"
    row["changed"] = True
    row["rms_after"] = round(rms_after, 6)

    if not dry_run:
        bak = path.with_suffix(path.suffix + ".bak")
        if not bak.exists():
            bak.write_bytes(path.read_bytes())
        sf.write(str(path), normalized, sr)
        row["backup"] = str(bak)

    return row


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize VoxCPM clone reference audios")
    parser.add_argument(
        "--profiles",
        type=Path,
        default=Path("~/projects/VoxCPM/profiles/voice_profiles.yaml").expanduser(),
        help="Path to voice_profiles.yaml",
    )
    parser.add_argument("--min-rms", type=float, default=DEFAULT_MIN_RMS)
    parser.add_argument("--max-rms", type=float, default=DEFAULT_MAX_RMS)
    parser.add_argument("--target-rms", type=float, default=DEFAULT_TARGET_RMS)
    parser.add_argument("--apply", action="store_true", help="Write changes to files")
    args = parser.parse_args()

    refs = load_clone_refs(args.profiles)

    rows: List[Dict] = []
    for ref in refs:
        rows.append(
            process_ref(
                ref,
                min_rms=args.min_rms,
                max_rms=args.max_rms,
                target_rms=args.target_rms,
                dry_run=not args.apply,
            )
        )

    summary = {
        "total": len(rows),
        "missing": sum(1 for r in rows if r.get("status") == "missing"),
        "ok": sum(1 for r in rows if r.get("status") == "ok"),
        "normalized": sum(1 for r in rows if r.get("status") == "normalized"),
        "mode": "apply" if args.apply else "dry-run",
    }

    print(json.dumps({"summary": summary, "files": rows}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
