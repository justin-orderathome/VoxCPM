#!/usr/bin/env python3
"""IndexTTS2 HTTP Client — Discord Voice Bot 後端介面卡

對接 IndexTTS2 Docker API (port 8007)，提供：
  - healthcheck / ensure_running / stop（Docker 自動啟停）
  - synthesize（角色 ref + 情感控制 + 自動 resample）
  - VRAM 互斥：啟動 VoxCPM 前自動停 Docker，反之亦然

使用同步 HTTP（urllib），可在 thread / executor 中安全呼叫。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf

# ---------------------------------------------------------------------------
# 路徑設定
# ---------------------------------------------------------------------------
INDEXTTS_API = os.environ.get("INDEXTTS_API_URL", "http://127.0.0.1:8007")
INDEXTTS_DOCKER_DIR = Path(os.environ.get(
    "INDEXTTS_DOCKER_DIR",
    str(Path.home() / "projects" / "index-tts2-hermes"),
)).expanduser()
INDEXTTS_AUDIO_OUTPUT = INDEXTTS_DOCKER_DIR / "audio_output"
INDEXTTS_ROLES_DIR = INDEXTTS_DOCKER_DIR / "voice_refs" / "roles"
DOCKER_COMPOSE_FILE = INDEXTTS_DOCKER_DIR / "docker-compose.yml"

# ---------------------------------------------------------------------------
# Mood → IndexTTS2 emo_text 映射（對齊 style_compiler.py）
# ---------------------------------------------------------------------------
MOOD_TO_EMO_TEXT: dict[str, str] = {
    "沉穩": "語氣沉穩內斂，從容不迫",
    "莊嚴": "語調莊重嚴肅，不怒自威",
    "剛正": "語氣堅定有力，鏗鏘正直",
    "自信": "語氣自信果斷，底氣十足",
    "謹慎": "語調審慎小心，字斟句酌",
    "恭敬": "語氣恭敬有禮，畢恭畢敬",
    "急切": "語速稍快，語氣急促緊迫",
    "歡快": "語氣輕快活潑，帶笑意",
    "悲憤": "語調低沉悲壯，帶壓抑的憤怒",
    "驚訝": "語調上揚，帶意外之情",
    "溫和": "語氣溫和親切，柔和從容",
    "冷酷": "語調冰冷疏離，不帶感情",
    "戲劇": "語氣誇張戲劇化，聲調起伏大",
    "朗讀": "語速適中，吐字清晰，朗誦風格",
    "輕鬆": "語氣輕鬆隨意，像聊天",
}

# ---------------------------------------------------------------------------
# 底層 HTTP
# ---------------------------------------------------------------------------

def _api_get(endpoint: str, timeout: int = 10) -> dict:
    url = f"{INDEXTTS_API}{endpoint}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return {}


def _api_post(endpoint: str, payload: dict, timeout: int = 300) -> dict:
    url = f"{INDEXTTS_API}{endpoint}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return {"ok": False, "reason": f"HTTP {e.code}: {body[:300]}"}
    except Exception as e:
        return {"ok": False, "reason": str(e)}

# ---------------------------------------------------------------------------
# Docker 管理
# ---------------------------------------------------------------------------

def healthcheck() -> bool:
    """IndexTTS2 API 是否回應。"""
    r = _api_get("/health", timeout=5)
    return r.get("status") == "ok"


def ensure_running(timeout: int = 300) -> dict:
    """確保 IndexTTS2 Docker 運行且 API 健康。

    Returns ``{"ok": True}`` 或 ``{"ok": False, "reason": "..."}``。
    """
    if healthcheck():
        return {"ok": True}

    print("[indextts2] API 未回應，啟動 Docker…")

    try:
        proc = subprocess.run(
            ["docker", "compose", "-f", str(DOCKER_COMPOSE_FILE),
             "--profile", "tts", "up", "-d"],
            capture_output=True, text=True, timeout=120,
            cwd=str(INDEXTTS_DOCKER_DIR),
        )
        if proc.returncode != 0:
            return {"ok": False, "reason": f"docker compose up failed:\n{proc.stderr[:300]}"}
    except Exception as e:
        return {"ok": False, "reason": f"docker compose error: {e}"}

    # 等待 API 健康
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        time.sleep(3)
        if healthcheck():
            print("[indextts2] API 健康，warmup 模型…")
            warmup = _api_post("/warmup", {}, timeout=timeout)
            print(f"[indextts2] Warmup: {warmup.get('status', warmup)}")
            return {"ok": True}

    return {"ok": False, "reason": f"IndexTTS2 未在 {timeout}s 內就緒"}


def stop() -> dict:
    """停止 IndexTTS2 Docker 容器。"""
    try:
        proc = subprocess.run(
            ["docker", "compose", "-f", str(DOCKER_COMPOSE_FILE),
             "--profile", "tts", "down"],
            capture_output=True, text=True, timeout=60,
            cwd=str(INDEXTTS_DOCKER_DIR),
        )
        if proc.returncode != 0:
            return {"ok": False, "reason": proc.stderr[:200]}
        print("[indextts2] Docker 已停止")
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "reason": str(e)}

# ---------------------------------------------------------------------------
# 角色參考音檔解析
# ---------------------------------------------------------------------------

def _resolve_ref(character: str) -> str:
    """角色名 → Docker 內部 ref 路徑。找不到則 fallback 唐伯虎。"""
    ref_local = INDEXTTS_ROLES_DIR / f"{character}.wav"
    if ref_local.exists():
        return f"/home/app/voice_refs/roles/{character}.wav"

    fallback_local = INDEXTTS_ROLES_DIR / "待詔·唐伯虎.wav"
    if fallback_local.exists():
        print(f"[indextts2] ⚠️ 角色 '{character}' 無 ref，fallback 唐伯虎")
        return "/home/app/voice_refs/roles/待詔·唐伯虎.wav"

    return ""


def list_available_roles() -> list[str]:
    """列出已就緒的角色 ref。"""
    if not INDEXTTS_ROLES_DIR.exists():
        return []
    return sorted(
        p.stem for p in INDEXTTS_ROLES_DIR.glob("*.wav")
    )

# ---------------------------------------------------------------------------
# 合成
# ---------------------------------------------------------------------------

def _normalize_rms(audio: np.ndarray, target_rms: float = 0.08) -> np.ndarray:
    rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2))) if audio.size else 0.0
    if rms < 1e-8:
        return audio.astype(np.float32)
    gain = min(target_rms / rms, 10.0)
    return (audio.astype(np.float32) * gain).astype(np.float32)


def _resample_to_48k(audio: np.ndarray, src_sr: int) -> np.ndarray:
    """Resample 到 48 kHz（Discord 串流格式）。"""
    if src_sr == 48000:
        return audio
    n = len(audio)
    if n == 0:
        return audio
    new_n = int(round(n * 48000 / src_sr))
    try:
        from scipy.signal import resample  # type: ignore
        return resample(audio, new_n).astype(np.float32)
    except Exception:
        x_old = np.linspace(0.0, 1.0, n, endpoint=False)
        x_new = np.linspace(0.0, 1.0, new_n, endpoint=False)
        return np.interp(x_new, x_old, audio).astype(np.float32)


def synthesize(
    text: str,
    character: str,
    output_path: str | Path,
    mood: Optional[str] = None,
    output_format: str = "wav",
) -> dict:
    """透過 IndexTTS2 API 合成語音。

    Parameters
    ----------
    text : 合成文字（繁體，API 內部自動轉簡體）
    character : 角色名（對映 voice_refs/roles/）
    output_path : 輸出檔案路徑
    mood : 情境描述（對映 MOOD_TO_EMO_TEXT）
    output_format : "wav" 或 "mp3"

    Returns
    -------
    dict — {"ok", "engine", "output_path", "format", "sample_rate", "character", ...}
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # 1. 確保 API 就緒
    ready = ensure_running(timeout=120)
    if not ready.get("ok"):
        return {"ok": False, "reason": f"IndexTTS2 未就緒: {ready.get('reason')}", "engine": "indextts"}

    # 2. 解析 ref
    spk_ref = _resolve_ref(character)
    if not spk_ref:
        return {"ok": False, "reason": f"角色 '{character}' 無可用 ref", "engine": "indextts"}

    # 3. 組 payload
    line_id = f"bot_{int(time.time() * 1000)}"
    payload: dict = {
        "text": text,
        "spk_audio_prompt": spk_ref,
        "line_id": line_id,
        "use_emo_text": True,
    }

    if mood and mood in MOOD_TO_EMO_TEXT:
        payload["emo_text"] = MOOD_TO_EMO_TEXT[mood]
        payload["emo_alpha"] = 0.5
    elif mood:
        payload["emo_text"] = mood
        payload["emo_alpha"] = 0.4

    # 4. 呼叫 API
    result = _api_post("/synthesize", payload, timeout=180)
    if "line_id" not in result:
        return {"ok": False, "reason": result.get("reason", "未知 API 錯誤"), "engine": "indextts"}

    # 5. 讀回 WAV（從 Docker volume mount）
    docker_output = INDEXTTS_AUDIO_OUTPUT / f"{result['line_id']}.wav"
    if not docker_output.exists():
        return {"ok": False, "reason": f"輸出檔案不存在: {docker_output}", "engine": "indextts"}

    audio, sr = sf.read(str(docker_output), dtype="float32")
    if audio.ndim == 2:
        audio = audio.mean(axis=1)

    # 6. Resample → 48 kHz
    audio = _resample_to_48k(audio, sr)

    # 7. Normalize RMS
    audio = _normalize_rms(audio, target_rms=0.08)

    # 8. 寫出
    if output_format == "wav":
        sf.write(str(output_path), audio, 48000)
        final_path = output_path
    else:
        wav_tmp = output_path.with_suffix(".tmp.wav")
        sf.write(str(wav_tmp), audio, 48000)
        cmd = [
            "ffmpeg", "-y", "-i", str(wav_tmp),
            "-codec:a", "libmp3lame", "-b:a", "128k",
            str(output_path),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        wav_tmp.unlink(missing_ok=True)
        if r.returncode != 0:
            return {"ok": False, "reason": f"ffmpeg MP3 失敗: {r.stderr[:300]}", "engine": "indextts"}
        final_path = output_path

    # 9. 清理 Docker 側輸出
    try:
        docker_output.unlink(missing_ok=True)
    except Exception:
        pass

    duration = len(audio) / 48000.0
    return {
        "ok": True,
        "engine": "indextts",
        "output_path": str(final_path),
        "format": output_format,
        "sample_rate": 48000,
        "character": character,
        "duration_sec": round(duration, 3),
        "fallback_from": "",
    }
