#!/usr/bin/env python3
"""離線生成脫口秀音檔 — VoxCPM 常駐模型 + generate() 合成完整 WAV"""

import sys
import time
from pathlib import Path

import numpy as np

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent

from dotenv import load_dotenv
load_dotenv(PROJECT_DIR / ".env")

# ---------------------------------------------------------------------------
# 脫口秀台詞（與串流版一致）
# ---------------------------------------------------------------------------
TALKSHOW_SEGMENTS = [
    {
        "character": "待詔·唐伯虎",
        "text": "各位觀眾晚安！歡迎來到「王朝夜總會」加長版！我是主持人待詔唐伯虎，旁邊這位是特別來賓，禮部尚書紀曉嵐！今晚我們要大聊特聊！",
    },
    {
        "character": "禮部·紀曉嵐",
        "text": "伯虎，你這「加長版」三個字，聽著就像我編四庫全書的時候，皇帝突然說「紀愛卿，再補個續編吧」。",
    },
    {
        "character": "待詔·唐伯虎",
        "text": "哈哈哈！曉嵐兄辛苦了。不過說真的，咱們王朝最近可是熱鬧非凡。你們知道工部李冰大人最近搞了什麼嗎？他把整個王朝的基礎建設都容器化了！",
    },
    {
        "character": "禮部·紀曉嵐",
        "text": "容器化？聽起來像是把磚頭裝進箱子裡。不過我聽說確實厲害，以前部署一個服務要三天，現在三分鐘就搞定。工部的人現在天天準時下班。",
    },
    {
        "character": "待詔·唐伯虎",
        "text": "說到下班，你們聽過兵部戚繼光將軍的資安演練嗎？他上週搞了一個紅隊演習，結果把自己人全駭了一遍。連丞相曾國藩的帳密都被破了！",
    },
    {
        "character": "禮部·紀曉嵐",
        "text": "這事我聽說了。曾國藩大人氣得在政事堂拍桌子，說「本相的密碼用的是孫子兵法，怎麼可能被破！」結果戚將軍說，密碼就是「孫子兵法」四個字。",
    },
    {
        "character": "待詔·唐伯虎",
        "text": "哈哈哈哈！太經典了！對了，說到軍師諸葛亮，你們知道他最近發明了什麼嗎？一個叫「暗衛」的系統！說是可以暗中派任務，神不知鬼不覺。",
    },
    {
        "character": "禮部·紀曉嵐",
        "text": "那個我見過。刑部狄仁傑大人操盤的。上次主公問「暗衛去查一下隔壁工作室的報價」，三秒鐘結果就回來了。比六部尚書開會還快。",
    },
    {
        "character": "待詔·唐伯虎",
        "text": "三秒鐘！我畫一幅畫要三個時辰呢！不過話說回來，鴻臚寺蘇秦大人的市場策略才叫厲害。他把咱們王朝的品牌定位成「AI時代的文武百官」，結果訂單接到手軟。",
    },
    {
        "character": "禮部·紀曉嵐",
        "text": "蘇秦那張嘴，合縱連橫的功夫用在行銷上，確實是降維打擊。不過我倒覺得戶部范蠡才是真厲害，預算控管到每一文錢都有去處。",
    },
    {
        "character": "待詔·唐伯虎",
        "text": "范蠡大人說了，什麼投資回報率、什麼成本效益分析，翻譯成白話就是「花一分錢要賺三分回來」。難怪他以前能把西施送到吳國，還能全身而退。",
    },
    {
        "character": "禮部·紀曉嵐",
        "text": "伯虎，你這段子要是被范蠡聽到了，你的繪畫預算恐怕就要被砍了。不過說真的，咱們王朝的語音系統才是今晚的主題。",
    },
    {
        "character": "待詔·唐伯虎",
        "text": "沒錯！你們現在聽到的就是最新的串流語音技術！以前每說一句話就要等半天載入模型，現在是即說即播，跟真人一樣！",
    },
    {
        "character": "禮部·紀曉嵐",
        "text": "即說即播倒是真的。不過唐伯虎的聲音怎麼比我還帥？這個 voice cloning 的參考音檔，到底是誰配的？",
    },
    {
        "character": "待詔·唐伯虎",
        "text": "曉嵐兄，這個嘛，商業機密！總之，今晚的王朝夜總會到此結束！感謝各位觀眾收聽，我們下次再見！晚安！",
    },
]


def main():
    import yaml
    import struct
    import wave

    # 載入 voice profiles
    profiles_path = PROJECT_DIR / "profiles" / "voice_profiles.yaml"
    with open(profiles_path) as f:
        profiles = yaml.safe_load(f).get("voice_profiles", {})

    # 載入模型
    print("🚀 載入 VoxCPM 模型...")
    t0 = time.time()
    from voxcpm import VoxCPM
    model = VoxCPM.from_pretrained("openbmb/VoxCPM2", load_denoiser=False, optimize=False)
    print(f"✅ 模型載入完成（{time.time()-t0:.1f}s）")

    out_sr = int(getattr(model.tts_model.audio_vae, 'out_sample_rate', 48000))
    print(f"   out_sample_rate={out_sr}")

    # 逐段生成
    all_audio = []
    SILENCE_GAP = np.zeros(out_sr, dtype=np.float32)  # 1 秒間隔

    for i, seg in enumerate(TALKSHOW_SEGMENTS):
        char = seg["character"]
        text = seg["text"]
        profile = profiles.get(char)
        if not profile:
            print(f"⚠️  找不到角色 {char}，跳過")
            continue

        kwargs = {
            "text": text,
            "cfg_value": 2.0,
            "inference_timesteps": 10,
        }

        # clone 模式
        if profile.get("mode") in ("clone", "ultimate_clone") and profile.get("reference"):
            ref_path = profile["reference"]
            if not Path(ref_path).is_absolute():
                ref_path = str(PROJECT_DIR / ref_path)
            kwargs["reference_wav_path"] = ref_path

        # description
        desc = profile.get("description", "")
        if desc:
            kwargs["text"] = f"{desc}{text}"

        print(f"\n🎤 [{i+1}/{len(TALKSHOW_SEGMENTS)}] {char} 生成中...")
        t0 = time.time()
        wav = model.generate(**kwargs)  # 返回 tensor 或 numpy
        elapsed = time.time() - t0

        # 轉 numpy 1D float32
        import torch
        if isinstance(wav, torch.Tensor):
            wav = wav.squeeze().cpu().numpy()
        elif hasattr(wav, 'squeeze'):
            wav = wav.squeeze()

        wav = wav.astype(np.float32)

        # RMS 標準化
        TARGET_RMS = 0.08
        rms = np.sqrt(np.mean(wav ** 2))
        if rms > 0:
            wav = wav * (TARGET_RMS / rms)

        duration = len(wav) / out_sr
        print(f"   ✅ {duration:.2f}s, 生成 {elapsed:.1f}s, RTF={elapsed/duration:.2f}")

        all_audio.append(wav)
        if i < len(TALKSHOW_SEGMENTS) - 1:
            all_audio.append(SILENCE_GAP)

    # 合併
    print("\n🔗 合併所有段...")
    combined = np.concatenate(all_audio)
    total_duration = len(combined) / out_sr
    print(f"   總時長：{total_duration:.1f}s")

    # 轉 s16le
    pcm = (combined * 32767).clip(-32768, 32767).astype(np.int16)

    # stereo（雙聲道複製）
    stereo = np.column_stack([pcm, pcm])

    # 寫 WAV
    out_wav = PROJECT_DIR / "output" / "talkshow.wav"
    out_wav.parent.mkdir(exist_ok=True)

    with wave.open(str(out_wav), 'wb') as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(out_sr)
        wf.writeframes(stereo.tobytes())

    print(f"\n✅ WAV 已存：{out_wav} ({total_duration:.1f}s)")

    # 轉 OGG (Discord 好播)
    out_ogg = out_wav.with_suffix('.ogg')
    import subprocess
    subprocess.run([
        "ffmpeg", "-y", "-i", str(out_wav),
        "-c:a", "libopus", "-b:a", "128k",
        "-application", "audio",
        str(out_ogg)
    ], check=True, capture_output=True)
    print(f"✅ OGG 已存：{out_ogg}")

    print("\n🏁 完成！")


if __name__ == "__main__":
    main()
