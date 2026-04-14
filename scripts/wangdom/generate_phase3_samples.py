#!/usr/bin/env python3
"""Phase 3：17 角色試聽樣本批次生成"""

from __future__ import annotations

import json
from pathlib import Path

from voxcpm_skill import VoxCpmSkill

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "profiles/voice_profiles.yaml"
OUTPUT_DIR = ROOT / "test_output/phase3_samples"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

LINES = {
    "主公·劉邦": "眾卿平身，今日議事即刻開始。",
    "軍師·諸葛亮": "主公萬安，亮已備妥全局方案。",
    "丞相·曾國藩": "稟主公，各部工單與工期已排定。",
    "御史·魏徵": "臣請核對驗收標準，避免偏差。",
    "禮部·紀曉嵐": "主公，臣已將紀錄歸檔入弘文館。",
    "戶部·范蠡": "本季預算尚有餘裕，可支應此案。",
    "太府·管仲": "此策可兼顧產品定位與商業模式。",
    "鴻臚·蘇秦": "臣建議先行試點，再擴大市場。",
    "兵部·戚繼光": "資安防線已加固，請主公安心。",
    "刑部·狄仁傑": "暗衛已就位，風險項即刻查辦。",
    "工部·李冰": "基礎建設穩定，部署可隨時啟動。",
    "待詔·唐伯虎": "此版視覺已修至可交付狀態。",
    "大理·包拯": "品質未達門檻者，一律打回重驗。",
    "將軍·韓信": "技術攻關已成，明日可交付上線。",
    "司空·大禹": "監控告警正常，系統運行平穩。",
    "使君·劉備": "客戶端回饋正向，續約可期。",
    "司天監·李淳風": "天時已至，今宜開市出行。",
}


def main() -> None:
    skill = VoxCpmSkill(profiles_path=PROFILES)
    reports = []

    for character, text in LINES.items():
        out_file = OUTPUT_DIR / f"{character.replace('·', '_')}.wav"
        result = skill.synthesize(
            text=text,
            character=character,
            output_path=out_file,
            force_engine="voxcpm",
        )
        reports.append(result)
        print(f"[{character}] -> {result.get('engine')} | {result.get('output_path')}")

    report_path = OUTPUT_DIR / "phase3_report.json"
    report_path.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"report={report_path}")


if __name__ == "__main__":
    main()
