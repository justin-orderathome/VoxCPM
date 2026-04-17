# PRD｜語音播報統一引擎重構（Broadcast Unification）

- Owner: 軍師·諸葛亮（架構）/ 工部·李冰（執行）
- Status: Ready for Review（待御史評定）
- Priority: P1
- Depends on: P0 morning integration（已完成）
- Related roadmap: `!greeting` / `!drama` / `!weekly`、E2E 35、VibeVoice 定位、avatar webhook

---

## 1) 需求目標（Why）

現有四個語音播報指令（`!morning`、`!greeting`、`!drama`、`!weekly`）各自維護一套近似流程：

`資料載入 → lines 建構 → mode 判定 → stream/file 輸出 → 文字同步`。

已造成：
1. **重複程式碼**（維護成本高，易分歧）
2. **行為不一致**（進度提示、錯誤處理、auto 降級不完全一致）
3. **擴充困難**（新增播報類指令成本高）

本 PRD 目標：
- 以 `_run_broadcast()` 統一播報引擎
- 讓 `!greeting` / `!drama` / `!weekly` 進入一致流程
- 為 E2E 35 項驗收建立統一測試面

---

## 2) 範圍（Scope）

### In Scope
- `BroadcastConfig` dataclass（統一配置）
- `_run_broadcast(ctx, config)` 共用引擎
- 四指令遷移：`!morning` → `!greeting` → `!drama` → `!weekly`
- scheduler 路徑改接共用引擎
- 統一 stream/file/auto 行為（含 auto 降級提示）

### Out of Scope（本期不做）
- STT+VAD 即時對話
- 新增語音商業化功能
- 多引擎混播（VoxCPM + VibeVoice 同回合）

---

## 3) 使用者故事（User Stories）

1. 作為開發者，我要只實作 `_build_*_lines()` 即可新增播報功能。
2. 作為主公，我要所有播報命令一致支援 `auto/stream/file`。
3. 作為維運者，我要錯誤訊息、降級訊息、完成訊息都統一可觀測。

---

## 4) 現況盤點（As-Is）

### 已完成
- `!morning` 與 `!morningcron` 路徑可用
- `!greeting` / `!drama` / `!weekly` 命令入口已存在
- clone refs 17 份 RMS 正規化完成（`normalization_report.json`）
- systemd 常駐（`voice-bot.service`）已配置

### 未完成
- 廣播指令仍多處重複流程
- E2E 35 項尚未整批驗收
- VibeVoice 定位尚待裁示
- avatar webhook 流程未串接

---

## 5) 技術設計（To-Be）

### 5.1 BroadcastConfig

```python
@dataclass
class BroadcastConfig:
    label: str
    lines: list[tuple[str, str]]
    mode: str  # auto / stream / file
    mood: str = "沉穩"
    ambience: str = "none"
    preferred_voice_channel_id: Optional[int] = None

    # 離線渲染（drama / weekly）
    pre_rendered_wav: Optional[str] = None
    transcript_timeline: Optional[list[dict]] = None

    # metadata
    source_path: Optional[str] = None
    summary_extra: Optional[str] = None
```

### 5.2 _run_broadcast() 共用流程

1. guild guard
2. mode resolve（auto / stream / file）
3. auto 降級提示（stream 不可用時改 file）
4. 輸出分流：
   - A. `pre_rendered_wav`（drama/weekly）
   - B. 逐段 TTS（morning/greeting）
5. 逐句/逐段文字同步
6. 完成訊息 + 例外處理

### 5.3 指令遷移映射

| 指令 | 新流程 |
|---|---|
| `!morning` | `_build_morning_lines` → `_run_broadcast` |
| `!greeting` | `_build_greeting_lines` → `_run_broadcast` |
| `!drama` | `_render_drama_audio` + timeline → `_run_broadcast` |
| `!weekly` | `_render_weekly_audio` + timeline → `_run_broadcast` |

---

## 6) 分期計畫（Roadmap / 依賴）

### Phase P1（本 PRD主體）
1. 統一引擎 + 四指令遷移
2. scheduler 改接
3. compile + smoke

### Phase P2（同批併行規劃）
1. **E2E 35 項**：建立矩陣與執行節點
2. **VibeVoice 定位三案比較**：維持原案 / 同測 / 重評
3. **avatar webhook 整合**：公開 URL → webhook `avatar_url`

依賴關係：
- P2-E2E 需在 P1 完成後做 full run
- avatar webhook 與語音引擎解耦，可平行規劃
- VibeVoice 決策不阻塞 P1，但影響 P2+ 的產品路線

---

## 7) 風險盤點（Risks）

| 風險 | 等級 | 緩解 |
|---|---|---|
| 行為回歸（重構後語音流程差異） | 中 | 每遷移一指令即 smoke + compile |
| drama/weekly 時間軸同步不準 | 中 | 維持 timeline 欄位，抽象層不改時間戳語意 |
| scheduler 引用舊函數 | 低 | 遷移後做 grep 檢查 `_run_morning_broadcast` 引用 |
| 429 配額問題影響驗收 | 中 | 驗收期間使用 fallback provider（OpenRouter/Gemini） |

---

## 8) 工期估算（Estimate）

| 工作項 | 估時 |
|---|---|
| BroadcastConfig + _run_broadcast | 1.3h |
| morning/greeting 遷移 | 0.8h |
| drama/weekly 遷移 | 1.0h |
| scheduler 改接 + 清理 | 0.4h |
| smoke + compile | 0.5h |
| **合計** | **~4.0h** |

---

## 9) 驗收標準（Acceptance Criteria）

1. 四指令皆走 `_run_broadcast`
2. `auto/stream/file` 行為一致
3. stream 不可用時都有明確降級提示
4. `python -m py_compile scripts/wangdom/discord_voice_bot.py` 通過
5. smoke 測試（morning/greeting/drama/weekly）全綠
6. scheduler 觸發日誌與 `last_run_date` 正常

---

## 10) 回滾方案（Rollback）

- 每階段獨立 commit，可逐顆 revert
- 先保留舊函數，待全驗收後刪除
- 如遇 production 異常：
  1) `!morningcron off`
  2) 回退至遷移前 commit

---

## 11) 御史評定六項檢查（Review Gate）

| 檢查項 | 狀態 |
|---|---|
| 需求明確 | ✅ |
| 架構設計 | ✅ |
| 風險盤點 | ✅ |
| 工期估計 | ✅ |
| 驗收標準 | ✅ |
| 回滾方案 | ✅ |

> 評定建議：**⚠️ 有條件准**（條件：先配置 fallback provider，避免 429 導致驗收中斷）

---

## 12) 執行清單（Execute Checklist）

- [ ] 建立 `BroadcastConfig`
- [ ] 實作 `_run_broadcast`
- [ ] 遷移 `!morning`
- [ ] 遷移 `!greeting`
- [ ] 遷移 `!drama`
- [ ] 遷移 `!weekly`
- [ ] scheduler 改接
- [ ] 移除舊流程
- [ ] compile + smoke
- [ ] 更新 `voxcpm-tts` skill

---

## 13) 關聯文件

- `docs/prd/morning-voice-integration-prd.md`
- `scripts/wangdom/discord_voice_bot.py`
- `scripts/wangdom/e2e_*_smoke.py`
- `test_output/voice_profiles/clone_refs/normalization_report.json`
