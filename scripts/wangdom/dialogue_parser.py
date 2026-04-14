#!/usr/bin/env python3
"""王朝多人劇本剖析器

將文字劇本剖析為結構化對話段，供 merger.py 逐段生成後合併。

支援格式：
  1. 標準格式 — 「角色名：台詞」
  2. Markdown 格式 — **角色名** 台詞
  3. 純文字 + style 指令 — 支援 mood / expression_tags 欄位

輸出格式（DialogueSegment list）：
  {
    "speaker": "軍師·諸葛亮",
    "text": "主公萬安",
    "mood": "沉穩",           # 可選
    "expression_tags": [...],  # 可選
    "pause_after": 0.5,       # 可選，秒
  }
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class DialogueSegment:
    """單一對話段"""
    speaker: str
    text: str
    mood: Optional[str] = None
    expression_tags: Optional[List[str]] = None
    pause_after: float = 0.5  # 預設間隙 0.5 秒

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None and not (isinstance(v, float) and v == 0.5 and k == "pause_after")}


class DialogueParser:
    """劇本剖析器，支援多種輸入格式"""

    # 格式 1：「角色名：台詞」或「角色名：台詞」
    _COLON_RE = re.compile(r"^【?(.+?)】?\s*[:：]\s*(.+)$")
    # 格式 2：**角色名** 台詞
    _MARKDOWN_RE = re.compile(r"^\*\*(.+?)\*\*\s*(.*)$")
    # 格式 3：style 指令行 — [mood:XXX] 或 [tags:xxx,yyy]
    _MOOD_RE = re.compile(r"^\[mood[:：]\s*(.+)\]$", re.IGNORECASE)
    _TAGS_RE = re.compile(r"^\[tags[:：]\s*(.+)\]$", re.IGNORECASE)
    _PAUSE_RE = re.compile(r"^\[pause[:：]\s*([\d.]+)\]$", re.IGNORECASE)
    # 非語言標籤（VoxCPM 原生支援）
    _NV_TAG_RE = re.compile(r"\[(laughing|sigh|Question-\w+|Surprise-\w+|Dissatisfaction-\w+|Uhm|Shh)\]", re.IGNORECASE)
    # 空行 / 註解
    _BLANK_RE = re.compile(r"^\s*$")
    _COMMENT_RE = re.compile(r"^\s*#\s*")

    def __init__(self, default_pause: float = 0.5):
        self.default_pause = default_pause

    def parse(self, script: str) -> List[DialogueSegment]:
        """剖析劇本文字，回傳對話段列表。

        Args:
            script: 劇本文字（多行）

        Returns:
            DialogueSegment 列表
        """
        segments: List[DialogueSegment] = []
        current_mood: Optional[str] = None
        current_tags: Optional[List[str]] = None
        current_pause: float = self.default_pause

        for line in script.splitlines():
            # 空行 → 重置 style
            if self._BLANK_RE.match(line):
                current_mood = None
                current_tags = None
                current_pause = self.default_pause
                continue

            # 註解 → 跳過
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

            # 對話行：格式 1 — 角色名：台詞
            m = self._COLON_RE.match(line.strip())
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

            # 對話行：格式 2 — **角色名** 台詞
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

            # 格式 3：純文字（沿用上一個 speaker 或標為 "narrator"）
            if line.strip() and segments:
                # 連續純文字 → 附加到上一段
                last = segments[-1]
                last.text += "\n" + line.strip()
            elif line.strip():
                segments.append(DialogueSegment(
                    speaker="旁白",
                    text=line.strip(),
                    mood=current_mood,
                    expression_tags=current_tags,
                    pause_after=current_pause,
                ))

        return segments

    def parse_file(self, path: str | Path) -> List[DialogueSegment]:
        """從檔案讀取劇本並剖析"""
        text = Path(path).read_text(encoding="utf-8")
        return self.parse(text)

    @staticmethod
    def _extract_nv_tags(text: str) -> Optional[List[str]]:
        """從台詞中抽取 VoxCPM 非語言標籤"""
        tags = DialogueParser._NV_TAG_RE.findall(text)
        return tags if tags else None


def segments_to_json(segments: List[DialogueSegment], output_path: str | Path) -> None:
    """將剖析結果存為 JSON"""
    data = [s.to_dict() for s in segments]
    Path(output_path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="王朝劇本剖析器")
    parser.add_argument("input", help="劇本檔案路徑")
    parser.add_argument("--output", "-o", help="輸出 JSON 路徑（預設 stdout）")
    args = parser.parse_args()

    p = DialogueParser()
    segments = p.parse_file(args.input)

    if args.output:
        segments_to_json(segments, args.output)
        print(f"✅ 剖析完成：{len(segments)} 段 → {args.output}")
    else:
        for i, seg in enumerate(segments, 1):
            print(f"[{i}] {seg.speaker}：{seg.text}")
            if seg.mood:
                print(f"    mood: {seg.mood}")
            if seg.expression_tags:
                print(f"    tags: {seg.expression_tags}")


if __name__ == "__main__":
    main()
