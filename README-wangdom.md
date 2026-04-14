# 王朝客製語音層（Phase 2）

本文件對應 PRD 的 Phase 2 封裝：
- `scripts/wangdom/voxcpm_skill.py`
- `profiles/voice_profiles.yaml`

## 功能
1. TTS Router：`VoxCPM2` 優先，VRAM 不足或 profile 指定時 fallback `Edge TTS`
2. Voice Profile Manager：讀取角色音色設定（YAML/JSON）
3. 取樣率鐵律：音檔輸出以 `model.tts_model.sample_rate` 為準

## 快速測試

```bash
cd ~/projects/VoxCPM
source venv/bin/activate
python scripts/wangdom/voxcpm_skill.py \
  --text "主公萬安，臣已備妥執行方案。" \
  --character "軍師·諸葛亮" \
  --output test_output/skill_demo_zhuge.wav
```

### 強制 fallback 測試

```bash
python scripts/wangdom/voxcpm_skill.py \
  --text "fallback 測試" \
  --character "軍師·諸葛亮" \
  --force-engine edge_tts \
  --output test_output/skill_demo_edge.mp3
```

> 若顯示 `edge_tts_not_installed`，先安裝：
>
> ```bash
> source venv/bin/activate
> pip install edge-tts
> ```
