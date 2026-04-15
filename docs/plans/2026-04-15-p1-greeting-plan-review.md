# P1｜!greeting 節日問候功能 — Plan / Review 草案

- 日期：2026-04-15
- 分支：feat/wangdom-voice-engine
- 範圍：`~/projects/VoxCPM`（不改 Hermes 原始碼）
- 前置成果：!morning 已完成、UAT 通過（語音+文字同步）

---

## 一、Plan（階段一）

### 1) 需求定義
新增 Discord 指令 `!greeting`：
- 可自動判定今日節慶（節氣/農曆節慶/國定假日）
- 產生節日問候語音
- 支援 `-f`（檔案）/ `-s`（串流）雙模式
- 全程保留「語音 + 文字同步」鐵律

### 2) 技術方案

#### 2.1 資料來源
- 使用 `~/.hermes/scripts/tianji-report.py` 作為天時資料來源（沿用 !morning）
- 取用欄位：
  - `today_festival`（今日節慶）
  - `solar_term`（節氣）
  - `holiday_check`（是否休沐/國定假日）

#### 2.2 文案組裝策略（Greeting Builder）
在 `discord_voice_bot.py` 新增：
- `_build_greeting_lines(report: dict) -> list[tuple[str, str]]`
- 規則：
  1. 若 `today_festival` 非空 → 優先節慶問候
  2. 若無節慶但有節氣資訊 → 輸出節氣問候
  3. 否則輸出日常問候
- 角色編排（首版）：
  - 司天監·李淳風（節慶說明）
  - 待詔·唐伯虎（文案潤飾）
  - 軍師·諸葛亮（收束與行動句）

#### 2.3 指令流程
`!greeting [ -f | -s ]`
1. 解析 flags（沿用 `_parse_mode_flags`）
2. 載入 tianji report
3. 生成 greeting lines
4. 依 mode 逐行呼叫：
   - stream: `_synthesize_and_play_stream`
   - file: `_synthesize_and_send_file`
5. 結尾送出完成訊息

### 3) 影響檔案
- 修改：`scripts/wangdom/discord_voice_bot.py`
- 新增（測試）：`scripts/wangdom/e2e_greeting_smoke.py`
- 更新（文件）：`docs/plans/2026-04-15-p1-greeting-plan-review.md`（本檔）

### 4) 工期估計
- 開發：0.5 天
- 離線 smoke：0.5 天
- Discord 實機 UAT：0.5 天
- 合計：1.5 天（含修補緩衝）

### 5) 驗收標準
1. `!greeting -f` 可輸出語音檔 + 同步文字
2. `!greeting -s` 可在語音頻道播報 + 同步文字
3. 有節慶時使用節慶文案，無節慶時 fallback 節氣/日常文案
4. 連續執行 3 次無崩潰、無卡死

### 6) 回滾方案
- 若新流程異常：
  1. 保留原 command 入口
  2. 將 `cmd_greeting` 邏輯暫回 placeholder 訊息
  3. 不影響既有 `!say / !morning / !status` 功能

---

## 二、Review（階段二，御史六項）

### 六項完備性檢查
1. 需求明確：✅
2. 架構設計：✅
3. 風險盤點：✅
4. 工期估計：✅
5. 驗收標準：✅
6. 回滾方案：✅

### 風險清單
- 風險A：節慶資料為空，文案可能過短
  - 對策：fallback 節氣/日常模板
- 風險B：串流模式多段播報衝突
  - 對策：沿用現有 active stream 檢查
- 風險C：文案過長導致體感延遲
  - 對策：每段控制在 1~2 句

### 評定結果
- 結論：⚠️ 有條件准
- 條件：需先完成 `e2e_greeting_smoke.py`，再進 Discord 實機 UAT

---

## 三、Execute（待主公裁示）

待主公下令：「准執行 P1 !greeting」後，進入實作。
