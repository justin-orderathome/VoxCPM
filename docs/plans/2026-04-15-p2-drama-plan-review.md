# P2｜!drama 廣播劇功能 — Plan / Review（開審稿）

- 日期：2026-04-15
- 分支：feat/wangdom-voice-engine
- 範圍：`~/projects/VoxCPM`（不改 Hermes 原始碼）
- 前置完成：`!morning` ✅、`!greeting` ✅（語音+文字同步均通過）

---

## 一、Plan（階段一）

### 1) 需求
新增 `!drama` 指令，將多人劇本轉為可播放音訊，並維持「語音 + 文字同步」。

目標能力：
1. 接收劇本來源（文字貼文 or 檔案路徑）
2. 解析角色台詞（`dialogue_parser.py`）
3. 逐段生成並合併（`merger.py`）
4. 依 `-f/-s` 輸出（檔案/串流）
5. 同步發送文字版（摘要或逐段）

### 2) 現有能力盤點（已存在）
- `dialogue_parser.py`：已支援 `角色：台詞`、markdown、style directives
- `merger.py`：已可 parse + synthesize + pause + merge
- `discord_voice_bot.py`：已有 `_synthesize_and_play_stream/_synthesize_and_send_file`

### 3) 技術方案

#### 3.1 指令介面（首版）
`!drama [-f|-s] <script_path>`

> 首版先做「路徑輸入」，避免 Discord 長文貼上解析與命令長度問題；第二版再擴充 inline script。

#### 3.2 流程
1. 解析 mode flags
2. 驗證 script_path 存在
3. 呼叫 `DialogueParser.parse_file()` 得 segments
4. 安全檢查（段數上限、角色合法性）
5. 呼叫 `merge_dialogue()` 產生單一 WAV
6. 若 `-f`：轉 MP3 並附件發送 + 劇本文字摘要
7. 若 `-s`：播合併 WAV 到語音頻道 + 劇本文字摘要
8. 清理暫存

#### 3.3 限制（首版）
- 段數上限：30 段（避免超長佔用）
- 單次總長目標：<= 6 分鐘
- 角色名稱需在 `voice_profiles.yaml` 中存在，否則 fail-fast

### 4) 影響檔案
- 修改：`scripts/wangdom/discord_voice_bot.py`
- 可能補強：`scripts/wangdom/merger.py`（加角色驗證與上限控制）
- 新增：`scripts/wangdom/e2e_drama_smoke.py`

### 5) 工期估計
- 開發：0.5 天
- smoke + 回歸：0.5 天
- 實機 UAT：0.5 天
- 合計：1.5 天

### 6) 驗收標準
1. `!drama -f <script>`：成功輸出附件音訊 + 文字摘要
2. `!drama -s <script>`：成功語音頻道播放 + 文字摘要
3. 劇本解析至少支援 `角色：台詞` 格式
4. 角色缺失/路徑錯誤可回覆清楚錯誤
5. 連續測試 3 次無崩潰

### 7) 回滾方案
- 若 `!drama` 失效：
  - 保留 command 入口，回退成 placeholder 提示
  - 不影響 `!say / !morning / !greeting / !status`

---

## 二、Review（階段二，御史六項）

1. 需求明確：✅
2. 架構設計：✅
3. 風險盤點：✅
4. 工期估計：✅
5. 驗收標準：✅
6. 回滾方案：✅

### 主要風險
- R1：劇本超長導致生成時間過久
  - 對策：段數與時長上限，超限直接拒絕
- R2：角色名拼寫錯誤導致中途失敗
  - 對策：預檢全部角色，不通過就不啟動生成
- R3：串流模式播放長檔中斷
  - 對策：首版以「整檔合併後播放」為主，降低即時分段風險

### 評定結論
**⚠️ 有條件准**

條件：
1. 先完成 `e2e_drama_smoke.py`
2. 先上 `-f` 主路，再驗 `-s`
3. 角色預檢與段數上限必須先落地

---

## 三、Execute（待主公再裁示）

主公若下令「准執行 P2 !drama」，即按本稿進入實作。