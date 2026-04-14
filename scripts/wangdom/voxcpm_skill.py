#!/usr/bin/env python3
"""王朝 VoxCPM Skill（Phase 2 完整版）

功能：
- TTS Router：VoxCPM2 優先，條件不符時 fallback Edge TTS
- Voice Profile Manager：角色音色設定讀取（YAML/JSON）
- Style Compiler：mood / expression_tags 轉譯為 VoxCPM 控制指令
- Audio Post-processing：ambience_profile 後處理（reverb/EQ）
- 輸出取樣率以 model.tts_model.sample_rate 為準（禁止硬編碼）
- 預設輸出 MP3（128kbps），自動 ffmpeg 轉檔，中間 WAV 自動清除
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import soundfile as sf

# 同目錄模組
import sys as _sys
_wangdom_dir = str(Path(__file__).parent)
if _wangdom_dir not in _sys.path:
    _sys.path.insert(0, _wangdom_dir)

from style_compiler import compile_text
from audio_post import post_process


@dataclass
class VoiceProfile:
    name: str
    mode: str = "design"  # design | clone | ultimate_clone | edge_tts
    description: str = ""
    reference: Optional[str] = None
    edge_voice: Optional[str] = None


class VoiceProfileManager:
    def __init__(self, profiles_path: str | Path):
        self.profiles_path = Path(profiles_path)
        self._profiles: Dict[str, VoiceProfile] = {}
        self.reload()

    def reload(self) -> None:
        self._profiles = self._load_profiles(self.profiles_path)

    def get(self, character: str) -> VoiceProfile:
        if character in self._profiles:
            return self._profiles[character]
        # fallback 預設設定
        return VoiceProfile(name=character, mode="design", description="(沉穩清晰，中性語氣)")

    @staticmethod
    def _load_profiles(path: Path) -> Dict[str, VoiceProfile]:
        if not path.exists():
            return {}
        raw = path.read_text(encoding="utf-8")

        data: Dict[str, Any]
        if path.suffix.lower() in {".yaml", ".yml"}:
            try:
                import yaml  # type: ignore
            except Exception as exc:
                raise RuntimeError("讀取 YAML 需安裝 pyyaml：pip install pyyaml") from exc
            data = yaml.safe_load(raw) or {}
        else:
            data = json.loads(raw)

        profiles: Dict[str, VoiceProfile] = {}
        voice_profiles = data.get("voice_profiles", data)
        for name, cfg in voice_profiles.items():
            if not isinstance(cfg, dict):
                continue
            profiles[name] = VoiceProfile(
                name=name,
                mode=str(cfg.get("mode", "design")),
                description=str(cfg.get("description", "")),
                reference=cfg.get("reference"),
                edge_voice=cfg.get("edge_voice"),
            )
        return profiles


class VoxCpmSkill:
    def __init__(
        self,
        model_id: str = "openbmb/VoxCPM2",
        profiles_path: str | Path = "profiles/voice_profiles.yaml",
        min_free_gb: float = 4.0,
    ) -> None:
        self.model_id = model_id
        self.min_free_gb = min_free_gb
        self.profile_manager = VoiceProfileManager(profiles_path)
        self._model = None

    # ---------- Router ----------
    def route_engine(self, profile: VoiceProfile, force_engine: Optional[str] = None) -> str:
        if force_engine in {"voxcpm", "edge_tts"}:
            return force_engine
        if profile.mode == "edge_tts":
            return "edge_tts"
        if not self._gpu_vram_sufficient(self.min_free_gb):
            return "edge_tts"
        return "voxcpm"

    def synthesize(
        self,
        text: str,
        character: str,
        output_path: str | Path,
        force_engine: Optional[str] = None,
        mood: Optional[str] = None,
        expression_tags: Optional[list] = None,
        ambience_profile: str = "none",
        output_format: str = "mp3",
    ) -> Dict[str, Any]:
        """合成語音（含 style compiler + audio post）

        Args:
            text: 台詞
            character: 角色名
            output_path: 輸出路徑
            force_engine: 強制引擎
            mood: 情緒基調（如「沉穩」「莊嚴」）
            expression_tags: 非語言標籤列表
            ambience_profile: 環境音場（none/studio/hall/battle/rain/cave）
            output_format: 輸出格式（mp3/wav），預設 mp3
        """
        profile = self.profile_manager.get(character)
        engine = self.route_engine(profile, force_engine)
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if engine == "voxcpm":
            result = self._generate_with_voxcpm(
                text, profile, output_path,
                mood=mood, expression_tags=expression_tags,
                ambience_profile=ambience_profile,
            )
        else:
            result = self._generate_with_edge_tts(text, profile, output_path)

        # MP3 預設輸出：WAV → MP3 轉檔
        if output_format == "mp3" and result.get("ok") and Path(result["output_path"]).suffix.lower() == ".wav":
            wav_path = Path(result["output_path"])
            mp3_path = wav_path.with_suffix(".mp3")
            try:
                self._wav_to_mp3(wav_path, mp3_path)
                result["output_path"] = str(mp3_path)
                result["format"] = "mp3"
            except RuntimeError as e:
                # ffmpeg 失敗時保留 WAV，不中斷流程
                result["format"] = "wav"
                result["mp3_error"] = str(e)

        return result

    # ---------- VoxCPM ----------
    def _load_model(self):
        if self._model is not None:
            return self._model
        from voxcpm import VoxCPM  # lazy import

        self._model = VoxCPM.from_pretrained(self.model_id, load_denoiser=False)
        return self._model

    def _generate_with_voxcpm(
        self,
        text: str,
        profile: VoiceProfile,
        output_path: Path,
        mood: Optional[str] = None,
        expression_tags: Optional[list] = None,
        ambience_profile: str = "none",
    ) -> Dict[str, Any]:
        model = self._load_model()

        # 使用 style_compiler 編譯最終文字
        compiled_text = compile_text(
            text=text,
            mood=mood,
            expression_tags=expression_tags,
            profile_description=profile.description,
        )

        kwargs: Dict[str, Any] = {"text": compiled_text, "cfg_value": 2.0, "inference_timesteps": 10}

        # clone / ultimate_clone 模式
        if profile.mode in {"clone", "ultimate_clone"} and profile.reference:
            kwargs["reference_wav_path"] = profile.reference

        wav = model.generate(**kwargs)
        arr = wav.cpu().numpy() if hasattr(wav, "cpu") else wav
        if getattr(arr, "ndim", 1) == 2:
            arr = arr.squeeze(0)

        sample_rate = int(model.tts_model.sample_rate)

        # 先存暫時 WAV（供 audio_post 處理）
        raw_path = output_path
        if ambience_profile != "none":
            raw_path = output_path.with_suffix(".raw.wav")

        sf.write(str(raw_path), arr, sample_rate)

        # Audio post-processing
        if ambience_profile != "none":
            post_result = post_process(
                raw_path, output_path,
                ambience_profile=ambience_profile,
                normalize=0.08,
            )
            # 清除暫存
            if raw_path != output_path and raw_path.exists():
                raw_path.unlink()
        else:
            if raw_path != output_path:
                import shutil
                shutil.move(str(raw_path), str(output_path))

        return {
            "ok": True,
            "engine": "voxcpm",
            "character": profile.name,
            "mode": profile.mode,
            "sample_rate": sample_rate,
            "output_path": str(output_path),
            "text_used": compiled_text,
            "mood": mood,
            "ambience": ambience_profile,
        }

    # ---------- Edge TTS Fallback ----------
    def _generate_with_edge_tts(self, text: str, profile: VoiceProfile, output_path: Path) -> Dict[str, Any]:
        voice = profile.edge_voice or "zh-TW-YunJheNeural"
        try:
            import edge_tts  # type: ignore
        except Exception:
            return {
                "ok": False,
                "engine": "edge_tts",
                "character": profile.name,
                "mode": profile.mode,
                "output_path": str(output_path),
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
            "character": profile.name,
            "mode": profile.mode,
            "voice": voice,
            "output_path": str(output_path),
        }

    # ---------- MP3 轉檔 ----------
    @staticmethod
    def _wav_to_mp3(wav_path: Path, mp3_path: Path, bitrate: str = "128k") -> Path:
        """ffmpeg WAV → MP3 轉檔。成功後刪除中間 WAV。"""
        cmd = [
            "ffmpeg", "-y", "-i", str(wav_path),
            "-codec:a", "libmp3lame", "-b:a", bitrate,
            str(mp3_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {result.stderr[:500]}")
        # 刪除中間 WAV
        if wav_path.exists() and wav_path != mp3_path:
            wav_path.unlink()
        return mp3_path

    # ---------- Health ----------
    @staticmethod
    def _gpu_vram_sufficient(min_free_gb: float) -> bool:
        try:
            import torch

            if not torch.cuda.is_available():
                return False
            free_bytes, _total_bytes = torch.cuda.mem_get_info()
            free_gb = free_bytes / (1024**3)
            return free_gb >= min_free_gb
        except Exception:
            return False


def main() -> None:
    parser = argparse.ArgumentParser(description="王朝 VoxCPM Skill（Phase 2 完整版）")
    parser.add_argument("--text", required=True, help="要合成的文字")
    parser.add_argument("--character", required=True, help="角色名稱（例如 軍師·諸葛亮）")
    parser.add_argument("--output", default="test_output/skill_out.mp3", help="輸出音檔路徑")
    parser.add_argument("--profiles", default="profiles/voice_profiles.yaml", help="voice profile 檔案")
    parser.add_argument("--force-engine", choices=["voxcpm", "edge_tts"], default=None)
    parser.add_argument("--mood", default=None, help="情緒基調（如 沉穩/莊嚴/歡快）")
    parser.add_argument("--expression-tags", default=None, help="非語言標籤（逗號分隔）")
    parser.add_argument("--ambience", default="none",
                       choices=["none", "studio", "hall", "battle", "rain", "cave"],
                       help="環境音場設定")
    parser.add_argument("--format", choices=["mp3", "wav"], default="mp3",
                       help="輸出格式（預設 mp3）")
    args = parser.parse_args()

    tags = args.expression_tags.split(",") if args.expression_tags else None

    skill = VoxCpmSkill(profiles_path=args.profiles)
    result = skill.synthesize(
        text=args.text,
        character=args.character,
        output_path=args.output,
        force_engine=args.force_engine,
        mood=args.mood,
        expression_tags=tags,
        ambience_profile=args.ambience,
        output_format=args.format,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
