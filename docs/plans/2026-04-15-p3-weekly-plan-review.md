# P3｜!weekly 有聲週報功能 — Plan / Review（開審稿）

- 日期：2026-04-15
- 分支：feat/wangdom-voice-engine
- 範圍：`~/projects/VoxCPM`（不改 Hermes 原始碼）
- 前置：P0 `!morning` ✅、P1 `!greeting` ✅、P2 `!drama` ✅

---

## 一、Plan（階段一）

### 1) 需求
新增 `!weekly` 指令，輸出「有聲週報 + 文字同步」。

首版範圍（MVP）：
1. 讀取最近 7 日進度摘要（先以手動/半自動資料源）
2. 產生週報段落（開場、重點、風險、下週計畫）
3. 轉語音並支援 `-f/-s` 雙模式
4. 同步送出完整文字版

### 2) 資料來源策略（分層）
- **Layer A（首版必做）**：命令參數傳入報告檔（`!weekly [-f|-s] <weekly_txt_path>`）
- **Layer B（次版）**：自動讀 Vault（如 `00-Inbox/議事紀錄/` + 指定周報索引）

> 首版先做 Layer A，可快速落地、減少 Vault 格式耦合風險。

### 3) 技術方案

#### 3.1 指令介面（首版）
`!weekly [-f|-s] <weekly_txt_path>`

#### 3.2 週報文本結構（固定 4 段）
1. 本週總覽（軍師）
2. 里程碑進展（丞相）
3. 風險與稽核（御史）
4. 下週計畫與請示（軍師）

#### 3.3 播報流程
1. 解析 flags + 路徑
2. 載入週報文字
3. 切分為 4 段 `[(角色, 台詞)]`
4. mode=file：逐段合成合併成單檔 MP3 + 文字同步
5. mode=stream：逐段串流播放 + 文字同步

### 4) 影響檔案
- 修改：`scripts/wangdom/discord_voice_bot.py`
- 新增：`scripts/wangdom/e2e_weekly_smoke.py`
- 文件：`docs/plans/2026-04-15-p3-weekly-plan-review.md`（本檔）

### 5) 工期估計
- 開發：0.5 天
- smoke + 回歸：0.5 天
- 實機 UAT：0.5 天
- 合計：1.5 天

### 6) 驗收標準
1. `!weekly -f <txt>`：有聲檔 + 文字同步
2. `!weekly -s <txt>`：語音頻道播放 + 文字同步
3. 文本過長可安全分段（不超 Discord 訊息限制）
4. 缺檔/空內容可讀錯誤
5. 連續 3 次執行穩定

### 7) 回滾方案
- `cmd_weekly` 回退為 placeholder
- 不影響 `!say/!morning/!greeting/!drama`

---

## 二、Review（階段二，御史六項）

1. 需求明確：✅
2. 架構設計：✅
3. 風險盤點：✅
4. 工期估計：✅
5. 驗收標準：✅
6. 回滾方案：✅

### 主要風險
- R1：周報原文過長，導致音檔過大/播放延遲
  - 對策：段落上限 + 文字分塊
- R2：資料來源格式不一致
  - 對策：首版只吃純文字檔，次版才接 Vault 自動彙整
- R3：串流同步落差
  - 對策：沿用 P2 已驗證的 lead-time 策略

### 評定結論
**⚠️ 有條件准**

條件：
1. 先完成 `e2e_weekly_smoke.py`
2. `-f` 先通，再驗 `-s`
3. 需保證文字同步必出（不可只摘要）

---

## 三、Execute（待主公裁示）

主公若下令：`准執行 P3 !weekly`，即按本稿進入實作。