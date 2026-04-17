#!/usr/bin/env python3
"""王朝多人劇本剖析器 v1.2

將文字劇本剖析為結構化對話段，供 merger.py 逐段生成後合併。

支援格式：
  1. 標準格式 — 「角色名：台詞」
  2. Markdown 格式 — **角色名** 台詞
  3. 純文字 + style 指令 — 支援 mood / expression_tags 欄位
  4. 散文朗讀模式 — 無角色標頭時自動按幕分段朗讀（v1.2 新增）

輸出格式（DialogueSegment list）：
  {
    "speaker": "軍師·諸葛亮",
    "text": "主公萬安",
    "mood": "沉穩",           # 可選
    "expression_tags": [...],  # 可選
    "pause_after": 0.5,       # 可選，秒
    "ambience": "hall",       # 可選，音場（v1.2）
    "transition": "chime",    # 可選，轉場音效（v1.2）
    "act_title": "第一幕",    # 可選，幕名（僅朗讀模式）
  }

v1.2 變更：
  - 新增 compile_mode: auto / dialogue / narration
  - 新增 frontmatter 解析（drama_mode, narrator, ambience_default, transition_default）
  - 新增音場標記解析：（音場：xxx）
  - 新增轉場音效解析：（轉場：xxx）
  - 散文朗讀模式：按幕分段，朗讀人預設為禮部·紀曉嵐
"""

from __future__ import annotations

import json
import re
import yaml
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional


# ─── 常量 ────────────────────────────────────────────────

VALID_AMBIENCES = {"none", "studio", "hall", "battle", "rain", "cave"}
VALID_TRANSITIONS = {"none", "chime", "gong", "drum", "rain", "laugh"}

DEFAULT_NARRATOR = "禮部·紀曉嵐"


# ─── Dataclass ───────────────────────────────────────────

@dataclass
class DialogueSegment:
    """單一對話段"""
    speaker: str
    text: str
    mood: Optional[str] = None
    expression_tags: Optional[List[str]] = None
    pause_after: float = 0.5
    ambience: Optional[str] = None       # v1.2: 音場
    transition: Optional[str] = None     # v1.2: 轉場音效
    act_title: Optional[str] = None      # v1.2: 幕名（僅朗讀模式）

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # 過濾 None 與預設值
        filtered = {}
        for k, v in d.items():
            if v is None:
                continue
            if k == "pause_after" and v == 0.5:
                continue
            if k == "ambience" and v == "none":
                continue
            if k == "transition" and v == "none":
                continue
            filtered[k] = v
        return filtered


# ─── Frontmatter 解析 ───────────────────────────────────

def extract_frontmatter(script: str) -> tuple[dict, str]:
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
        meta = yaml.safe_load(fm_text) or {}
    except Exception:
        meta = {}

    return meta, body


# ─── 主解析器 ───────────────────────────────────────────

class DialogueParser:
    """劇本剖析器，支援多種輸入格式 + 散文朗讀模式"""

    # 格式 1：「角色名：台詞」或「角色名：」（兩行格式，台詞在下一行）
    _COLON_RE = re.compile(r"^【?(.+?)】?\s*[:：]\s*(.*)$")
    # 格式 2：**角色名** 台詞
    _MARKDOWN_RE = re.compile(r"^\*\*(.+?)\*\*\s*(.*)$")
    # 格式 3：style 指令行
    _MOOD_RE = re.compile(r"^\[mood[:：]\s*(.+)\]$", re.IGNORECASE)
    _TAGS_RE = re.compile(r"^\[tags[:：]\s*(.+)\]$", re.IGNORECASE)
    _PAUSE_RE = re.compile(r"^\[pause[:：]\s*([\d.]+)\]$", re.IGNORECASE)
    # 非語言標籤（VoxCPM 原生支援）
    _NV_TAG_RE = re.compile(
        r"\[(laughing|sigh|Question-\w+|Surprise-\w+|Dissatisfaction-\w+|Uhm|Shh)\]",
        re.IGNORECASE,
    )
    # 空行 / 註解
    _BLANK_RE = re.compile(r"^\s*$")
    _COMMENT_RE = re.compile(r"^\s*#\s*")

    # v1.2: 朗讀模式標記
    _AMBIENCE_RE = re.compile(r"^[（(]\s*音場\s*[:：]\s*(\S+)\s*[）)]$")
    _TRANSITION_RE = re.compile(r"^[（(]\s*轉場\s*[:：]\s*(\S+)\s*[）)]$")
    _ACT_RE = re.compile(r"^##\s+【第.+幕[:：].+】|^\s*【第.+幕[:：].+】")
    _ACT_EXTRACT_RE = re.compile(r"【第(.+?)幕[:：]\s*(.+)】")

    def __init__(
        self,
        default_pause: float = 0.5,
        compile_mode: str = "auto",
        narrator_speaker: str = DEFAULT_NARRATOR,
    ):
        """初始化解析器。

        Args:
            default_pause: 預設段間間隙（秒）
            compile_mode: "auto" 自動偵測 / "dialogue" 強制對話 / "narration" 強制朗讀
            narrator_speaker: 朗讀模式預設朗讀人
        """
        self.default_pause = default_pause
        self.compile_mode = compile_mode
        self.narrator_speaker = narrator_speaker

    def parse(self, script: str) -> List[DialogueSegment]:
        """剖析劇本文字，回傳對話段列表。

        流程：
          1. 提取 frontmatter
          2. 判定 compile_mode（auto → 偵測 / dialogue / narration）
          3. 分派到 _parse_dialogue() 或 _parse_narration()
        """
        meta, body = extract_frontmatter(script)

        # 決定模式
        mode = self.compile_mode
        if mode == "auto":
            mode = self._detect_mode(body)

        # 覆蓋設定
        narrator = meta.get("narrator", self.narrator_speaker)
        ambience_default = meta.get("ambience_default", "none")
        if ambience_default not in VALID_AMBIENCES:
            ambience_default = "none"
        transition_default = meta.get("transition_default", "none")
        if transition_default not in VALID_TRANSITIONS:
            transition_default = "none"

        if mode == "narration":
            return self._parse_narration(body, narrator, ambience_default, transition_default)
        else:
            return self._parse_dialogue(body)

    def parse_file(self, path: str | Path) -> List[DialogueSegment]:
        """從檔案讀取劇本並剖析"""
        text = Path(path).read_text(encoding="utf-8")
        return self.parse(text)

    # ─── 模式偵測 ────────────────────────────────────

    def _detect_mode(self, body: str) -> str:
        """偵測文本應使用對話模式或朗讀模式。

        規則：掃描全文，若有角色發言標頭（emoji 開頭或王朝角色名 + 冒號）≥1 → dialogue；
        否則 → narration。

        排除：
          - frontmatter 區塊
          - 引述塊（> 開頭）
          - markdown 標題、清單、裝飾行
          - 散文中自然出現的全形冒號（「XXX：YYY」非角色標頭格式）
        """
        hit = False
        in_frontmatter = False
        fm_seen = 0
        for line in body.splitlines():
            stripped = line.strip()
            # 跳過 frontmatter
            if stripped == "---":
                fm_seen += 1
                if fm_seen <= 2:
                    in_frontmatter = fm_seen == 1
                    continue
            if in_frontmatter:
                continue
            # 排除引用塊、註解、清單、分隔線
            if stripped.startswith(">") or stripped.startswith("#"):
                continue
            if stripped.startswith("- ") or stripped.startswith("* "):
                continue
            if not stripped or stripped == "---":
                continue

            # 判定角色標頭：_COLON_RE 命中且前綴為 emoji 或是已知角色
            m = self._COLON_RE.match(stripped)
            if m:
                speaker_candidate = m.group(1).strip()
                # emoji 開頭 → 角色標頭
                if speaker_candidate and self._is_emoji(speaker_candidate[0]):
                    hit = True
                    break
                # 已知王朝角色名（含 · 分隔）→ 角色標頭
                if self._is_known_role(speaker_candidate):
                    hit = True
                    break
            # Markdown 格式：**角色名** 台詞
            if self._MARKDOWN_RE.match(stripped):
                hit = True
                break
        return "dialogue" if hit else "narration"

    @staticmethod
    def _is_emoji(ch: str) -> bool:
        """判斷字元是否為 emoji（Unicode 範圍，完整覆蓋）。"""
        cp = ord(ch)
        return (
            # ── 常用 Emoji 區段 ──
            (0x2600 <= cp <= 0x26FF)        # Misc Symbols (⚔🗡 등)
            or (0x2700 <= cp <= 0x27BF)     # Dingbats
            or (0x1F300 <= cp <= 0x1F5FF)   # Misc Symbols & Pictographs (🏔🗡 등)
            or (0x1F600 <= cp <= 0x1F64F)   # Emoticons
            or (0x1F680 <= cp <= 0x1F6FF)   # Transport & Map
            or (0x1F900 <= cp <= 0x1F9FF)   # Supplemental Symbols
            or (0x1FA00 <= cp <= 0x1FAFF)   # Chess / Extended-A
            or (0x1FAE0 <= cp <= 0x1FAEF)   # Extended-B (🫠🫡 等)
            or (0x1F1E0 <= cp <= 0x1F1FF)   # Flags
            # ── 較少見但仍在用的區段 ──
            or (0x2300 <= cp <= 0x23FF)     # Misc Technical (⚡♿ 等)
            or (0x2B50 <= cp <= 0x2B55)     # Stars (⭐ 等)
            or (0x1F7E0 <= cp <= 0x1F7FF)   # Geometric Shapes Extended
            or (0x1F0CF == cp)              # Playing card
            # ── 修飾符 ──
            or (0xFE00 <= cp <= 0xFE0F)     # Variation Selectors
            or (0x200D == cp)               # Zero Width Joiner
            or (0x20E3 == cp)               # Combining Enclosing Keycap
        )

    @staticmethod
    def _is_known_role(name: str) -> bool:
        """粗略判斷是否為王朝角色名（含 · 分隔或已知簡稱）"""
        known_short = [
            "劉邦", "諸葛亮", "曾國藩", "魏徵", "紀曉嵐", "范蠡", "管仲",
            "蘇秦", "戚繼光", "狄仁傑", "李冰", "唐伯虎", "包拯", "韓信",
            "大禹", "劉備", "李淳風", "主公", "旁白",
        ]
        clean = name.lstrip("【】").strip()
        if "·" in clean:
            parts = clean.split("·")
            # 取最後一段做比對
            return any(parts[-1] == s or parts[-1].endswith(s) for s in known_short)
        return any(clean == s or clean.endswith(s) for s in known_short)

    # ─── 對話模式（既存邏輯，無行為變更） ─────────────

    def _parse_dialogue(self, body: str) -> List[DialogueSegment]:
        """對話模式：解析角色標頭 + 台詞。"""
        segments: List[DialogueSegment] = []
        current_mood: Optional[str] = None
        current_tags: Optional[List[str]] = None
        current_pause: float = self.default_pause

        for line in body.splitlines():
            if self._BLANK_RE.match(line):
                current_mood = None
                current_tags = None
                current_pause = self.default_pause
                continue

            if self._COMMENT_RE.match(line):
                continue

            # style 指令行
            m = self._MOOD_RE.match(line.strip())
            if m:
                current_mood = m.group(1).strip()
                continue

            m = self._TAGS_RE.match(line.strip())
            if m:
                current_tags = [t.strip() for t in m.group(1).split(",")]
                continue

            m = self._PAUSE_RE.match(line.strip())
            if m:
                current_pause = float(m.group(1))
                continue

            # 格式 1：角色名：台詞
            m = self._COLON_RE.match(line.strip())
            if m:
                speaker = m.group(1).strip()
                text_part = m.group(2).strip()
                # 過濾舞台提示行（如 （音效：...）→ 不產生段）
                if text_part and not re.match(r"^[（(].*[）)]$", text_part):
                    seg = DialogueSegment(
                        speaker=speaker,
                        text=text_part,
                        mood=current_mood,
                        expression_tags=current_tags if current_tags else self._extract_nv_tags(text_part),
                        pause_after=current_pause,
                    )
                    segments.append(seg)
                    current_mood = None
                    current_tags = None
                    current_pause = self.default_pause
                elif text_part:
                    # 舞台提示行，不產生段但記錄到 pending
                    # （後續對話行可取用 mood）
                    current_mood = None
                    current_tags = None
                    current_pause = self.default_pause
                else:
                    # 兩行格式：角色標頭獨立一行，等下一行台詞
                    # 這裡不立即建立段，保留 current state 等待下一行
                    # 但要重置 style
                    current_mood = None
                    current_tags = None
                    current_pause = self.default_pause
                continue

            # 格式 2：**角色名** 台詞
            m = self._MARKDOWN_RE.match(line.strip())
            if m:
                seg = DialogueSegment(
                    speaker=m.group(1).strip(),
                    text=m.group(2).strip(),
                    mood=current_mood,
                    expression_tags=current_tags if current_tags else self._extract_nv_tags(m.group(2)),
                    pause_after=current_pause,
                )
                segments.append(seg)
                current_mood = None
                current_tags = None
                current_pause = self.default_pause
                continue

            # 純文字追加上一段
            if line.strip() and segments:
                segments[-1].text += "\n" + line.strip()
            elif line.strip():
                segments.append(DialogueSegment(
                    speaker="旁白",
                    text=line.strip(),
                    mood=current_mood,
                    expression_tags=current_tags,
                    pause_after=current_pause,
                ))

        return segments

    # ─── 朗讀模式（v1.2 新增） ───────────────────────

    def _parse_narration(
        self,
        body: str,
        narrator: str,
        ambience_default: str,
        transition_default: str,
    ) -> List[DialogueSegment]:
        """朗讀模式：按幕分段，每幕一個朗讀段。

        分段規則：
          - 以 【第X幕：...】 或 ## 標題 為分段點
          - 每段內的音場/轉場標記寫入 segment metadata
          - 若無幕標題，整篇視為一幕
        """
        segments: List[DialogueSegment] = []
        current_act: Optional[str] = None
        current_lines: List[str] = []
        current_ambience: Optional[str] = ambience_default
        current_transition: Optional[str] = transition_default
        first_act = True

        for line in body.splitlines():
            stripped = line.strip()

            # 跳過空行與 markdown 裝飾
            if not stripped or stripped == "---":
                continue
            if stripped.startswith(">"):
                continue
            if stripped.startswith("```"):
                continue

            # 偵測幕標題
            act_m = self._ACT_EXTRACT_RE.search(stripped)
            if act_m:
                # 先 flush 前一段
                if current_lines:
                    self._flush_narration_segment(
                        segments, current_lines, narrator, current_act,
                        current_ambience, current_transition,
                        transition_default if first_act else "none",
                    )
                    first_act = False
                    current_lines = []

                current_act = f"第{act_m.group(1)}幕：{act_m.group(2)}"
                current_ambience = ambience_default
                current_transition = transition_default
                continue

            # 偵測 markdown ## 標題作為分段點
            if re.match(r"^#{1,3}\s+", stripped):
                if current_lines:
                    self._flush_narration_segment(
                        segments, current_lines, narrator, current_act,
                        current_ambience, current_transition,
                        transition_default if first_act else "none",
                    )
                    first_act = False
                    current_lines = []

                current_act = re.sub(r"^#{1,3}\s+", "", stripped)
                current_ambience = ambience_default
                current_transition = transition_default
                continue

            # 偵測音場標記
            m = self._AMBIENCE_RE.match(stripped)
            if m:
                val = m.group(1).strip()
                current_ambience = val if val in VALID_AMBIENCES else ambience_default
                continue

            # 偵測轉場標記
            m = self._TRANSITION_RE.match(stripped)
            if m:
                val = m.group(1).strip()
                current_transition = val if val in VALID_TRANSITIONS else transition_default
                continue

            # 去除 markdown 強調符號
            clean = re.sub(r"[*_~`]+", "", stripped).strip()
            if clean:
                current_lines.append(clean)

        # flush 最後一段
        if current_lines:
            self._flush_narration_segment(
                segments, current_lines, narrator, current_act,
                current_ambience, current_transition,
                transition_default if first_act else "none",
            )

        return segments

    def _flush_narration_segment(
        self,
        segments: List[DialogueSegment],
        lines: List[str],
        narrator: str,
        act_title: Optional[str],
        ambience: Optional[str],
        transition: Optional[str],
        default_transition: str,
    ) -> None:
        """將收集到的行 flush 為一個 DialogueSegment。"""
        text = " ".join(lines).strip()
        text = re.sub(r"\s+", " ", text)
        if not text:
            return

        seg = DialogueSegment(
            speaker=narrator,
            text=text,
            pause_after=1.0,  # 朗讀模式間隙稍長
            ambience=ambience or "none",
            transition=transition or default_transition,
            act_title=act_title,
        )
        segments.append(seg)

    @staticmethod
    def _extract_nv_tags(text: str) -> Optional[List[str]]:
        """從台詞中抽取 VoxCPM 非語言標籤"""
        tags = DialogueParser._NV_TAG_RE.findall(text)
        return tags if tags else None


# ─── 工具函式 ───────────────────────────────────────────

def segments_to_json(segments: List[DialogueSegment], output_path: str | Path) -> None:
    """將剖析結果存為 JSON"""
    data = [s.to_dict() for s in segments]
    Path(output_path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_with_metadata(
    path: str | Path,
    compile_mode: str = "auto",
    narrator_speaker: str = DEFAULT_NARRATOR,
) -> tuple[List[DialogueSegment], dict]:
    """剖析劇本檔案，回傳 (segments, metadata)。

    metadata 包含：
      - compile_mode: 最終使用的模式
      - narrator: 朗讀人
      - ambience_default: 預設音場
      - transition_default: 預設轉場
      - source_file: 來源檔名
    """
    text = Path(path).read_text(encoding="utf-8")
    meta, body = extract_frontmatter(text)

    parser = DialogueParser(
        compile_mode=compile_mode,
        narrator_speaker=narrator_speaker,
    )
    segments = parser.parse(text)

    # 收集 metadata
    ambience_default = meta.get("ambience_default", "none")
    transition_default = meta.get("transition_default", "none")
    narrator = meta.get("narrator", narrator_speaker)

    # 偵測實際使用的模式
    if compile_mode == "auto":
        actual_mode = parser._detect_mode(body)
    else:
        actual_mode = compile_mode

    metadata = {
        "compile_mode": actual_mode,
        "narrator": narrator,
        "ambience_default": ambience_default,
        "transition_default": transition_default,
        "source_file": str(path),
    }
    return segments, metadata


# ─── CLI ────────────────────────────────────────────────

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="王朝劇本剖析器 v1.2")
    parser.add_argument("input", help="劇本檔案路徑")
    parser.add_argument("--output", "-o", help="輸出 JSON 路徑（預設 stdout）")
    parser.add_argument("--mode", choices=["auto", "dialogue", "narration"], default="auto",
                        help="編譯模式（預設 auto 自動偵測）")
    parser.add_argument("--narrator", default=DEFAULT_NARRATOR,
                        help=f"朗讀模式朗讀人（預設 {DEFAULT_NARRATOR}）")
    args = parser.parse_args()

    p = DialogueParser(compile_mode=args.mode, narrator_speaker=args.narrator)
    segments = p.parse_file(args.input)

    if args.output:
        segments_to_json(segments, args.output)
        print(f"✅ 剖析完成：{len(segments)} 段 → {args.output}")
    else:
        for i, seg in enumerate(segments, 1):
            extras = []
            if seg.mood:
                extras.append(f"mood={seg.mood}")
            if seg.ambience and seg.ambience != "none":
                extras.append(f"ambience={seg.ambience}")
            if seg.transition and seg.transition != "none":
                extras.append(f"transition={seg.transition}")
            if seg.act_title:
                extras.append(f"act={seg.act_title}")
            extra_str = f"  [{', '.join(extras)}]" if extras else ""
            print(f"[{i}] {seg.speaker}：{seg.text[:60]}{'...' if len(seg.text) > 60 else ''}{extra_str}")


if __name__ == "__main__":
    main()
