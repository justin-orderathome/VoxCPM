#!/usr/bin/env python3
"""Phase 3：逐角色生成 clone reference 候選音檔

每位角色生成一段 ~8-10 秒台詞，供主公試聽 → 准 → 升級 clone
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from voxcpm_skill import VoxCpmSkill
import soundfile as sf
import numpy as np

PROFILES = {
    "01_主公·劉邦": {
        "description": "(沉穩威嚴的中年男性，聲音渾厚有力，帶帝王氣度，語調從容自信，偶有豪邁之氣)",
        "text": "眾卿平身。今日議事，有本早奏，無事退朝。天下之事，皆繫於朕之一念。",
    },
    "03_丞相·曾國藩": {
        "description": "(穩重踏實的中年男性，聲音低沉敦厚，語速平穩，帶有長者般的沉穩與威嚴)",
        "text": "稟主公，各部工單已分派完畢，工期估計三日可成。臣必竭盡所能，不負所託。",
    },
    "04_御史·魏徵": {
        "description": "(剛正不阿的中年男性，聲音清朗堅定，語調鏗鏘有力，直言不諱)",
        "text": "臣斗膽進言，此案尚有三處未備，請主公三思。臣寧可得罪於上，不敢欺瞞主公。",
    },
    "06_戶部·范蠡": {
        "description": "(精明幹練的青年男性，聲音清亮圓滑，語速稍快，帶有商人的精明與從容)",
        "text": "稟主公，本季預算尚有餘裕，可投資於語音基建。臣已精算過，報酬率相當可觀。",
    },
    "07_太府·管仲": {
        "description": "(深謀遠慮的中年男性，聲音沉穩宏亮，語調充滿自信與遠見，有大格局氣場)",
        "text": "主公，此產品定位精準，若能搶先上市，必成氣候。臣願以項上人頭擔保此策可行。",
    },
    "08_鴻臚·蘇秦": {
        "description": "(深沉內斂的青年男性，聲音低沉有磁性，語速沉穩流暢，帶有謀略家的自信與從容)",
        "text": "主公，臣已擬定品牌策略，只需三月便可打響名號。合縱連橫，天下皆知我朝威名。",
    },
    "09_兵部·戚繼光": {
        "description": "(果敢剛毅的青年男性，聲音堅實有力，語調果斷明快，帶有軍人的紀律感)",
        "text": "稟主公，資安防禦已部署完畢，所有漏洞皆已修補。末將敢立軍令狀，萬無一失。",
    },
    "10_刑部·狄仁傑": {
        "description": "(沉著冷靜的中年男性，聲音低沉內斂，語調審慎精準，帶有偵探般的洞察力)",
        "text": "主公放心，暗衛已派出，三日之內必有回報。臣已掌握線索，真相即將大白於天下。",
    },
    "11_工部·李冰": {
        "description": "(風流瀟灑的青年男性，聲音清朗帶笑意，語調輕快活潑，富有藝術家氣質)",
        "text": "主公，基礎設施建設完成，系統穩定運行中。臣親自測試過，固若金湯，請主公放心。",
    },
    "13_大理·包拯": {
        "description": "(鐵面無私的中年男性，聲音渾厚莊嚴，語調嚴肅正氣，不怒自威)",
        "text": "此版本品質未達標準，臣建議打回重做，不得放水。律法面前，人人平等，不容絲毫苟且。",
    },
    "14_將軍·韓信": {
        "description": "(英武果決的青年男性，聲音清亮銳利，語速明快果斷，帶有將帥之風)",
        "text": "主公，技術攻關已完成，明日即可交付上線。兵貴神速，臣已部署妥當，隨時可戰。",
    },
    "15_司空·大禹": {
        "description": "(沉穩堅韌的中年男性，聲音渾厚踏實，語調沉穩如山，帶有治水者的堅毅)",
        "text": "主公，系統監控一切正常，暫無異常告警。臣日夜堅守，三過家門而不入，請主公安心。",
    },
    "16_使君·劉備": {
        "description": "(溫和仁厚的青年男性，聲音柔和親切，語調真誠感人，帶有以德服人的親和力)",
        "text": "承蒙貴客信任，此事包在我們身上，定不負所託。以誠待人，以信立業，是我們的宗旨。",
    },
    "17_司天監·李淳風": {
        "description": "(靈動活潑的青年男性，聲音清亮有朝氣，語調輕快靈巧，帶有少年天才的自信與俏皮)",
        "text": "今日穀雨，天時已至。宜開市出行，忌動土安葬。主公，臣觀天象，大吉之兆也。",
    },
}

def main():
    out_dir = Path("test_output/clone_candidates")
    out_dir.mkdir(parents=True, exist_ok=True)

    skill = VoxCpmSkill(profiles_path="profiles/voice_profiles.yaml")
    model = skill._load_model()
    sample_rate = int(model.tts_model.sample_rate)

    results = []
    for char_key, cfg in PROFILES.items():
        idx = char_key.split("_")[0]
        full_text = f"{cfg['description']}{cfg['text']}"
        out_path = out_dir / f"{char_key}_candidate.wav"

        print(f"[{idx}] Generating {char_key}...", flush=True)
        t0 = time.time()

        wav = model.generate(text=full_text, cfg_value=2.0, inference_timesteps=10)
        arr = wav.cpu().numpy() if hasattr(wav, "cpu") else wav
        if getattr(arr, "ndim", 1) == 2:
            arr = arr.squeeze(0)

        # RMS normalize
        rms = np.sqrt(np.mean(arr ** 2))
        if rms > 1e-6:
            gain = min(0.08 / rms, 0.95 / np.max(np.abs(arr)))
            arr = arr * gain

        sf.write(str(out_path), arr, sample_rate)
        elapsed = time.time() - t0
        duration = len(arr) / sample_rate

        print(f"    → {out_path.name} ({duration:.1f}s, {elapsed:.1f}s)", flush=True)
        results.append({
            "character": char_key,
            "duration_sec": round(duration, 1),
            "output": str(out_path),
        })

    # Save manifest
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"\n✅ Done. {len(results)} candidates → {out_dir}/")
    print(f"Manifest: {manifest_path}")

if __name__ == "__main__":
    main()
