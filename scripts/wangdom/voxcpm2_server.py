#!/usr/bin/env python3
"""VoxCPM2 TTS HTTP API Server

輕量 HTTP server 封裝 VoxCPM2 語音合成引擎。
- 啟動不載入模型（lazy load），首次 /synthesize 才觸發載入
- 閒置超時自動卸載模型釋放 VRAM（預設 300s）
- GPU 互斥鎖：同時只處理一個合成請求
- VRAM 檢查：載入前確認可用 VRAM > 閾值

Usage:
    python voxcpm2_server.py [--port 8008] [--idle-timeout 300]

Endpoints:
    GET  /health           — 健康檢查 + 模型狀態
    POST /synthesize       — 合成語音（回傳 WAV 路徑）
    POST /load             — 預載模型
    POST /unload           — 卸載模型釋放 VRAM
    GET  /status           — 詳細狀態（VRAM、queue、idle）

Run with VoxCPM venv:
    ~/projects/VoxCPM/venv/bin/python voxcpm2_server.py
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
PROFILES_PATH = PROJECT_DIR / "profiles" / "voice_profiles.yaml"
OUTPUT_BASE_DIR = Path(tempfile.gettempdir()) / "voxcpm2_server"
OUTPUT_BASE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_PORT = 8008
DEFAULT_IDLE_TIMEOUT = 300  # 秒
DEFAULT_MODEL_ID = "openbmb/VoxCPM2"
VRAM_LOAD_THRESHOLD_GB = 5.5  # 載入模型前要求可用 VRAM

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="VoxCPM2 TTS Server", version="1.0.0")

# ---------------------------------------------------------------------------
# 模型管理（全域狀態，thread-safe）
# ---------------------------------------------------------------------------
class _ModelManager:
    """VoxCPM2 模型生命週期管理器。"""

    def __init__(self, model_id: str, idle_timeout: int):
        self.model_id = model_id
        self.idle_timeout = idle_timeout
        self._model = None
        self._lock = threading.Lock()         # GPU 合成互斥
        self._state_lock = threading.Lock()   # 狀態讀寫互斥
        self._last_used = 0.0                 # 上次合成完成時間
        self._idle_timer: Optional[threading.Timer] = None
        self._loading = False

    # ---- 狀態查詢 ----

    @property
    def model_loaded(self) -> bool:
        return self._model is not None

    def status_dict(self) -> dict:
        with self._state_lock:
            vram_gb = 0.0
            try:
                import torch
                if torch.cuda.is_available():
                    vram_gb = round(torch.cuda.memory_allocated() / (1024**3), 2)
            except Exception:
                pass

            idle_sec = 0.0
            if self._last_used > 0:
                idle_sec = round(time.monotonic() - self._last_used, 1)

            return {
                "model_loaded": self.model_loaded,
                "loading": self._loading,
                "busy": self._lock.locked(),
                "vram_gb": vram_gb,
                "idle_seconds": idle_sec,
                "idle_timeout": self.idle_timeout,
                "model_id": self.model_id,
            }

    # ---- 模型載入 ----

    def load_model(self) -> dict:
        """載入 VoxCPM2 模型。已有模型時直接返回成功。"""
        if self._model is not None:
            self._touch()
            return {"ok": True}

        with self._state_lock:
            if self._loading:
                return {"ok": False, "reason": "模型載入中，請稍候"}
            if self._model is not None:
                self._touch()
                return {"ok": True}

            # VRAM 檢查
            vram_ok, vram_info = self._check_vram()
            if not vram_ok:
                return {"ok": False, "reason": f"VRAM 不足：{vram_info}"}

            self._loading = True

        try:
            from voxcpm import VoxCPM  # lazy import — 僅在 VoxCPM venv 中可用

            t0 = time.time()
            print(f"[voxcpm2-server] 載入模型 {self.model_id} …")
            # optimize=False：避免多線程下 CUDA graph assertion
            self._model = VoxCPM.from_pretrained(
                self.model_id, load_denoiser=False, optimize=False
            )
            dt = time.time() - t0
            sr = self._get_sample_rate()
            print(f"[voxcpm2-server] ✅ 模型已載入（{dt:.1f}s）, sample_rate={sr}Hz")

            with self._state_lock:
                self._loading = False
            self._touch()
            self._schedule_idle_unload()
            return {"ok": True, "load_time_sec": round(dt, 1), "sample_rate": sr}

        except Exception as e:
            with self._state_lock:
                self._loading = False
            return {"ok": False, "reason": f"模型載入失敗：{e}"}

    # ---- 模型卸載 ----

    def unload_model(self) -> dict:
        """卸載模型釋放 VRAM。"""
        self._cancel_idle_timer()
        with self._state_lock:
            if self._model is None:
                return {"ok": True, "message": "模型未載入"}

            try:
                del self._model
            except Exception:
                pass
            self._model = None

        try:
            import torch
            gc.collect()
            torch.cuda.empty_cache()
        except Exception:
            pass

        print("[voxcpm2-server] 模型已卸載，VRAM 已釋放")
        return {"ok": True, "message": "模型已卸載"}

    # ---- 合成 ----

    def synthesize(
        self,
        text: str,
        reference_wav_path: Optional[str] = None,
        cfg_value: float = 2.0,
        inference_timesteps: int = 10,
    ) -> dict:
        """合成語音，回傳 WAV 路徑。GPU 鎖保護。"""
        # 確保模型已載入
        load_result = self.load_model()
        if not load_result.get("ok"):
            return load_result

        self._lock.acquire()
        try:
            self._touch()
            kwargs = {
                "text": text,
                "cfg_value": cfg_value,
                "inference_timesteps": inference_timesteps,
            }
            if reference_wav_path:
                if not os.path.exists(reference_wav_path):
                    return {"ok": False, "reason": f"reference_wav_path 不存在：{reference_wav_path}"}
                kwargs["reference_wav_path"] = reference_wav_path

            print(f"[voxcpm2-server] 合成：text={text[:50]}… ref={reference_wav_path}")
            wav = self._model.generate(**kwargs)

            # numpy 處理
            arr = wav.cpu().numpy() if hasattr(wav, "cpu") else wav
            arr = np.asarray(arr, dtype=np.float32)
            if arr.ndim == 2:
                arr = arr.squeeze(0)

            sr = self._get_sample_rate()
            line_id = f"vox_{uuid.uuid4().hex[:8]}"
            wav_path = str(OUTPUT_BASE_DIR / f"{line_id}.wav")
            sf.write(wav_path, arr, sr)

            duration = len(arr) / float(sr)
            print(f"[voxcpm2-server] ✅ 合成完成 {line_id}（{duration:.1f}s）")
            return {
                "ok": True,
                "wav_path": wav_path,
                "sample_rate": sr,
                "duration_sec": round(duration, 3),
                "line_id": line_id,
            }
        except Exception as e:
            return {"ok": False, "reason": f"合成異常：{e}"}
        finally:
            if self._lock.locked():
                self._lock.release()
            self._schedule_idle_unload()

    # ---- 內部方法 ----

    def _get_sample_rate(self) -> int:
        if self._model is None:
            return 24000  # VoxCPM2 預設
        try:
            tts = self._model.tts_model
            return int(getattr(getattr(tts, "audio_vae", None), "out_sample_rate",
                               getattr(tts, "sample_rate", 24000)))
        except Exception:
            return 24000

    def _check_vram(self) -> tuple[bool, str]:
        """回傳 (足夠?, 說明文字)。"""
        try:
            import torch
            if not torch.cuda.is_available():
                return False, "CUDA 不可用"
            # nvidia-smi 取得實際可用 VRAM
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                free_mb = int(r.stdout.strip().split()[0])
                free_gb = round(free_mb / 1024, 2)
                if free_gb < VRAM_LOAD_THRESHOLD_GB:
                    return False, f"可用 {free_gb}GB < 閾值 {VRAM_LOAD_THRESHOLD_GB}GB"
                return True, f"可用 VRAM {free_gb}GB"
        except Exception as e:
            pass
        # fallback：嘗試載入，讓 CUDA OOM 自然報錯
        return True, "無法偵測 VRAM，嘗試載入"

    def _touch(self):
        """更新最後使用時間。"""
        with self._state_lock:
            self._last_used = time.monotonic()

    def _schedule_idle_unload(self):
        """排程閒置卸載。"""
        self._cancel_idle_timer()
        if self.idle_timeout <= 0:
            return  # idle_timeout=0 表示永不自動卸載
        with self._state_lock:
            self._last_used = time.monotonic()

        def _do_unload():
            with self._state_lock:
                idle = time.monotonic() - self._last_used
                if idle < self.idle_timeout:
                    return  # 已被新請求重設
            print(f"[voxcpm2-server] 閒置 {idle:.0f}s ≥ {self.idle_timeout}s，自動卸載模型")
            self.unload_model()

        self._idle_timer = threading.Timer(self.idle_timeout, _do_unload)
        self._idle_timer.daemon = True
        self._idle_timer.start()

    def _cancel_idle_timer(self):
        if self._idle_timer:
            self._idle_timer.cancel()
            self._idle_timer = None

    def cleanup(self):
        """關機前清理。"""
        self._cancel_idle_timer()
        self.unload_model()


# ---------------------------------------------------------------------------
# 全域實例
# ---------------------------------------------------------------------------
_manager: Optional[_ModelManager] = None


def _get_manager() -> _ModelManager:
    global _manager
    if _manager is None:
        raise RuntimeError("Server not initialized")
    return _manager


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """健康檢查。"""
    mgr = _get_manager()
    st = mgr.status_dict()
    return JSONResponse({
        "status": "ok",
        **st,
    })


@app.get("/status")
async def status():
    """詳細狀態。"""
    mgr = _get_manager()
    return JSONResponse(mgr.status_dict())


@app.post("/load")
async def load_model():
    """預載模型。"""
    mgr = _get_manager()
    result = mgr.load_model()
    return JSONResponse(result)


@app.post("/unload")
async def unload_model():
    """卸載模型釋放 VRAM。"""
    mgr = _get_manager()
    result = mgr.unload_model()
    return JSONResponse(result)


@app.post("/synthesize")
async def synthesize(request_body: dict):
    """合成語音。

    Request body:
        text (str): 要合成的文字
        reference_wav_path (str, optional): 參考音檔絕對路徑
        cfg_value (float, optional): CFG 值，預設 2.0
        inference_timesteps (int, optional): 推理步數，預設 10

    Response:
        ok: bool
        wav_path: str — 生成的 WAV 檔案路徑
        sample_rate: int
        duration_sec: float
        line_id: str
        reason: str — 失敗原因
    """
    mgr = _get_manager()
    text = request_body.get("text", "").strip()
    if not text:
        return JSONResponse({"ok": False, "reason": "text 不可為空"})

    result = mgr.synthesize(
        text=text,
        reference_wav_path=request_body.get("reference_wav_path"),
        cfg_value=float(request_body.get("cfg_value", 2.0)),
        inference_timesteps=int(request_body.get("inference_timesteps", 10)),
    )
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# 清理舊 WAV 檔案（啟動時）
# ---------------------------------------------------------------------------
def _cleanup_old_output():
    """啟動時清理超過 1 小時的暫存 WAV。"""
    try:
        cutoff = time.time() - 3600
        for f in OUTPUT_BASE_DIR.glob("vox_*.wav"):
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
                print(f"[voxcpm2-server] 清理舊檔：{f.name}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    global _manager

    parser = argparse.ArgumentParser(description="VoxCPM2 TTS HTTP API Server")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--idle-timeout", type=int, default=DEFAULT_IDLE_TIMEOUT,
                        help="模型閒置自動卸載秒數（0=不卸載）")
    parser.add_argument("--model-id", type=str, default=DEFAULT_MODEL_ID)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    args = parser.parse_args()

    _manager = _ModelManager(
        model_id=args.model_id,
        idle_timeout=args.idle_timeout,
    )

    _cleanup_old_output()
    print(f"[voxcpm2-server] 🚀 VoxCPM2 TTS Server 啟動於 http://{args.host}:{args.port}")
    print(f"[voxcpm2-server] 模型：{args.model_id}")
    print(f"[voxcpm2-server] 閒置超時：{args.idle_timeout}s")
    print(f"[voxcpm2-server] 輸出目錄：{OUTPUT_BASE_DIR}")

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        _manager.cleanup()


if __name__ == "__main__":
    main()
