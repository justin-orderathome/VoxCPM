# E2E 35 項全驗計畫 — Plan / Review（開審稿）

- 日期：2026-04-15
- 分支：feat/wangdom-voice-engine
- 範圍：`~/projects/VoxCPM`（不改 Hermes 原始碼）
- 前置：P0~P3 功能皆已上線並通過實機驗收

---

## 一、Plan（階段一）

## 目標
完成 PRD 定義之 E2E-01 ~ E2E-08 共 35 項驗收，形成可追溯測試報告與缺陷清單。

## 測試範圍對應
1. E2E-01 語音早朝（P0）
2. E2E-02 節日語音問候（P1）
3. E2E-03 角色語音生成（核心）
4. E2E-04 廣播劇+脫口秀（P2）
5. E2E-05 有聲週報（P3）
6. E2E-06 輸出模式切換
7. E2E-07 容錯與回退
8. E2E-08 串流文字同步

## 實施策略（兩層）
- Layer A：離線 smoke 回歸（已存在腳本）
  - `e2e_morning_smoke.py`
  - `e2e_greeting_smoke.py`
  - `e2e_drama_smoke.py`
  - `e2e_weekly_smoke.py`
- Layer B：Discord 實機回歸（最終口徑）
  - 指令實測、語音頻道播放、文字同步體感

## 交付產物
1. `docs/reports/e2e-35-validation-2026-04-15.md`（測試報告）
2. `docs/reports/e2e-defects-2026-04-15.md`（缺陷清單，若有）
3. `scripts/wangdom/e2e_runbook.md`（重跑手冊）

## 檔案影響
- 新增：
  - `docs/reports/e2e-35-validation-2026-04-15.md`
  - `docs/reports/e2e-defects-2026-04-15.md`（若有）
  - `scripts/wangdom/e2e_runbook.md`
- 修改（若補測試缺口）：
  - `scripts/wangdom/e2e_*_smoke.py`

## 工期估計
- 準備與腳本回歸：0.5 天
- Discord 實機 35 項走查：1.0 天
- 缺陷修補與複驗：0.5~1.0 天
- 合計：2.0~2.5 天

## 驗收標準
1. 35 項皆有「Pass / Fail / Blocked」狀態
2. 每項有最少一條證據（訊息截圖文字紀錄或 log 摘要）
3. 關鍵口徑：
   - 語音可播
   - 文字同步存在
   - 串流模式文字不落後（至少首句先行）
4. 若 Fail，必附重現步驟與修補狀態

## 回滾方案
- 測試本身不改生產邏輯；如修補引入風險則回退到前一穩定 commit。

---

## 二、Review（階段二，御史六項）

1. 需求明確：✅
2. 架構設計：✅
3. 風險盤點：✅
4. 工期估計：✅
5. 驗收標準：✅
6. 回滾方案：✅

### 主要風險
- R1：Discord 網路抖動導致體感判讀偏差
  - 對策：同案例至少跑 2 次
- R2：背景服務重啟導致驗收中斷
  - 對策：每批測前先 `systemctl --user status voice-bot.service`
- R3：輸出檔累積影響磁碟
  - 對策：沿用清理機制，測後執行清理

### 評定結論
**⚠️ 有條件准**

條件：
1. 先產出 Runbook
2. 先跑 Layer A（離線）全綠
3. 再進 Layer B（Discord 實機）

---

## 三、Execute（待主公再裁示）

主公若下令：`准執行 E2E 35 項全驗`，即刻啟動。