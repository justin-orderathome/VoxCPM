# 王朝 VoxCPM 語音引擎（feat/wangdom-voice-engine）

## 快速測試

```bash
cd ~/projects/VoxCPM
source venv/bin/activate

# 單人語音（VoxCPM 優先）
python scripts/wangdom/voxcpm_skill.py \
  --text "主公萬安" \
  --character "軍師·諸葛亮" \
  --output test_output/quick_test.wav

# 帶情緒控制
python scripts/wangdom/voxcpm_skill.py \
  --text "稟主公，大事不好！" \
  --character "御史·魏徵" \
  --mood "急切" \
  --output test_output/mood_test.wav

# 帶環境音場（朝堂殘響）
python scripts/wangdom/voxcpm_skill.py \
  --text "退朝！" \
  --character "主公·劉邦" \
  --ambience hall \
  --output test_output/hall_test.wav

# 多人劇本
python scripts/wangdom/merger.py \
  test_output/test_script.txt \
  -o test_output/full_dialogue.wav

# 強制 Edge TTS fallback
python scripts/wangdom/voxcpm_skill.py \
  --text "fallback 測試" \
  --character "軍師·諸葛亮" \
  --force-engine edge_tts \
  --output test_output/edge_test.mp3
```

## 模組架構

```
scripts/wangdom/
├── voxcpm_skill.py       # 主 Router + Voice Profile Manager
├── style_compiler.py     # mood / expression_tags → VoxCPM 控制指令
├── audio_post.py         # ambience 後處理（reverb/EQ/正規化）
├── dialogue_parser.py    # 多人劇本剖析器
└── merger.py             # 多人語音合併器

profiles/
└── voice_profiles.yaml   # 17 角色音色配置
```

## 情緒控制

| 情緒 | 說明 |
|------|------|
| 沉穩 | 語氣沉穩內斂，從容不迫 |
| 莊嚴 | 語調莊重嚴肅，不怒自威 |
| 剛正 | 語氣堅定有力，鏗鏘正直 |
| 自信 | 語氣自信果斷，底氣十足 |
| 歡快 | 語氣輕快活潑，帶笑意 |
| 急切 | 語速稍快，語氣急促緊迫 |
| 恭敬 | 語氣恭敬有禮，畢恭畢敬 |

## 環境音場

| Profile | 效果 |
|---------|------|
| none | 不處理（預設） |
| studio | 輕微壓縮 + 提升，錄音室效果 |
| hall | 殘響 + 低頻增強，大殿/朝堂 |
| battle | 低頻增強 + 輕微失真，戰場氛圍 |
| rain | 輕微殘響，雨天環境 |
| cave | 長殘響 + 低頻衰減，洞穴/密室 |

## Hermes 整合

VoxCPM 已註冊為 Hermes TTS provider（`feat/voxcpm-tts-provider` 分支）。

啟用方式：在 `~/.hermes/config.yaml` 設定：
```yaml
tts:
  provider: voxcpm
```

Router 會自動偵測 VRAM，不足時 fallback Edge TTS。
