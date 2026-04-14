# Phase 3 測試報告（17 角色音色定版首輪）

- 日期：2026-04-14
- 分支：`feat/wangdom-voice-engine`
- 生成腳本：`scripts/wangdom/generate_phase3_samples.py`
- profile 檔：`profiles/voice_profiles.yaml`

## 總結

- 總生成：17 / 17
- 成功：17 / 17（100%）
- 引擎：VoxCPM 17、Edge fallback 0
- 取樣率：48000 Hz（17/17）

## 產物位置

- 試聽音檔：`test_output/phase3_samples/*.wav`
- 結果 JSON：`test_output/phase3_samples/phase3_report.json`

## 主公試聽建議順序（先聽核心角色）

1. `主公_劉邦.wav`
2. `軍師_諸葛亮.wav`
3. `丞相_曾國藩.wav`
4. `御史_魏徵.wav`
5. `禮部_紀曉嵐.wav`

若核心角色通過，再批次確認其餘 12 角色。

## 已知技術備註

- VoxCPM Python API 不接受 `control=` 參數；風格控制需編譯進 `text`。
- `voxcpm_skill.py` 已修補此點，clone/design 皆走文字前綴描述。
