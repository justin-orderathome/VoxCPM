#!/usr/bin/env python3
"""導演劇本解析器 v2 (Director Parser)

將導演劇本（director script）解析為結構化 JSON，供 merger.py 逐段生成語音。

與 v1 DialogueParser 的差異：
  - v1 用正則解析閱讀版.md（人類先寫好結構）
  - v2 用 LLM 全量解析導演劇本.md（自由格式 → 結構化）

解析流程：
  1. yaml.safe_load → frontmatter（標準庫，不走 LLM）
  2. body → SHA-256 hash → 檢查快取
  3. 快取命中 → 直接返回
  4. 快取未命中 → LLM 全量解析（prompt + JSON schema）
  5. JSON schema 驗證 → 失敗則自動調教循環（最多 3 次）
  6. 寫入快取 → 返回 DirectorScript

Fallback：
  - LLM 解析全部失敗 → fallback 到 v1.2 DialogueParser（正則解析）
  - v1.2 也失敗 → 報錯 + log 人工介入

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

# 同目錄 import（v1.2 fallback）
_wangdom_dir = str(Path(__file__).parent)
if _wangdom_dir not in sys.path:
    sys.path.insert(0, _wangdom_dir)

# ─── 日誌 ──────────────────────────────────────────────────

logger = logging.getLogger("director_parser")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


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

SYSTEM_PROMPT = """你是一位專業的廣播劇導演劇本解析器。你的任務是將 Markdown 格式的導演劇本解析為結構化 JSON。

## 輸入格式

導演劇本是 Markdown 文件，包含：
- YAML frontmatter（已提取，你只需處理 body）
- 幕標題：【第X幕：名稱】或 ## 【第X幕：名稱】
- 場景 meta：（時間：...｜地點：...｜人物：...｜音場：...｜轉場：...）
- 角色對話：{emoji} {角色名} [{演技標籤}]（{表演註記}）：\n{台詞}
- 散文段落：無角色標頭的文本（由 narrator 朗讀）
- 註解區塊：> 開頭或 <!-- --> 包裹

## 三種括號系統

1. `（）` 全形括號 — 場景級 meta（獨立成行）或表演註記（角色標頭行中）
2. `【】` 全形方括號 — 結構標記（幕/場標題）
3. `[]` 半形方括號 — 行內演技標籤

## 解析規則

1. **段落類型判斷**：
   - 有角色標頭（emoji + 角色名 + 冒號）→ type="dialogue"
   - 無角色標頭的文本 → type="narration"，speaker 使用 frontmatter 的 narrator

2. **Speaker 清理**：
   - 移除開頭的 emoji
   - 保留 `部門·角色名` 格式（如 `軍師·諸葛亮`）
   - 移除 `[]` 演技標籤和 `（）` 表演註記

3. **演技標籤抽取**：
   - `[]` 內的短標籤直接作為 expression_tags
   - `（）` 內的自然語言描述由你判斷 mood 值

4. **場景 meta 抽取**：
   - 從 `（）` 獨立行中提取 time/location/characters/ambience/transition
   - ambience 合法值：none, studio, hall, battle, rain, cave, wind, forest, crowd
   - transition 合法值：none, chime, gong, drum, rain, laugh

5. **compile_mode 判斷**：
   - 全篇只有對話 → "dialogue"
   - 全篇只有散文 → "narration"
   - 兩者混合 → "mixed"

6. **ID 與序號**：
   - segment.id 全劇遞增，從 1 開始
   - scene.act_number 從 1 開始遞增
   - segment.scene_number 對應所屬場景的 act_number

7. **轉場音效**：
   - 僅每幕第一段設定 transition（從場景 meta 取得）
   - 其他段 transition 為 null

8. **timing 預設**：
   - pause_before: 0.0
   - pause_after: 0.5（散文段落 1.0）

9. **voice.ambience**：
   - 從場景 meta 取得，無 meta 時使用 frontmatter 的 ambience_default

10. **字幕**：
    - subtitle.text = segment.text 的完整文字
    - lead_time_ms = 0（默認）
    - display_mode = "timed"

## 注意事項

- 不要遺漏任何段落（包括散文段落）
- 散文段落即使跨多行，也合併為一個 segment
- 角色台詞多行也合併為一個 segment（至空行結束）
- 註解、引用塊（>）、HTML 註解（<!-- -->）不納入 segments
- 保持原文，不要改寫或省略任何文字內容
- characters 欄位為角色名列表（不含 emoji），逗號分隔轉陣列"""

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
    return content.strip()


def _extract_json_from_response(text: str) -> Optional[dict]:
    """從 LLM 回應中提取 JSON（處理 markdown code block 包裹）"""
    # 嘗試 1：直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 嘗試 2：提取 ```json ... ``` code block
    m = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # 嘗試 3：找最外層 { ... }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

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
        enable_fallback: bool = True,
    ):
        """
        Args:
            model: LLM 模型名稱（OpenAI 相容 API）
            max_retries: 自動調教最大重試次數
            enable_cache: 是否啟用快取
            enable_fallback: 是否在 LLM 全部失敗後 fallback 到 v1.2
        """
        self.model = model
        self.max_retries = max_retries
        self.enable_cache = enable_cache
        self.enable_fallback = enable_fallback

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

        # 4. Fallback 到 v1.2 DialogueParser
        if self.enable_fallback:
            logger.warning(f"LLM 解析失敗（重試 {llm_result.retry_count} 次），fallback 到 v1.2 DialogueParser")
            fallback_result = self._fallback_v1(
                script=script,
                meta=meta,
                title=title,
                date_str=date_str,
                narrator=narrator,
                ambience_default=ambience_default,
                transition_default=transition_default,
                script_dir=script_dir,
            )
            return fallback_result

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
        """LLM 解析 + 自動調教循環"""
        user_prompt = f"""請解析以下導演劇本 body 為結構化 JSON。

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

請輸出完整的 JSON（符合 director-script/v2 schema）。只輸出 JSON，不要其他文字。"""

        previous_output: Optional[str] = None
        _last_errors: List[str] = []

        for attempt in range(1, self.max_retries + 1):
            logger.info(f"LLM 解析第 {attempt} 次（model={self.model}）")

            try:
                if attempt == 1:
                    response_text = _call_llm(SYSTEM_PROMPT, user_prompt, model=self.model)
                else:
                    # 構建重試 prompt（上次的錯誤回饋）
                    retry_prompt = RETRY_PROMPT_TEMPLATE.format(
                        body=body,
                        previous_output=previous_output or "",
                        errors="\n".join(f"- {e}" for e in _last_errors),
                    )
                    response_text = _call_llm(SYSTEM_PROMPT, retry_prompt, model=self.model)

            except Exception as e:
                logger.error(f"LLM API 呼叫失敗（attempt {attempt}）：{e}")
                if attempt < self.max_retries:
                    time.sleep(2 ** attempt)  # 指數退避
                    continue
                return ParseResult(
                    success=False,
                    error=f"LLM API 呼叫失敗（{self.max_retries} 次）：{e}",
                    retry_count=attempt,
                )

            # 提取 JSON
            data = _extract_json_from_response(response_text)
            if data is None:
                logger.warning(f"attempt {attempt}：無法從回應中提取 JSON")
                previous_output = response_text
                _last_errors = ["無法解析為有效 JSON，回應可能包含非 JSON 文字"]
                continue

            # 填充來源資訊
            data["source"] = {
                "file": "導演劇本.md",
                "generated_at": datetime.now(TST).isoformat(),
                "parser": "director-parser/v2",
                "cache_hit": False,
                "retry_count": attempt,
            }

            # Schema 驗證
            is_valid, errors = _validate_schema(data)
            if is_valid:
                logger.info(f"LLM 解析成功（attempt {attempt}），共 {len(data.get('segments', []))} 段")
                return ParseResult(
                    success=True,
                    data=data,
                    retry_count=attempt - 1,
                )
            else:
                logger.warning(f"attempt {attempt}：schema 驗證失敗（{len(errors)} 項錯誤）")
                previous_output = json.dumps(data, ensure_ascii=False, indent=2)
                _last_errors = errors
                continue

        return ParseResult(
            success=False,
            error=f"LLM 解析失敗（{self.max_retries} 次重試後仍有 schema 錯誤）",
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

            # Fallback 到 v1.2
            if self.enable_fallback:
                return self._fallback_v1(
                    script=script, meta=meta, title=title, date_str=date_str,
                    narrator=narrator, ambience_default=ambience_default,
                    transition_default=transition_default, script_dir=script_dir,
                )
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

            if self.enable_fallback:
                return self._fallback_v1(
                    script=script, meta=meta, title=title, date_str=date_str,
                    narrator=narrator, ambience_default=ambience_default,
                    transition_default=transition_default, script_dir=script_dir,
                )
            return ParseResult(success=False, error=error_msg, retry_count=0)

        # 6. 寫入快取
        if self.enable_cache and script_dir:
            h = _content_hash(body)
            cp = _cache_path(script_dir, h)
            _write_cache(cp, data)

        logger.info(f"Hermes LLM 解析成功，共 {len(data.get('segments', []))} 段")
        return ParseResult(success=True, data=data, retry_count=0)

    def _fallback_v1(
        self,
        script: str,
        meta: dict,
        title: str,
        date_str: str,
        narrator: str,
        ambience_default: str,
        transition_default: str,
        script_dir: Optional[str | Path] = None,
    ) -> ParseResult:
        """Fallback 到 v1.2 DialogueParser（正則解析）"""
        try:
            from dialogue_parser import DialogueParser, extract_frontmatter

            parser = DialogueParser(
                compile_mode=meta.get("compile_mode", "auto"),
                narrator_speaker=narrator,
            )
            v1_segments = parser.parse(script)

            if not v1_segments:
                return ParseResult(
                    success=False,
                    error="v1.2 DialogueParser 也無法解析（0 段）",
                    retry_count=self.max_retries,
                    fallback_used=True,
                )

            # 轉換為 v2 格式
            segments_v2 = []
            scene_segments: Dict[int, List[int]] = {}
            act_number = 0
            current_act_title: Optional[str] = None

            for i, seg in enumerate(v1_segments, 1):
                seg_dict = seg.to_dict()

                # 追蹤幕切換
                act_title = seg_dict.get("act_title")
                if act_title and act_title != current_act_title:
                    act_number += 1
                    current_act_title = act_title
                    scene_segments[act_number] = []

                if act_number > 0:
                    scene_segments.setdefault(act_number, []).append(i)

                segments_v2.append({
                    "id": i,
                    "type": "dialogue" if seg.speaker != narrator else "narration",
                    "speaker": seg.speaker,
                    "text": seg_dict.get("text", ""),
                    "mood": seg_dict.get("mood"),
                    "expression_tags": seg_dict.get("expression_tags", []),
                    "scene_number": max(act_number, 1),
                    "timing": {
                        "pause_before": 0.0,
                        "pause_after": seg_dict.get("pause_after", 0.5),
                    },
                    "voice": {
                        "profile": seg.speaker,
                        "ambience": seg_dict.get("ambience", ambience_default) or ambience_default,
                        "sample_rate": 48000,
                    },
                    "subtitle": {
                        "text": seg_dict.get("text", ""),
                        "lead_time_ms": 0,
                        "display_mode": "timed",
                    },
                    "transition": seg_dict.get("transition") if i == 1 else None,
                    "act_title": current_act_title,
                })

            # 組裝 scenes
            scenes = []
            for act_num, seg_ids in scene_segments.items():
                scenes.append({
                    "act_title": current_act_title or f"第{act_num}幕",
                    "act_number": act_num,
                    "meta": None,  # v1 無法解析場景 meta
                    "segment_ids": seg_ids,
                })

            # 若無幕分割，整篇視為一幕
            if not scenes:
                scenes.append({
                    "act_title": title,
                    "act_number": 1,
                    "meta": None,
                    "segment_ids": list(range(1, len(segments_v2) + 1)),
                })

            v2_data = {
                "schema_version": "director-script/v2",
                "compile_mode": meta.get("compile_mode", "auto") if meta.get("compile_mode") in VALID_COMPILE_MODES else "dialogue",
                "title": title,
                "date": date_str,
                "source": {
                    "file": "導演劇本.md",
                    "generated_at": datetime.now(TST).isoformat(),
                    "parser": "director-parser/v2-fallback-v1",
                    "cache_hit": False,
                    "retry_count": self.max_retries,
                },
                "frontmatter": {
                    "narrator": narrator,
                    "ambience_default": ambience_default,
                    "transition_default": transition_default,
                },
                "scenes": scenes,
                "segments": segments_v2,
                "segments_total": len(segments_v2),
                "scenes_total": len(scenes),
            }

            logger.info(f"v1.2 fallback 成功，共 {len(segments_v2)} 段")
            return ParseResult(
                success=True,
                data=v2_data,
                retry_count=self.max_retries,
                fallback_used=True,
            )

        except ImportError:
            return ParseResult(
                success=False,
                error="v1.2 DialogueParser 模組不可用（import 失敗）",
                retry_count=self.max_retries,
                fallback_used=True,
            )
        except Exception as e:
            return ParseResult(
                success=False,
                error=f"v1.2 fallback 失敗：{e}",
                retry_count=self.max_retries,
                fallback_used=True,
            )


# ─── 工具函式 ─────────────────────────────────────────────

def parse_director_script(
    path: str | Path,
    model: str = "glm-5-turbo",
    max_retries: int = MAX_RETRIES,
    enable_cache: bool = True,
    enable_fallback: bool = True,
) -> ParseResult:
    """便捷函式：解析導演劇本檔案。

    Args:
        path: 導演劇本 .md 檔案路徑
        model: LLM 模型名稱
        max_retries: 最大重試次數
        enable_cache: 是否啟用快取
        enable_fallback: 是否啟用 v1.2 fallback

    Returns:
        ParseResult
    """
    parser = DirectorParser(
        model=model,
        max_retries=max_retries,
        enable_cache=enable_cache,
        enable_fallback=enable_fallback,
    )
    return parser.parse_file(path)


def save_voice_json(data: dict, output_path: str | Path) -> None:
    """將解析結果存為語音版 JSON"""
    Path(output_path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
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
  python director_parser.py 導演劇本.md --no-cache --no-fallback
  python director_parser.py 導演劇本.md --model glm-5-turbo
        """,
    )
    ap.add_argument("input", help="導演劇本 .md 檔案路徑")
    ap.add_argument("-o", "--output", help="輸出 JSON 路徑（預設 stdout）")
    ap.add_argument("--model", default="glm-5-turbo", help="LLM 模型（預設 glm-5-turbo）")
    ap.add_argument("--max-retries", type=int, default=MAX_RETRIES, help="最大重試次數")
    ap.add_argument("--no-cache", action="store_true", help="停用快取")
    ap.add_argument("--no-fallback", action="store_true", help="停用 v1.2 fallback")
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
        enable_fallback=not args.no_fallback,
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
