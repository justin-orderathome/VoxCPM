# E2E 35 項全驗報告（2026-04-15）

- 專案：VoxCPM / 崴勝王朝語音 Bot
- 分支：`feat/wangdom-voice-engine`
- 執行時間：2026-04-15 15:24~15:27 CST
- 執行層級：Layer A（離線 smoke）+ Layer B（主公 Discord 實機回報）

## 總結
- 總項目：35
- Pass：35
- Fail：0
- Blocked：0

## 證據摘要
- 離線 smoke 全綠：
  - `e2e_morning_smoke.py` ✅
  - `e2e_greeting_smoke.py` ✅
  - `e2e_drama_smoke.py` ✅
  - `e2e_weekly_smoke.py` ✅
  - `e2e_aux_smoke.py` ✅
- 服務狀態：`voice-bot.service` active ✅
- 主公實機回報：
  - `!morning`、`!greeting`、`!drama`、`!weekly` 皆完成 UAT
  - 串流同步問題已修補，達成「文字先於聲音 / 逐句同步」

## 驗收明細（35 項）

| ID | 類別 | 測試點 | 結果 | 證據 |
|---|---|---|---|---|
| 01 | E2E-01 | `!morning -f` 可用 | PASS | morning smoke + 主公UAT |
| 02 | E2E-01 | `!morning -s` 可用 | PASS | morning smoke + 主公UAT |
| 03 | E2E-01 | 早朝文字同步存在 | PASS | 主公UAT |
| 04 | E2E-01 | 天機報串接正常 | PASS | morning smoke |
| 05 | E2E-02 | `!greeting -f` 可用 | PASS | greeting smoke + 主公UAT |
| 06 | E2E-02 | `!greeting -s` 可用 | PASS | greeting smoke + 主公UAT |
| 07 | E2E-02 | 問候文字同步存在 | PASS | 主公UAT |
| 08 | E2E-02 | 節慶/節氣 fallback 正常 | PASS | greeting smoke |
| 09 | E2E-03 | `!say -f` 路徑正常 | PASS | 既有功能回歸 + aux smoke |
| 10 | E2E-03 | `!say -s` 路徑正常 | PASS | 既有功能回歸 |
| 11 | E2E-03 | `!voices` 角色清單正常 | PASS | aux smoke |
| 12 | E2E-03 | `!status` 狀態輸出正常 | PASS | aux smoke |
| 13 | E2E-04 | `!drama -f` 可用 | PASS | drama smoke + 主公UAT |
| 14 | E2E-04 | `!drama -s` 可用 | PASS | drama smoke + 主公UAT |
| 15 | E2E-04 | 廣播劇文字同步（完整） | PASS | 主公UAT（修補後） |
| 16 | E2E-04 | 串流逐句同步 | PASS | 主公UAT（修補後） |
| 17 | E2E-04 | 文字先於聲音 | PASS | 主公UAT（修補後） |
| 18 | E2E-05 | `!weekly -f` 可用 | PASS | weekly smoke + 主公UAT |
| 19 | E2E-05 | `!weekly -s` 可用 | PASS | weekly smoke + 主公UAT |
| 20 | E2E-05 | 週報文字同步完整 | PASS | 主公UAT（修補後） |
| 21 | E2E-06 | `!mode auto` 正常 | PASS | aux smoke |
| 22 | E2E-06 | `!mode file` 正常 | PASS | aux smoke |
| 23 | E2E-06 | `!mode stream` 正常 | PASS | aux smoke |
| 24 | E2E-06 | flag 優先於全域 mode | PASS | smoke 設計覆蓋 |
| 25 | E2E-06 | auto 模式可用 | PASS | smoke 覆蓋 |
| 26 | E2E-07 | `!play` 缺檔錯誤可讀 | PASS | aux smoke |
| 27 | E2E-07 | `!drama` 缺檔錯誤可讀 | PASS | aux smoke |
| 28 | E2E-07 | `!weekly` 缺檔錯誤可讀 | PASS | aux smoke |
| 29 | E2E-07 | `!stop` 無播放容錯 | PASS | aux smoke |
| 30 | E2E-07 | Edge TTS fallback 可用 | PASS | `voxcpm_skill.py --force-engine edge_tts` |
| 31 | E2E-08 | morning 串流文字同步 | PASS | 主公UAT |
| 32 | E2E-08 | greeting 串流文字同步 | PASS | 主公UAT |
| 33 | E2E-08 | drama 串流逐句同步 | PASS | 主公UAT（修補後） |
| 34 | E2E-08 | weekly 串流逐句同步 | PASS | 主公UAT（修補後） |
| 35 | E2E-08 | 串流同步體感達標 | PASS | 主公最終 OK |

## 結論
本輪 E2E 35 項全驗結果：**全數通過（35/35）**。
