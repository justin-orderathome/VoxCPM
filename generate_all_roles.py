#!/usr/bin/env python3
"""VoxCPM2 — 17 角色 Voice Design 批次生成"""
import time
import numpy as np
import torch
import soundfile as sf
import json
import os
from voxcpm import VoxCPM

OUTPUT_DIR = "test_output/voice_profiles"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── 17 角色語音設定 ──────────────────────────────────────
ROLES = [
    {
        "id": "01_lord_liu_bang",
        "title": "主公·劉邦",
        "role": "CEO · 決策者",
        "description": "(沉穩威嚴的中年男性，聲音渾厚有力，帶帝王氣度，語調從容自信，偶有豪邁之氣)",
        "line": "眾卿平身。今日議事，有本早奏，無事退朝。",
    },
    {
        "id": "02_zhuge_liang",
        "title": "軍師·諸葛亮",
        "role": "CTO · 技術決策",
        "description": "(儒雅清朗的青年男性，音色明亮溫潤，帶書卷氣，語調沉穩內斂，從容不迫)",
        "line": "主公萬安。亮已查明局勢，請容臣細細道來。",
    },
    {
        "id": "03_zeng_guofan",
        "title": "丞相·曾國藩",
        "role": "任務分派 · 進度協調",
        "description": "(穩重踏實的中年男性，聲音低沉敦厚，語速平穩，帶有長者般的沉穩與威嚴)",
        "line": "稟主公，各部工單已分派完畢，工期估計三日可成。",
    },
    {
        "id": "04_wei_zheng",
        "title": "御史·魏徵",
        "role": "制度合規 · 進度追蹤",
        "description": "(剛正不阿的中年男性，聲音清朗堅定，語調鏗鏘有力，直言不諱)",
        "line": "臣斗膽進言，此案尚有三處未備，請主公三思。",
    },
    {
        "id": "05_ji_xiaolan",
        "title": "禮部·紀曉嵐",
        "role": "知識管理 · 筆記歸檔",
        "description": "(溫文爾雅的青年男性，聲音清澈柔和，語速適中，帶有學者氣質與些許幽默感)",
        "line": "主公，臣已將此文整理入弘文館，隨時可供查閱。",
    },
    {
        "id": "06_fan_li",
        "title": "戶部·范蠡",
        "role": "預算審計 · 財務投資",
        "description": "(精明幹練的青年男性，聲音清亮圓滑，語速稍快，帶有商人的精明與從容)",
        "line": "稟主公，本季預算尚有餘裕，可投資於語音基建。",
    },
    {
        "id": "07_guan_zhong",
        "title": "太府·管仲",
        "role": "產品策略 · 商業模式",
        "description": "(深謀遠慮的中年男性，聲音沉穩宏亮，語調充滿自信與遠見，有大格局氣場)",
        "line": "主公，此產品定位精準，若能搶先上市，必成氣候。",
    },
    {
        "id": "08_su_qin",
        "title": "鴻臚·蘇秦",
        "role": "市場策略 · 品牌定位",
        "description": "(口才便給的青年男性，聲音清脆明亮，語速流暢，富有感染力與說服力)",
        "line": "主公，臣已擬定品牌策略，只需三月便可打響名號。",
    },
    {
        "id": "09_qi_jiguang",
        "title": "兵部·戚繼光",
        "role": "安全架構 · 資安制度",
        "description": "(果敢剛毅的青年男性，聲音堅實有力，語調果斷明快，帶有軍人的紀律感)",
        "line": "稟主公，資安防禦已部署完畢，所有漏洞皆已修補。",
    },
    {
        "id": "10_di_renjie",
        "title": "刑部·狄仁傑",
        "role": "合規制度 · 風險防範",
        "description": "(沉著冷靜的中年男性，聲音低沉內斂，語調審慎精準，帶有侦探般的洞察力)",
        "line": "主公放心，暗衛已派出，三日之內必有回報。",
    },
    {
        "id": "11_li_bing",
        "title": "工部·李冰",
        "role": "基礎建設 · 部署維運",
        "description": "(樸實堅毅的中年男性，聲音厚實穩重，語速不疾不徐，帶有工匠的踏實感)",
        "line": "主公，基礎設施建設完成，系統穩定運行中。",
    },
    {
        "id": "12_tang_bohu",
        "title": "待詔·唐伯虎",
        "role": "視覺設計 · 用戶體驗",
        "description": "(風流瀟灑的青年男性，聲音清朗帶笑意，語調輕快活潑，富有藝術家氣質)",
        "line": "主公請看，這是臣最新繪製的設計稿，保您滿意。",
    },
    {
        "id": "13_bao_zheng",
        "title": "大理·包拯",
        "role": "品質管控 · 測試策略",
        "description": "(鐵面無私的中年男性，聲音渾厚莊嚴，語調嚴肅正氣，不怒自威)",
        "line": "此版本品質未達標準，臣建議打回重做，不得放水。",
    },
    {
        "id": "14_han_xin",
        "title": "將軍·韓信",
        "role": "技術領導 · 交付",
        "description": "(英武果決的青年男性，聲音清亮銳利，語速明快果斷，帶有將帥之風)",
        "line": "主公，技術攻關已完成，明日即可交付上線。",
    },
    {
        "id": "15_da_yu",
        "title": "司空·大禹",
        "role": "系統維運 · 監控告警",
        "description": "(沉穩堅韌的中年男性，聲音渾厚踏實，語調沉穩如山，帶有治水者的堅毅)",
        "line": "主公，系統監控一切正常，暫無異常告警。",
    },
    {
        "id": "16_liu_bei_envoy",
        "title": "使君·劉備",
        "role": "客戶關係 · 客訴處理",
        "description": "(溫和仁厚的青年男性，聲音柔和親切，語調真誠感人，帶有以德服人的親和力)",
        "line": "承蒙貴客信任，此事包在我們身上，定不負所託。",
    },
    {
        "id": "17_li_chunfeng",
        "title": "司天監·李淳風",
        "role": "天時服務 · 黃曆氣象",
        "description": "(神秘超然的老年男性，聲音蒼老悠遠，語調緩慢而富有玄機，帶有觀星者的飄逸)",
        "line": "今日穀雨，天時已至。宜開市、出行，忌動土、安葬。",
    },
]

# ── 載入模型 ──────────────────────────────────────────────
print("=" * 60)
print("VoxCPM2 — 17 角色 Voice Design 批次生成")
print("=" * 60)

model = VoxCPM.from_pretrained("openbmb/VoxCPM2", load_denoiser=False)
SR = model.tts_model.sample_rate
print(f"模型就緒 | SR={SR}Hz | VRAM={torch.cuda.memory_allocated(0)/1024**3:.2f}GB\n")

# ── 批次生成 ──────────────────────────────────────────────
results = []
for i, role in enumerate(ROLES):
    text = f"{role['description']}{role['line']}"
    out_path = f"{OUTPUT_DIR}/{role['id']}.wav"

    print(f"[{i+1:02d}/17] {role['title']}（{role['role']}）")
    print(f"    台詞：「{role['line']}」")

    t0 = time.time()
    wav = model.generate(text=text, cfg_value=2.0, inference_timesteps=10)
    t_gen = time.time() - t0

    # 存檔
    arr = wav.cpu().numpy() if isinstance(wav, torch.Tensor) else wav
    if arr.ndim == 2:
        arr = arr.squeeze(0)
    sf.write(out_path, arr, SR)
    dur = len(arr) / SR
    rtf = t_gen / dur
    vram = torch.cuda.memory_allocated(0) / 1024**3

    print(f"    → {dur:.1f}s | {t_gen:.2f}s | RTF {rtf:.3f} | {out_path}")

    results.append({
        "id": role["id"],
        "title": role["title"],
        "role": role["role"],
        "description": role["description"],
        "line": role["line"],
        "duration_s": round(dur, 1),
        "gen_time_s": round(t_gen, 2),
        "rtf": round(rtf, 3),
        "vram_gb": round(vram, 2),
        "file": out_path,
    })

# ── 輸出 JSON 紀錄 ──────────────────────────────────────
json_path = f"{OUTPUT_DIR}/voice_profiles.json"
with open(json_path, "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print(f"\n紀錄已存: {json_path}")

# ── 總結 ──────────────────────────────────────────────
avg_rtf = sum(r["rtf"] for r in results) / len(results)
print(f"\n{'='*60}")
print(f"完成！17 角色語音全部生成")
print(f"平均 RTF: {avg_rtf:.3f} | 檔案數: {len(results)}")
