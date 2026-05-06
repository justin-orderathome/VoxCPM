#!/usr/bin/env python3
"""VoxCPM2 HTTP Client — Voice Bot 後端介面卡

對接 VoxCPM2 HTTP API Server (port 8008)，提供：
  - healthcheck / ensure_running / stop（subprocess 自動啟停）
  - synthesize（角色 ref + 情感控制 + 自動 resample）
  - VRAM 互斥：啟動 VoxCPM 前自動停 IndexTTS2 Docker，反之亦然

對齊 indextts2_client.py 風格，使用同步 HTTP（urllib）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
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
VOXCPM_API = os.environ.get("VOXCPM_API_URL", "http://127.0.0.1:8008")
VOXCPM_VENV = Path(os.environ.get(
    "VOXCPM_VENV_DIR",
    str(Path.home() / "projects" / "VoxCPM" / "venv"),
)).expanduser()
VOXCPM_SERVER_SCRIPT = Path(os.environ.get(
    "VOXCPM_SERVER_SCRIPT",
    str(Path(__file__).resolve().parent / "voxcpm2_server.py"),
)).expanduser()

# ---------------------------------------------------------------------------
# Mood → 編譯後文字（由 client 端 style_compiler 處理，此處留空）
# ---------------------------------------------------------------------------
# 實際 mood 處理已由 Voice Bot 的 _build_kwargs() 中的 compile_text() 完成
# client 只傳編譯後的 text + reference_wav_path

# ---------------------------------------------------------------------------
# 底層 HTTP
# ---------------------------------------------------------------------------

def _api_get(endpoint: str, timeout: int = 10) -> dict:
    url = f"{VOXCPM_API}{endpoint}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return {}


def _api_post(endpoint: str, payload: dict, timeout: int = 300) -> dict:
    url = f"{VOXCPM_API}{endpoint}"
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
# Server 生命週期管理
# ---------------------------------------------------------------------------

_server_process: Optional[subprocess.Popen] = None


def healthcheck() -> bool:
    """VoxCPM2 API 是否回應。"""
    r = _api_get("/health", timeout=5)
    return r.get("status") == "ok"


def get_status() -> dict:
    """取得 VoxCPM2 詳細狀態。"""
    return _api_get("/status", timeout=5)


def ensure_running(timeout: int = 300) -> dict:
    """確保 VoxCPM2 server 運行且 API 健康。

    流程：
    1. healthcheck → 已在跑就返回
    2. 停 IndexTTS2 Docker（VRAM 互斥）
    3. 啟動 subprocess
    4. 等 /health 回應

    Returns ``{"ok": True}`` 或 ``{"ok": False, "reason": "..."}``。
    """
    global _server_process

    if healthcheck():
        return {"ok": True}

    # VRAM 互斥：先停 IndexTTS2 Docker
    try:
        from indextts2_client import stop as stop_indextts
        indextts_ok = False
        try:
            from indextts2_client import healthcheck as hc_indextts
            indextts_ok = hc_indextts()
        except Exception:
            pass
        if indextts_ok:
            print("[voxcpm2] VRAM 互斥：停止 IndexTTS2 Docker…")
            stop_indextts()
            time.sleep(2)  # 等 Docker 完全釋放 VRAM
    except ImportError:
        pass  # indextts2_client 不可用時跳過

    # 啟動 server subprocess
    python = str(VOXCPM_VENV / "bin" / "python3.11")
    if not Path(python).exists():
        python = str(VOXCPM_VENV / "bin" / "python")
    if not Path(python).exists():
        return {"ok": False, "reason": f"找不到 VoxCPM Python：{python}"}

    if not VOXCPM_SERVER_SCRIPT.exists():
        return {"ok": False, "reason": f"找不到 server 腳本：{VOXCPM_SERVER_SCRIPT}"}

    print("[voxcpm2] 啟動 VoxCPM2 server…")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    try:
        _server_process = subprocess.Popen(
            [python, str(VOXCPM_SERVER_SCRIPT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(VOXCPM_SERVER_SCRIPT.parent),
        )
    except Exception as e:
        return {"ok": False, "reason": f"啟動 server 失敗：{e}"}

    # 等 API 健康
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        time.sleep(2)
        if healthcheck():
            print("[voxcpm2] ✅ VoxCPM2 server 已就緒")
            return {"ok": True}
        # 檢查 process 是否已死
        if _server_process and _server_process.poll() is not None:
            return {"ok": False, "reason": f"server 意外退出，exit code={_server_process.returncode}"}

    return {"ok": False, "reason": f"VoxCPM2 server 未在 {timeout}s 內就緒"}


def stop() -> dict:
    """停止 VoxCPM2 server subprocess。"""
    global _server_process
    if _server_process is None:
        return {"ok": True}

    # 先嘗試 API unload
    try:
        _api_post("/unload", {}, timeout=10)
    except Exception:
        pass

    # SIGTERM
    try:
        _server_process.terminate()
        _server_process.wait(timeout=15)
        print("[voxcpm2] server 已停止")
    except subprocess.TimeoutExpired:
        _server_process.kill()
        print("[voxcpm2] server 已強制終止")
    except Exception as e:
        pass
    finally:
        _server_process = None

    return {"ok": True}


def unload() -> dict:
    """僅卸載模型（不停止 server）。"""
    return _api_post("/unload", {}, timeout=30)


# ---------------------------------------------------------------------------
# 合成
# ---------------------------------------------------------------------------

def _normalize_rms(audio: np.ndarray, target_rms: float = 0.08) -> np.ndarray:
    rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2))) if audio.size else 0.0
    if rms < 1e-8:
        return audio.astype(np.float32)
    gain = min(target_rms / rms, 10.0)
    return (audio.astype(np.float32) * gain).astype(np.float32)


def _resample_audio(audio: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    if src_sr == dst_sr:
        return audio
    n = len(audio)
    if n == 0:
        return audio
    new_n = int(round(n * dst_sr / src_sr))
    try:
        from scipy.signal import resample
        return resample(audio, new_n).astype(np.float32)
    except Exception:
        x_old = np.linspace(0.0, 1.0, n, endpoint=False)
        x_new = np.linspace(0.0, 1.0, new_n, endpoint=False)
        return np.interp(x_new, x_old, audio).astype(np.float32)


def synthesize(
    text: str,
    character: str,
    output_path: str | Path,
    reference_wav_path: Optional[str] = None,
    mood: Optional[str] = None,
    expression_tags: Optional[list[str]] = None,
    ambience: str = "none",
    output_format: str = "wav",
    profile_manager=None,
    style_compiler_func=None,
) -> dict:
    """透過 VoxCPM2 HTTP API 合成語音。

    Parameters
    ----------
    text : 合成文字（已經 style_compiler 編譯過，或原始文字）
    character : 角色名（用於 profile 解析）
    output_path : 輸出檔案路徑
    reference_wav_path : 直接指定參考音檔路徑（優先於 profile 解析）
    mood : 情境描述（傳入 style_compiler）
    expression_tags : 表情標籤（傳入 style_compiler）
    ambience : 環境音（post-process 用）
    output_format : "wav" 或 "mp3"
    profile_manager : VoiceProfileManager 實例（用於解析角色 → ref）
    style_compiler_func : compile_text 函數（用於文字編譯）

    Returns
    -------
    dict — {"ok", "engine", "output_path", "format", "sample_rate", "character", ...}
    """
    import threading
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # 1. 確保 server 就緒
    ready = ensure_running(timeout=300)
    if not ready.get("ok"):
        return {"ok": False, "reason": f"VoxCPM2 server 未就緒: {ready.get('reason')}", "engine": "voxcpm"}

    # 2. 解析 reference_wav_path
    ref_path = reference_wav_path
    if not ref_path and profile_manager is not None:
        profile = profile_manager.get(character)
        if profile and profile.mode in {"clone", "ultimate_clone"} and profile.reference:
            p = Path(profile.reference)
            if not p.is_absolute():
                p = Path(__file__).resolve().parent.parent.parent / p
            if p.exists():
                ref_path = str(p)

    # 3. 編譯文字（如果提供了 style_compiler）
    compiled_text = text
    if style_compiler_func is not None:
        try:
            compiled_text = style_compiler_func(
                text=text,
                mood=mood,
                expression_tags=expression_tags,
                profile_description=profile_manager.get(character).description if profile_manager else "",
            )
        except Exception as e:
            print(f"[voxcpm2] ⚠️ style_compiler 編譯失敗: {e}，使用原始文字")

    # 4. 組 payload
    payload = {
        "text": compiled_text,
        "cfg_value": 2.0,
        "inference_timesteps": 10,
    }
    if ref_path:
        payload["reference_wav_path"] = ref_path

    # 5. 呼叫 API
    result = _api_post("/synthesize", payload, timeout=300)
    if not result.get("ok"):
        return {"ok": False, "reason": result.get("reason", "未知 API 錯誤"), "engine": "voxcpm"}

    # 6. 讀回 WAV（server 端暫存檔）
    wav_path = Path(result["wav_path"])
    if not wav_path.exists():
        return {"ok": False, "reason": f"輸出檔案不存在: {wav_path}", "engine": "voxcpm"}

    audio, sr = sf.read(str(wav_path), dtype="float32")
    if audio.ndim == 2:
        audio = audio.mean(axis=1)

    # 7. Resample → 48 kHz（Discord 格式）
    audio = _resample_audio(audio, sr, 48000)

    # 8. Normalize RMS
    audio = _normalize_rms(audio, target_rms=0.08)

    # 9. Ambience post-process（如果有）
    if ambience and ambience != "none":
        try:
            from audio_post import post_process
            wav_tmp = output_path.with_name(f"{output_path.stem}.pre.wav")
            sf.write(str(wav_tmp), audio, 48000)
            post_path = output_path.with_name(f"{output_path.stem}.post.wav")
            post_process(wav_tmp, post_path, ambience_profile=ambience, normalize=0.08)
            wav_tmp.unlink(missing_ok=True)
            audio_post = sf.read(str(post_path), dtype="float32")[0]
            if audio_post.ndim == 2:
                audio_post = audio_post.mean(axis=1)
            audio = audio_post.astype(np.float32)
            post_path.unlink(missing_ok=True)
        except ImportError:
            print("[voxcpm2] ⚠️ audio_post 模組不可用，跳過 ambience 處理")
        except Exception as e:
            print(f"[voxcpm2] ⚠️ ambience 處理失敗: {e}")

    # 10. 寫出
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
            return {"ok": False, "reason": f"ffmpeg MP3 失敗: {r.stderr[:300]}", "engine": "voxcpm"}
        final_path = output_path

    # 11. 清理 server 端暫存
    try:
        wav_path.unlink(missing_ok=True)
    except Exception:
        pass

    duration = len(audio) / 48000.0
    return {
        "ok": True,
        "engine": "voxcpm",
        "output_path": str(final_path),
        "format": output_format,
        "sample_rate": 48000,
        "character": character,
        "duration_sec": round(duration, 3),
        "fallback_from": "",
    }
