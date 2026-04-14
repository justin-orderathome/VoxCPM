#!/usr/bin/env python3
"""王朝情緒/表情轉譯器

將高階 mood / expression_tags 欄位轉譯為 VoxCPM 原生控制指令。

職責：
  1. mood → 編譯為括號描述（control description）
  2. expression_tags → 注入 [laughing][sigh] 等非語言標籤
  3. 合成最終 text（供 model.generate() 使用）

不處理 ambience_profile（由 audio_post.py 負責）。
"""

from __future__ import annotations

from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Mood → Control Description 映射表
# ---------------------------------------------------------------------------

_MOOD_MAP: Dict[str, str] = {
    # 朝政常用
    "沉穩": "語氣沉穩內斂，從容不迫",
    "莊嚴": "語調莊重嚴肅，不怒自威",
    "剛正": "語氣堅定有力，鏗鏘正直",
    "自信": "語氣自信果斷，底氣十足",
    "謹慎": "語調審慎小心，字斟句酌",
    "恭敬": "語氣恭敬有禮，畢恭畢敬",
    "急切": "語速稍快，語氣急促緊迫",
    "歡快": "語氣輕快活潑，帶笑意",
    "悲憤": "語調低沉悲壯，帶壓抑的憤怒",
    "驚訝": "語調上揚，帶意外之情",
    "溫和": "語氣溫和親切，柔和從容",
    "冷酷": "語調冰冷疏離，不帶感情",
    # 司樂署
    "戲劇": "語氣誇張戲劇化，聲調起伏大",
    "朗讀": "語速適中，吐字清晰，朗誦風格",
    "輕鬆": "語氣輕鬆隨意，像聊天",
}

# expression_tags → VoxCPM 非語言標籤白名單
_VALID_NW_TAGS = frozenset({
    "laughing", "sigh",
    "Question-ah", "Question-ei", "Question-en", "Question-oh",
    "Surprise-wa", "Surprise-yo",
    "Dissatisfaction-hnn",
    "Uhm", "Shh",
})


def compile_text(
    text: str,
    mood: Optional[str] = None,
    expression_tags: Optional[List[str]] = None,
    profile_description: Optional[str] = None,
) -> str:
    """將 mood + expression_tags + profile_description 編譯為最終 text。

    VoxCPM 控制方式：
      - clone / ultimate_clone 模式：控制描述放在括號內，置於台詞前方
        → "(語氣沉穩，書卷氣)主公萬安"
      - design 模式：同上，但描述即為音色定義
        → "(儒雅清朗的青年男性)主公萬安"

    Args:
        text: 原始台詞
        mood: 情緒基調（如「沉穩」「莊嚴」）
        expression_tags: 非語言標籤列表（如 ["laughing", "sigh"]）
        profile_description: 角色 voice profile 的 description（前置）

    Returns:
        編譯後的完整文字字串
    """
    parts: List[str] = []

    # 1. Profile description（角色音色定義，最前面）
    if profile_description:
        desc = profile_description.strip()
        # 去掉外層括號（voice_profiles.yaml 中已含括號）
        if desc.startswith("(") and desc.endswith(")"):
            desc = desc[1:-1].strip()
        parts.append(desc)

    # 2. Mood → control description
    if mood:
        mood_lower = mood.strip().lower()
        mood_desc = _MOOD_MAP.get(mood.strip(), mood.strip())
        parts.append(mood_desc)

    # 3. 組合控制前綴
    control_prefix = ""
    if parts:
        control_prefix = f"({', '.join(parts)})"

    # 4. 注入 expression_tags 到台詞中
    processed_text = text
    if expression_tags:
        valid_tags = [t for t in expression_tags if t in _VALID_NW_TAGS]
        if valid_tags:
            # 在台詞前方注入標籤
            tag_str = " ".join(f"[{t}]" for t in valid_tags)
            processed_text = f"{tag_str} {text}"

    # 5. 最終組合
    if control_prefix:
        return f"{control_prefix}{processed_text}"
    return processed_text


def get_mood_description(mood: str) -> str:
    """查詢 mood 對應的 VoxCPM 控制描述"""
    return _MOOD_MAP.get(mood, mood)


def list_moods() -> Dict[str, str]:
    """列出所有可用 mood"""
    return dict(_MOOD_MAP)
