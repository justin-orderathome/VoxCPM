# PRD｜!morning 語音早朝整合（Phase P0）

- Owner: 軍師·諸葛亮（技術） / 工部·李冰（基建） / 司天監·李淳風（天時資料）
- Status: Draft for Review
- Priority: P0

## 1) 需求目標（Why）
目前 Discord Bot 已有 `!morning`（手動）與 `!morningcron`（排程框架），但尚未完成「天機曆報文 → 多角色語音 → 文字同步/語音推播」的一體化穩定流程。

本期目標：
1. 讓 `!morning` 手動觸發可穩定輸出（stream/file/auto）
2. 讓 `!morningcron` 每日固定時間自動播報
3. 支援排程 stream 模式可指定 `voice_channel_id`（無 author 仍可播）
4. 失敗時可降級、可追蹤、可回滾

## 2) 範圍（Scope）
### In Scope
- `discord_voice_bot.py`
  - 排程設定讀寫（JSON）
  - scheduler background loop
  - `!morningcron on/off/status`
  - `_run_morning_broadcast()` 共用流程
  - stream 模式語音頻道解析（preferred voice channel fallback）
- 天時報文接線：`tianji-report.py` 讀取與容錯
- Discord 文字狀態回報（開始、降級、完成、錯誤）
- 排程時區固定 Asia/Taipei

### Out of Scope（本期不做）
- STT+VAD 即時雙向語音
- `!greeting` / `!drama` / `!weekly` 完整落地
- 語音 IP 商業化與決策留痕制度

## 3) 使用者故事（User Stories）
1. 作為群管理者，我要能 `!morningcron on 07:00 stream <voice_channel_id>`，讓 Bot 每日自動播報。
2. 作為成員，我要能隨時 `!morningcron status` 看到排程狀態與最後執行日。
3. 作為操作者，我要在 stream 條件不足時自動降級到 file 並得到提示。

## 4) 技術設計（How）
### 4.1 核心流程
1. Command 收到 `!morning` / scheduler hit
2. `_run_morning_broadcast(...)` 統一執行
3. 載入 tianji report，生成文本 lines
4. 判斷模式：
   - stream：可解析 voice target（author voice 或 config voice_channel_id）
   - auto：有 voice target 用 stream，否則 file
   - file：直接生成檔案上傳
5. 發送完成狀態；異常時記錄並回覆。

### 4.2 可靠性設計
- `last_run_date` 避免同日重複觸發
- `try/except` 包覆 scheduler tick，防止背景任務崩潰
- config 落盤 `profiles/morning_schedule.json`（重啟可恢復）

### 4.3 觀測性
- 啟動日誌：`morning scheduler background task 已建立`、`Morning scheduler 已啟動`
- 指令錯誤可見：`CommandNotFound` / 參數錯誤 / channel resolve 失敗
- 執行結果標準化：開始、降級、完成、失敗

## 5) 風險盤點（Risks）
1. **舊程序未重啟** → 新指令不存在（已發生）
   - Mitigation: 發版後固定 `systemctl --user restart voice-bot.service`
2. **stream 無可用語音頻道**
   - Mitigation: 支援 `voice_channel_id` + auto fallback file
3. **天時資料載入失敗**
   - Mitigation: 降級文本與錯誤提示，任務不中斷
4. **長時間 TTS 造成阻塞**
   - Mitigation: 背景 loop 與 broadcast 分離，避免整體卡死

## 6) 工期估算（Estimate）
- D0: 指令與排程整合、重啟驗證（已完成）
- D1: 穩定性補強（錯誤訊息、fallback、狀態訊息）
- D2: 測試與驗收、文件更新

## 7) 驗收標準（Acceptance Criteria）
1. `!morningcron status` 可回傳：enabled/time/mode/guild/text_channel/voice_channel/last_run_date
2. `!morningcron on HH:MM stream <voice_channel_id>` 可成功設定並落盤
3. 到點後排程可自動觸發至少 1 次，且 `last_run_date` 更新
4. stream 缺少語音目標時，`auto` 會降級 `file` 且有提示
5. 服務重啟後排程設定仍保留且背景任務自動啟動

## 8) 回滾方案（Rollback）
- 代碼回退至前一穩定 commit（保留舊 `!morning` 路徑）
- 停用排程：`!morningcron off`
- 若需緊急止血：`systemctl --user stop voice-bot.service`

## 9) 執行清單（Execute Checklist）
- [ ] 補齊 command usage 錯誤訊息與參數邊界測試
- [ ] 以近分鐘時間做一次排程 E2E（on/status/觸發/完成）
- [ ] 更新 `voxcpm-tts` skill 的 morningcron 使用段落
- [ ] 納入 E2E 35 項測試清單
