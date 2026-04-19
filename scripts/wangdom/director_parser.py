#!/usr/bin/env python3
"""導演劇本解析器 v2 (Director Parser)

將導演劇本（director script）解析為結構化 JSON，供 merger.py 逐段生成語音。

解析流程：
  1. yaml.safe_load → frontmatter（標準庫，不走 LLM）
  2. body → SHA-256 hash → 檢查快取
  3. 快取命中 → 直接返回
  4. 快取未命中 → LLM 全量解析（prompt + JSON schema）
  5. JSON schema 驗證 → 失敗則自動調教循環（最多 3 次）
  6. 寫入快取 → 返回 DirectorScript

用法：
  python director_parser.py 導演劇本.md -o 語音版.json
  python director_parser.py 導演劇本.md --validate-only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 同目錄 import
_wangdom_dir = str(Path(__file__).parent)
if _wangdom_dir not in sys.path:
    sys.path.insert(0, _wangdom_dir)

# ─── 日誌 ──────────────────────────────────────────────────

logger = logging.getLogger("director_parser")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# 全局解析狀態（供外部 heartbeat 輪詢）
parse_status = ""


# ─── 常量 ──────────────────────────────────────────────────

VALID_AMBIENCES = {"none", "studio", "hall", "battle", "rain", "cave", "wind", "forest", "crowd"}
VALID_TRANSITIONS = {"none", "chime", "gong", "drum", "rain", "laugh"}
VALID_COMPILE_MODES = {"dialogue", "narration", "mixed"}
DEFAULT_NARRATOR = "禮部·紀曉嵐"

MAX_RETRIES = 3

TST = timezone(timedelta(hours=8))  # 台北時區


# ─── Frontmatter 解析 ─────────────────────────────────────

def extract_frontmatter(script: str) -> Tuple[dict, str]:
    """從 Markdown 文字中提取 YAML frontmatter。

    Returns:
        (meta_dict, body_text) — 若無 frontmatter，meta 為空 dict。
    """
    if not script.startswith("---"):
        return {}, script

    lines = script.splitlines()
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break

    if end_idx is None:
        return {}, script

    fm_text = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1:])

    try:
        import yaml
        meta = yaml.safe_load(fm_text) or {}
    except Exception:
        meta = {}

    return meta, body


# ─── 快取機制 ─────────────────────────────────────────────

def _content_hash(text: str) -> str:
    """計算文字內容的 SHA-256 hash（用於快取鍵）"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _cache_path(script_dir: str | Path, content_hash: str) -> Path:
    """取得快取檔案路徑"""
    cache_dir = Path(script_dir) / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{content_hash}.json"


def _read_cache(cache_file: Path) -> Optional[dict]:
    """讀取快取，返回解析後的 dict 或 None"""
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        data["source"]["cache_hit"] = True
        logger.info(f"快取命中：{cache_file.name}")
        return data
    except Exception as e:
        logger.warning(f"快取損壞，將重新解析：{e}")
        return None


def _write_cache(cache_file: Path, data: dict) -> None:
    """寫入快取"""
    try:
        cache_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(f"快取寫入：{cache_file.name}")
    except Exception as e:
        logger.warning(f"快取寫入失敗：{e}")


# ─── LLM 解析 ─────────────────────────────────────────────

SEMANTIC_PROMPT = """你是一位廣播劇劇本語義解析器。你的任務是從 Markdown 導演劇本中提取語義段落清單。

## 輸入格式

導演劇本是 Markdown 文件，body 部分包含：
- 幕標題：## 【第X幕：名稱】
- 場景 meta：（時間：...｜地點：...｜人物：...｜音場：...｜轉場：...）
- 角色對話：emoji + 角色名 + [演技標籤] +（表演註記）+ ：+ 台詞
- 散文段落：無角色標頭的描述文本
- 註解/引用塊（> 開頭）：不納入

## 三種括號

1. `（）` 全形 — 場景 meta（獨立行）或表演註記
2. `【】` 全形 — 幕/場標題
3. `[]` 半形 — 演技標籤

## 你只需要輸出

一個 JSON 陣列，每個元素代表一個段落，**只要這幾個欄位**：

```json
{
  "scene_number": 1,
  "type": "dialogue",
  "speaker": "待詔·唐伯虎",
  "text": "台詞全文",
  "expression_tags": ["laughing"],
  "mood": "開心"
}
```

### 欄位說明

- `scene_number`：整數，遇到 ## 【第X幕】或 （場景 meta 獨立行）時遞增
- `type`：`"dialogue"`（有角色標頭）或 `"narration"`（散文）
- `speaker`：
  - 對話段：移除 emoji，保留 `部門·角色名` 格式。移除 [] 和（）。例如 `🌸 唐伯虎 [laughing]（搖扇子）：` → `"待詔·唐伯虎"`
  - 散文段：使用 frontmatter 的 narrator
- `text`：完整台詞或散文原文，不改寫不省略
- `expression_tags`：`[]` 半形標籤陣列，無則空陣列 `[]`
- `mood`：從 `（）` 表演註記推斷的心情，無則 `null`

### 場景列表

同時在陣列之前，輸出 `scenes` 陣列：

```json
{
  "scenes": [
    {"scene_number": 1, "title": "第一幕：七項的變遷", "ambience": "studio", "transition": "chime"}
  ],
  "segments": [...]
}
```

## 規則

- 不要遺漏任何段落（含散文）
- 散文多行合併為一個 segment
- 台詞多行也合併（到空行結束）
- 註解（> 開頭）、引用塊、HTML 註解、取材來源、簽名行不納入
- **只輸出 JSON，不要其他文字**
- 輸出用 ```json 包裹"""

RETRY_PROMPT_TEMPLATE = """上一次解析結果有錯誤，請修正。

## 原始劇本
```
{body}
```

## 上次輸出（有錯誤）
```json
{previous_output}
```

## 錯誤清單
{errors}

請修正上述錯誤，重新輸出完整的 JSON。只輸出 JSON，不要其他文字。"""


def _split_by_scenes(body: str) -> List[dict]:
    """將 body 按場景邊界分割為多個場景區塊。

    分批錨點（優先序）：
    1. 場景 meta 獨立行：`（時間：...｜...）`  — 最穩定，100% 出現
    2. 幕/段子標題：`## 【...】`              — 次穩定

    每個區塊 = 從一個錨點到下一個錨點之間的所有內容。
    第一批包含 preamble（標題區/引用區）。

    Returns:
        [{"scene_number": N, "title": "...", "body": "..."}]
    """
    lines = body.split("\n")

    # 找出所有場景邊界行
    boundaries = []  # (line_index, title)
    for i, line in enumerate(lines):
        stripped = line.strip()

        # 錨點 1：場景 meta 獨立行（以「（時間」或「（音場」開頭）
        if stripped.startswith("（") and stripped.endswith("）") and "：" in stripped:
            # 向上找幕標題（如果有的話）
            title = ""
            for j in range(i - 1, max(i - 5, -1), -1):
                prev = lines[j].strip()
                if prev.startswith("## 【") and "】" in prev:
                    title = prev.lstrip("#").strip().strip("【】")
                    break
            if not title:
                # 從 meta 行中提取地點作為 title
                loc_match = re.search(r"地點：([^｜）]+)", stripped)
                title = loc_match.group(1).strip() if loc_match else f"場景{len(boundaries)+1}"
            boundaries.append((i, title))

        # 錨點 2：幕/段子標題（僅在沒有緊接的場景 meta 時作為錨點）
        elif stripped.startswith("## 【") and "】" in stripped:
            # 檢查下一行是否為場景 meta（如果是，meta 才是錨點）
            next_content = ""
            for j in range(i + 1, min(i + 3, len(lines))):
                if lines[j].strip():
                    next_content = lines[j].strip()
                    break
            if next_content.startswith("（") and next_content.endswith("）"):
                continue  # 讓 meta 行當錨點
            act_title = stripped.lstrip("#").strip().strip("【】")
            boundaries.append((i, act_title))

    # 沒有任何邊界 → 整份當一個場景
    if not boundaries:
        return [{"scene_number": 1, "title": "全劇", "body": body}]

    # 切割：第一批 = 開頭到第一個邊界
    chunks = []
    first_boundary_line = boundaries[0][0]
    preamble = "\n".join(lines[:first_boundary_line]).strip()
    if preamble:
        chunks.append({"scene_number": 0, "title": "preamble", "body": preamble})

    # 逐段切割
    for idx, (start_line, title) in enumerate(boundaries):
        if idx + 1 < len(boundaries):
            end_line = boundaries[idx + 1][0]
        else:
            end_line = len(lines)
        chunk_body = "\n".join(lines[start_line:end_line]).strip()
        if chunk_body:
            chunks.append({
                "scene_number": idx + 1,
                "title": title,
                "body": chunk_body,
            })

    # 如果 preamble 存在，併入第一批
    if chunks and chunks[0]["scene_number"] == 0:
        if len(chunks) > 1:
            chunks[1]["body"] = chunks[0]["body"] + "\n\n" + chunks[1]["body"]
            chunks.pop(0)
        else:
            chunks[0]["scene_number"] = 1

    # 重新編號
    for i, chunk in enumerate(chunks):
        chunk["scene_number"] = i + 1

    return chunks


def _build_full_json(
    semantic_data: dict,
    meta: dict,
    title: str,
    date_str: str,
    narrator: str,
    ambience_default: str,
    transition_default: str,
    body: str,
) -> dict:
    """將 LLM 輸出的語義 JSON 補完為完整 director-script/v2 schema。

    LLM 只需輸出 scenes[] + segments[] 的語義欄位，
    此函式負責補完所有格式欄位（id, timing, voice, subtitle, scene 關聯）。
    """
    raw_scenes = semantic_data.get("scenes", [])
    raw_segments = semantic_data.get("segments", [])

    # 判斷 compile_mode
    has_dialogue = any(s.get("type") == "dialogue" for s in raw_segments)
    has_narration = any(s.get("type") == "narration" for s in raw_segments)
    if has_dialogue and has_narration:
        compile_mode = "mixed"
    elif has_dialogue:
        compile_mode = "dialogue"
    else:
        compile_mode = "narration"

    # 建構 scenes（完整 schema）
    scenes = []
    scene_segment_ids = {}  # scene_number -> [segment_ids]
    for raw_scene in raw_scenes:
        sn = raw_scene.get("scene_number", len(scenes) + 1)
        scene_segment_ids[sn] = []
        scenes.append({
            "act_number": sn,
            "act_title": raw_scene.get("title", f"第{sn}幕"),
            "meta": {
                "ambience": raw_scene.get("ambience", ambience_default),
                "transition": raw_scene.get("transition", transition_default),
            },
            "segment_ids": [],  # 稍後填入
        })

    # 如果 LLM 沒輸出 scenes，從 segments 的 scene_number 自動推斷
    if not scenes:
        scene_numbers_seen = sorted(set(
            s.get("scene_number", 1) for s in raw_segments
        ))
        for sn in scene_numbers_seen:
            scene_segment_ids[sn] = []
            scenes.append({
                "act_number": sn,
                "act_title": f"第{sn}幕",
                "meta": {
                    "ambience": ambience_default,
                    "transition": transition_default,
                },
                "segment_ids": [],
            })

    # 建構 segments（完整 schema）
    segments = []
    for idx, raw_seg in enumerate(raw_segments):
        seg_id = idx + 1
        scene_num = raw_seg.get("scene_number", 1)
        seg_type = raw_seg.get("type", "dialogue")

        # 找出此段所屬 scene 的 ambience
        scene_ambience = ambience_default
        for sc in scenes:
            if sc["act_number"] == scene_num:
                scene_ambience = sc["meta"].get("ambience", ambience_default)
                break

        segment = {
            "id": seg_id,
            "type": seg_type,
            "speaker": raw_seg.get("speaker", narrator),
            "text": raw_seg.get("text", ""),
            "scene_number": scene_num,
            "timing": {
                "pause_before": 0.0,
                "pause_after": 1.0 if seg_type == "narration" else 0.5,
            },
            "voice": {
                "profile": raw_seg.get("speaker", narrator),
                "ambience": scene_ambience,
                "sample_rate": 48000,
                "mood": raw_seg.get("mood"),
                "expression_tags": raw_seg.get("expression_tags", []),
            },
            "subtitle": {
                "text": raw_seg.get("text", ""),
                "lead_time_ms": 0,
                "display_mode": "timed",
            },
        }
        segments.append(segment)

        # 記錄 segment -> scene 對應
        if scene_num in scene_segment_ids:
            scene_segment_ids[scene_num].append(seg_id)

    # 回填 scene 的 segment_ids
    for sc in scenes:
        sn = sc["act_number"]
        sc["segment_ids"] = scene_segment_ids.get(sn, [])

    return {
        "schema_version": "director-script/v2",
        "compile_mode": compile_mode,
        "title": title,
        "date": date_str,
        "source": {
            "file": "導演劇本.md",
            "generated_at": datetime.now(TST).isoformat(),
            "parser": "director-parser/v2-semantic",
            "cache_hit": False,
            "retry_count": 0,
        },
        "frontmatter": meta,
        "scenes": scenes,
        "segments": segments,
        "segments_total": len(segments),
        "scenes_total": len(scenes),
    }


def _call_llm(
    system_prompt: str,
    user_prompt: str,
    model: str = "glm-5-turbo",
    temperature: float = 0.1,
    max_tokens: int = 16384,
) -> str:
    """呼叫 LLM API，返回原始文字回應。

    支援 OpenAI 相容 API（z.ai / OpenAI / 本地模型）。
    環境變數優先序：OPENAI_API_KEY > GLM_API_KEY
                   OPENAI_BASE_URL > GLM_BASE_URL

    注意：此函式為獨立執行用。在 Hermes 環境中，
    應使用 DirectorParser.parse_with_hermes_json() 代替，
    由 Hermes agent 自身做 LLM 解析後將 JSON 傳入。
    """
    import openai

    # 判定 API 設定（支援多組 env var）
    base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get("GLM_BASE_URL", "")
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("GLM_API_KEY", "")

    if not api_key:
        raise RuntimeError("缺少 OPENAI_API_KEY 或 GLM_API_KEY 環境變數")

    client_kwargs: Dict[str, Any] = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url

    client = openai.OpenAI(**client_kwargs)

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    )

    content = response.choices[0].message.content or ""

    # 空:回應自動重試（glm-5-turbo 偶發空輸出）
    if not content.strip():
        logger.warning("LLM 回應為空，立即重試...")
        for _retry in range(2):
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                content = response.choices[0].message.content or ""
                if content.strip():
                    break
            except Exception:
                continue

    # 確保 frontmatter 中的 date 可序列化
    return content.strip()


def _extract_json_from_response(text: str) -> Optional[dict]:
    """從 LLM 回應中提取 JSON（處理 markdown code block 包裹）

    支援格式：
    1. 純 JSON（直接是 dict 或 list）
    2. ```json ... ``` code block
    3. 混合文字中的 JSON 物件/陣列
    """
    # 嘗試 1：直接解析
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            # LLM 可能直接輸出 segments 陣列 → 包裝為 dict
            return {"segments": parsed}
    except json.JSONDecodeError:
        pass

    # 嘗試 2：提取 ```json ... ``` code block
    m = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if m:
        try:
            parsed = json.loads(m.group(1))
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list):
                return {"segments": parsed}
        except json.JSONDecodeError:
            pass

    # 嘗試 3：找最外層 { ... }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(text[start:end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    # 嘗試 4：找最外層 [ ... ]（LLM 直接輸出陣列）
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(text[start:end + 1])
            if isinstance(parsed, list):
                return {"segments": parsed}
        except json.JSONDecodeError:
            pass

    logger.warning(f"JSON 提取失敗，回應前 500 字：{text[:500]}")
    return None


def _validate_schema(data: dict) -> Tuple[bool, List[str]]:
    """驗證 JSON 輸出是否符合 schema 規格。

    Returns:
        (is_valid, error_list)
    """
    errors: List[str] = []

    # 必填頂層欄位
    required_top = ["schema_version", "compile_mode", "title", "date", "source",
                    "frontmatter", "scenes", "segments", "segments_total", "scenes_total"]
    for key in required_top:
        if key not in data:
            errors.append(f"缺少必填頂層欄位：{key}")

    if errors:
        return False, errors

    # schema_version
    if data.get("schema_version") != "director-script/v2":
        errors.append(f"schema_version 應為 'director-script/v2'，實際為 '{data.get('schema_version')}'")

    # compile_mode
    if data.get("compile_mode") not in VALID_COMPILE_MODES:
        errors.append(f"compile_mode 應為 {VALID_COMPILE_MODES} 之一，實際為 '{data.get('compile_mode')}'")

    # segments 陣列
    segments = data.get("segments", [])
    if not isinstance(segments, list):
        errors.append("segments 應為陣列")
    else:
        seg_ids = set()
        for i, seg in enumerate(segments):
            seg_id = seg.get("id")
            if seg_id is None:
                errors.append(f"segments[{i}] 缺少 id")
            elif seg_id in seg_ids:
                errors.append(f"segments[{i}] id={seg_id} 重複")
            else:
                seg_ids.add(seg_id)

            # segment 必填欄位
            for req_field in ["id", "type", "speaker", "text", "scene_number", "timing", "voice", "subtitle"]:
                if req_field not in seg:
                    errors.append(f"segments[{i}] 缺少必填欄位：{req_field}")

            # type 合法值
            if seg.get("type") not in {"dialogue", "narration"}:
                errors.append(f"segments[{i}].type 應為 dialogue/narration，實際為 '{seg.get('type')}'")

            # timing 必填欄位
            timing = seg.get("timing", {})
            for req_t in ["pause_before", "pause_after"]:
                if req_t not in timing:
                    errors.append(f"segments[{i}].timing 缺少 {req_t}")

            # voice 必填欄位
            voice = seg.get("voice", {})
            for req_v in ["profile", "ambience", "sample_rate"]:
                if req_v not in voice:
                    errors.append(f"segments[{i}].voice 缺少 {req_v}")

            # subtitle 必填欄位
            subtitle = seg.get("subtitle", {})
            for req_s in ["text", "lead_time_ms", "display_mode"]:
                if req_s not in subtitle:
                    errors.append(f"segments[{i}].subtitle 缺少 {req_s}")

        # segments_total 一致性
        if data.get("segments_total") != len(segments):
            errors.append(
                f"segments_total={data.get('segments_total')} 與實際 segments 長度={len(segments)} 不一致"
            )

    # scenes 陣列
    scenes = data.get("scenes", [])
    if not isinstance(scenes, list):
        errors.append("scenes 應為陣列")
    else:
        for i, scene in enumerate(scenes):
            for req_sc in ["act_title", "act_number", "meta", "segment_ids"]:
                if req_sc not in scene:
                    errors.append(f"scenes[{i}] 缺少必填欄位：{req_sc}")

            # segment_ids 指向存在的 segment
            seg_ids_valid = {s.get("id") for s in segments if isinstance(s, dict)}
            for sid in scene.get("segment_ids", []):
                if sid not in seg_ids_valid:
                    errors.append(f"scenes[{i}].segment_ids 包含不存在的 segment id：{sid}")

        if data.get("scenes_total") != len(scenes):
            errors.append(
                f"scenes_total={data.get('scenes_total')} 與實際 scenes 長度={len(scenes)} 不一致"
            )

    return len(errors) == 0, errors


# ─── 主解析器 ─────────────────────────────────────────────

@dataclass
class ParseResult:
    """解析結果"""
    success: bool
    data: Optional[dict] = None
    error: Optional[str] = None
    retry_count: int = 0
    cache_hit: bool = False
    fallback_used: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class DirectorParser:
    """導演劇本解析器 v2

    使用 LLM 全量解析導演劇本為結構化 JSON，
    含自動調教循環與快取機制。
    """

    def __init__(
        self,
        model: str = "glm-5-turbo",
        max_retries: int = MAX_RETRIES,
        enable_cache: bool = True,
    ):
        """
        Args:
            model: LLM 模型名稱（OpenAI 相容 API）
            max_retries: 自動調教最大重試次數
            enable_cache: 是否啟用快取
        """
        self.model = model
        self.max_retries = max_retries
        self.enable_cache = enable_cache

    def parse_file(self, path: str | Path) -> ParseResult:
        """從檔案解析導演劇本"""
        path = Path(path)
        if not path.exists():
            return ParseResult(success=False, error=f"檔案不存在：{path}")

        text = path.read_text(encoding="utf-8")
        return self.parse(text, script_dir=path.parent)

    def parse(self, script: str, script_dir: Optional[str | Path] = None) -> ParseResult:
        """解析導演劇本文本

        Args:
            script: 完整 Markdown 文本（含 frontmatter）
            script_dir: 劇本所在目錄（用於快取，可選）

        Returns:
            ParseResult 包含解析後的 JSON dict 或錯誤資訊
        """
        # 1. 提取 frontmatter
        meta, body = extract_frontmatter(script)

        title = meta.get("title", "未命名劇本")
        raw_date = meta.get("date", datetime.now(TST).strftime("%Y-%m-%d"))
        date_str = str(raw_date) if not isinstance(raw_date, str) else raw_date
        narrator = meta.get("narrator", DEFAULT_NARRATOR)
        ambience_default = meta.get("ambience_default", "none")
        transition_default = meta.get("transition_default", "none")

        # 2. 快取檢查
        if self.enable_cache and script_dir:
            h = _content_hash(body)
            cp = _cache_path(script_dir, h)
            cached = _read_cache(cp)
            if cached is not None:
                return ParseResult(
                    success=True,
                    data=cached,
                    cache_hit=True,
                    retry_count=0,
                )

        # 3. LLM 解析（含自動調教循環）
        llm_result = self._parse_with_llm(
            body=body,
            meta=meta,
            title=title,
            date_str=date_str,
            narrator=narrator,
            ambience_default=ambience_default,
            transition_default=transition_default,
            script_dir=script_dir,
        )

        if llm_result.success:
            # 寫入快取
            if self.enable_cache and script_dir:
                h = _content_hash(body)
                cp = _cache_path(script_dir, h)
                _write_cache(cp, llm_result.data)
            return llm_result

        return llm_result

    def _parse_with_llm(
        self,
        body: str,
        meta: dict,
        title: str,
        date_str: str,
        narrator: str,
        ambience_default: str,
        transition_default: str,
        script_dir: Optional[str | Path] = None,
    ) -> ParseResult:
        """分批語義解析 → 組合 v2 schema。

        策略：
        1. _split_by_scenes() 按場景邊界分批
        2. 每批獨立呼叫 LLM（輕量語義 JSON）
        3. 收集所有批次的 segments
        4. _build_full_json() 統一補完 v2 schema
        """
        scenes = _split_by_scenes(body)
        total_scenes = len(scenes)

        if total_scenes == 0:
            return ParseResult(success=False, error="body 為空，無法分批", retry_count=0)

        # 如果只有一個場景 → 走單次呼叫（不分批）
        if total_scenes == 1:
            return self._parse_single_scene(
                body, meta, title, date_str, narrator,
                ambience_default, transition_default,
            )

        # 分批解析
        logger.info(f"分批解析：{total_scenes} 個場景")
        all_segments = []
        all_scenes_meta = []
        failed_batches = []

        # ── 並行處理每個場景 ──
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _process_scene(scene: dict) -> tuple[int, str, str, dict | None, list | None]:
            """處理單一場景，回傳 (sn, title, body, scene_meta, segments)"""
            sn = scene["scene_number"]
            scene_title = scene["title"]
            scene_body = scene["body"]

            logger.info(f"  場景 {sn}/{total_scenes}：「{scene_title[:30]}」（{len(scene_body)} 字）")

            user_prompt = f"""請從以下場景中提取語義段落清單。

## 場景資訊
- scene_number: {sn}
- title: {scene_title}
- narrator: {narrator}

## 場景內容
```
{scene_body}
```

請輸出 JSON 陣列（segments），每個元素含 scene_number, type, speaker, text, expression_tags, mood。只輸出 JSON。"""

            try:
                response_text = _call_llm(
                    SEMANTIC_PROMPT, user_prompt,
                    model=self.model,
                    max_tokens=8192,
                )
            except Exception as e:
                logger.warning(f"  場景 {sn} LLM 呼叫失敗：{e}")
                return (sn, scene_title, scene_body, None, None)

            scene_data = _extract_json_from_response(response_text)
            if scene_data is None:
                logger.warning(f"  場景 {sn} JSON 提取失敗")
                return (sn, scene_title, scene_body, None, None)

            segs = scene_data if isinstance(scene_data, list) else scene_data.get("segments", [])
            if not isinstance(segs, list) or len(segs) == 0:
                logger.warning(f"  場景 {sn} segments 為空")
                return (sn, scene_title, scene_body, None, None)

            # 統一 scene_number
            for seg in segs:
                seg["scene_number"] = sn

            # 場景 meta
            scene_meta = {"scene_number": sn, "title": scene_title}
            amb_match = re.search(r"音場[：:]([^｜）]+)", scene_body)
            trans_match = re.search(r"轉場[：:]([^｜）]+)", scene_body)
            if amb_match:
                scene_meta["ambience"] = amb_match.group(1).strip()
            if trans_match:
                scene_meta["transition"] = trans_match.group(1).strip()

            logger.info(f"  場景 {sn} 完成：{len(segs)} 段")
            return (sn, scene_title, scene_body, scene_meta, segs)

        # ── 第一輪：並行 ──
        logger.info(f"[並行] 同時處理 {len(scenes)} 個場景")
        global parse_status
        parse_status = f"並行解析 0/{total_scenes}"
        with ThreadPoolExecutor(max_workers=len(scenes)) as pool:
            futures = {pool.submit(_process_scene, s): s for s in scenes}
            for future in as_completed(futures):
                sn, scene_title, scene_body, scene_meta, segs = future.result()
                if segs is not None and scene_meta is not None:
                    all_segments.extend(segs)
                    all_scenes_meta.append(scene_meta)
                else:
                    failed_batches.append(sn)
                parse_status = f"並行解析 {len(all_scenes_meta)}/{total_scenes}"

        logger.info(f"[並行] 完成 {total_scenes - len(failed_batches)}/{total_scenes}，失敗 {len(failed_batches)}")
        parse_status = f"並行完成 {total_scenes - len(failed_batches)}/{total_scenes}"

        # ── 第二輪：序列 fallback（並行失敗數 ≥ 3 時觸發）──
        FALLBACK_THRESHOLD = 3
        if len(failed_batches) >= FALLBACK_THRESHOLD:
            logger.warning(f"[Fallback] 並行失敗 {len(failed_batches)} ≥ {FALLBACK_THRESHOLD}，降為序列重跑")
            parse_status = f"序列重跑 {len(failed_batches)} 場失敗場景"
            failed_scenes = [s for s in scenes if s["scene_number"] in failed_batches]
            retry_failed = []
            for i, scene in enumerate(failed_scenes):
                parse_status = f"序列重跑 {i+1}/{len(failed_scenes)}"
                sn, scene_title, scene_body, scene_meta, segs = _process_scene(scene)
                if segs is not None and scene_meta is not None:
                    all_segments.extend(segs)
                    all_scenes_meta.append(scene_meta)
                    logger.info(f"  [Fallback] 場景 {sn} 重試成功：{len(segs)} 段")
                else:
                    retry_failed.append(sn)

            failed_batches = retry_failed
            logger.info(f"[Fallback] 序列重跑完成，仍失敗 {len(failed_batches)}")
            parse_status = f"序列重跑完成"

        if not all_segments:
            return ParseResult(
                success=False,
                error=f"分批解析失敗：所有 {total_scenes} 個場景均未產出有效段落",
                retry_count=0,
            )

        # 組合為完整 v2 schema
        semantic_data = {"scenes": all_scenes_meta, "segments": all_segments}
        full_data = _build_full_json(
            semantic_data=semantic_data,
            meta=meta,
            title=title,
            date_str=date_str,
            narrator=narrator,
            ambience_default=ambience_default,
            transition_default=transition_default,
            body=body,
        )

        # 最終 schema 驗證
        is_valid, errors = _validate_schema(full_data)
        if is_valid:
            note = f"（場景 {failed_batches} 失敗）" if failed_batches else ""
            logger.info(f"分批解析完成：{len(all_segments)} 段 / {total_scenes} 場景{note}")
            return ParseResult(success=True, data=full_data, retry_count=0)
        else:
            logger.warning(f"schema 補完後仍有 {len(errors)} 項錯誤：{errors[:5]}")
            # 嘗試 fallback 到單次解析
            logger.info("嘗試 fallback 單次解析...")
            return self._parse_single_scene(
                body, meta, title, date_str, narrator,
                ambience_default, transition_default,
            )

    def _parse_single_scene(
        self,
        body: str,
        meta: dict,
        title: str,
        date_str: str,
        narrator: str,
        ambience_default: str,
        transition_default: str,
    ) -> ParseResult:
        """單次 LLM 呼叫（不分批），用於場景數 ≤ 1 或 fallback。"""
        user_prompt = f"""請從以下導演劇本 body 中提取語義段落清單。

## Frontmatter 資訊（已提取）
- title: {title}
- date: {date_str}
- narrator: {narrator}
- ambience_default: {ambience_default}
- transition_default: {transition_default}

## 導演劇本 Body
```
{body}
```

請輸出 JSON（含 scenes 陣列 + segments 陣列，只需語義欄位）。只輸出 JSON。"""

        previous_output: Optional[str] = None
        _last_errors: List[str] = []

        for attempt in range(1, self.max_retries + 1):
            logger.info(f"LLM 單次解析第 {attempt} 次（model={self.model}）")

            try:
                if attempt == 1:
                    response_text = _call_llm(
                        SEMANTIC_PROMPT, user_prompt,
                        model=self.model,
                        max_tokens=8192,
                    )
                else:
                    retry_prompt = f"""上一次提取結果有誤，請修正。

## 導演劇本 Body
```
{body}
```

## 上次輸出（有誤）
```
{previous_output or ""}
```

## 錯誤
{chr(10).join(f"- {e}" for e in _last_errors)}

請重新輸出 JSON（含 scenes + segments）。只輸出 JSON。"""
                    response_text = _call_llm(
                        SEMANTIC_PROMPT, retry_prompt,
                        model=self.model,
                        max_tokens=8192,
                    )
            except Exception as e:
                logger.error(f"LLM API 呼叫失敗（attempt {attempt}）：{e}")
                if attempt < self.max_retries:
                    time.sleep(2 ** attempt)
                    continue
                return ParseResult(
                    success=False,
                    error=f"LLM API 呼叫失敗（{self.max_retries} 次）：{e}",
                    retry_count=attempt,
                )

            semantic_data = _extract_json_from_response(response_text)
            if semantic_data is None:
                logger.warning(f"attempt {attempt}：無法從回應中提取 JSON")
                previous_output = response_text
                _last_errors = ["無法解析為有效 JSON"]
                continue

            if "segments" not in semantic_data or not isinstance(semantic_data.get("segments"), list):
                # 可能 LLM 直接輸出了 segments 陣列
                if isinstance(semantic_data.get("segments"), list) and len(semantic_data["segments"]) == 0:
                    logger.warning(f"attempt {attempt}：segments 為空")
                    previous_output = response_text
                    _last_errors = ["segments 陣列為空"]
                    continue

            raw_segments = semantic_data.get("segments", [])
            if len(raw_segments) == 0:
                logger.warning(f"attempt {attempt}：segments 為空")
                previous_output = response_text
                _last_errors = ["未提取到任何段落"]
                continue

            full_data = _build_full_json(
                semantic_data=semantic_data,
                meta=meta,
                title=title,
                date_str=date_str,
                narrator=narrator,
                ambience_default=ambience_default,
                transition_default=transition_default,
                body=body,
            )
            full_data["source"]["retry_count"] = attempt - 1

            is_valid, errors = _validate_schema(full_data)
            if is_valid:
                logger.info(f"LLM 單次解析成功（attempt {attempt}），{len(raw_segments)} 段")
                return ParseResult(success=True, data=full_data, retry_count=attempt - 1)
            else:
                logger.warning(f"attempt {attempt}：schema 補完後仍有 {len(errors)} 項錯誤")
                previous_output = response_text
                _last_errors = errors[:5]
                continue

        return ParseResult(
            success=False,
            error=f"LLM 語義解析失敗（{self.max_retries} 次重試後仍無法提取有效段落）",
            retry_count=self.max_retries,
        )

    def parse_with_hermes_json(
        self,
        script: str,
        hermes_json_str: str,
        script_dir: Optional[str | Path] = None,
    ) -> ParseResult:
        """由 Hermes agent 傳入 LLM 解析後的 JSON 字串，走驗證 + 快取。

        此方法為 Hermes Skill 專用：
        1. Hermes agent 讀取導演劇本 + SYSTEM_PROMPT → 產生 JSON
        2. 呼叫此方法驗證 + 快取 + fallback

        Args:
            script: 完整 Markdown 文本（含 frontmatter）
            hermes_json_str: Hermes agent 產生的 JSON 字串
            script_dir: 劇本所在目錄（用於快取，可選）

        Returns:
            ParseResult
        """
        # 1. 提取 frontmatter
        meta, body = extract_frontmatter(script)

        title = meta.get("title", "未命名劇本")
        raw_date = meta.get("date", datetime.now(TST).strftime("%Y-%m-%d"))
        date_str = str(raw_date) if not isinstance(raw_date, str) else raw_date
        narrator = meta.get("narrator", DEFAULT_NARRATOR)
        ambience_default = meta.get("ambience_default", "none")
        transition_default = meta.get("transition_default", "none")

        # 2. 快取檢查
        if self.enable_cache and script_dir:
            h = _content_hash(body)
            cp = _cache_path(script_dir, h)
            cached = _read_cache(cp)
            if cached is not None:
                return ParseResult(
                    success=True,
                    data=cached,
                    cache_hit=True,
                    retry_count=0,
                )

        # 3. 提取 + 驗證 Hermes 傳入的 JSON
        data = _extract_json_from_response(hermes_json_str)
        if data is None:
            error_msg = "Hermes 傳入的 JSON 無法解析"
            logger.error(error_msg)
            return ParseResult(success=False, error=error_msg, retry_count=0)

        # 4. 填充來源資訊
        data["source"] = {
            "file": "導演劇本.md",
            "generated_at": datetime.now(TST).isoformat(),
            "parser": "director-parser/v2-hermes",
            "cache_hit": False,
            "retry_count": 0,
        }

        # 5. Schema 驗證
        is_valid, errors = _validate_schema(data)
        if not is_valid:
            error_msg = f"Schema 驗證失敗（{len(errors)} 項）：{'；'.join(errors[:5])}"
            logger.warning(error_msg)

            return ParseResult(success=False, error=error_msg, retry_count=0)

        # 6. 寫入快取
        if self.enable_cache and script_dir:
            h = _content_hash(body)
            cp = _cache_path(script_dir, h)
            _write_cache(cp, data)

        logger.info(f"Hermes LLM 解析成功，共 {len(data.get('segments', []))} 段")
        return ParseResult(success=True, data=data, retry_count=0)


# ─── 工具函式 ─────────────────────────────────────────────

def parse_director_script(
    path: str | Path,
    model: str = "glm-5-turbo",
    max_retries: int = MAX_RETRIES,
    enable_cache: bool = True,
) -> ParseResult:
    """便捷函式：解析導演劇本檔案。

    Args:
        path: 導演劇本 .md 檔案路徑
        model: LLM 模型名稱
        max_retries: 最大重試次數
        enable_cache: 是否啟用快取

    Returns:
        ParseResult
    """
    parser = DirectorParser(
        model=model,
        max_retries=max_retries,
        enable_cache=enable_cache,
    )
    return parser.parse_file(path)


def save_voice_json(data: dict, output_path: str | Path) -> None:
    """將解析結果存為語音版 JSON"""
    Path(output_path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


# ─── CLI ──────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="導演劇本解析器 v2（Director Parser）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
範例：
  python director_parser.py 導演劇本.md -o 語音版.json
  python director_parser.py 導演劇本.md --validate-only
  python director_parser.py 導演劇本.md --no-cache
  python director_parser.py 導演劇本.md --model glm-5-turbo
        """,
    )
    ap.add_argument("input", help="導演劇本 .md 檔案路徑")
    ap.add_argument("-o", "--output", help="輸出 JSON 路徑（預設 stdout）")
    ap.add_argument("--model", default="glm-5-turbo", help="LLM 模型（預設 glm-5-turbo）")
    ap.add_argument("--max-retries", type=int, default=MAX_RETRIES, help="最大重試次數")
    ap.add_argument("--no-cache", action="store_true", help="停用快取")
    ap.add_argument("--validate-only", action="store_true", help="僅驗證 schema，不做 LLM 解析")

    args = ap.parse_args()

    if args.validate_only:
        # 僅驗證模式：讀取已有 JSON 並驗證
        try:
            data = json.loads(Path(args.input).read_text(encoding="utf-8"))
        except Exception as e:
            print(f"❌ JSON 解析失敗：{e}", file=sys.stderr)
            sys.exit(1)
        is_valid, errors = _validate_schema(data)
        if is_valid:
            print(f"✅ Schema 驗證通過（{data.get('segments_total', 0)} 段 / {data.get('scenes_total', 0)} 場）")
        else:
            print(f"❌ Schema 驗證失敗（{len(errors)} 項錯誤）：")
            for err in errors:
                print(f"  - {err}")
            sys.exit(1)
        return

    result = parse_director_script(
        args.input,
        model=args.model,
        max_retries=args.max_retries,
        enable_cache=not args.no_cache,
    )

    if result.success and result.data:
        if args.output:
            save_voice_json(result.data, args.output)
            status_parts = []
            if result.cache_hit:
                status_parts.append("快取命中")
            if result.fallback_used:
                status_parts.append("v1.2 fallback")
            if result.retry_count > 0:
                status_parts.append(f"重試{result.retry_count}次")
            status = f"（{'，'.join(status_parts)}）" if status_parts else ""
            print(f"✅ 解析完成{status}：{result.data.get('segments_total', 0)} 段 → {args.output}")
        else:
            print(json.dumps(result.data, ensure_ascii=False, indent=2))
    else:
        print(f"❌ 解析失敗：{result.error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
