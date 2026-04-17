# Wangdom Voice Bot — E2E 35 項重跑手冊

## 0) 前置
```bash
cd ~/projects/VoxCPM
source venv/bin/activate
systemctl --user status voice-bot.service
```

## 1) Layer A：離線 smoke
```bash
python scripts/wangdom/e2e_morning_smoke.py
python scripts/wangdom/e2e_greeting_smoke.py
python scripts/wangdom/e2e_drama_smoke.py
python scripts/wangdom/e2e_weekly_smoke.py
python scripts/wangdom/e2e_aux_smoke.py
```

## 2) Layer B：Discord 實機
使用測試資料：
- 劇本：`/home/hermes01/projects/VoxCPM/test_output/drama_scripts/demo_scene.txt`
- 週報：`/home/hermes01/projects/VoxCPM/test_output/weekly_reports/demo_weekly.txt`

### 指令清單
```text
!morning -f
!join
!morning -s

a. !greeting -f
b. !greeting -s

!drama -f /home/hermes01/projects/VoxCPM/test_output/drama_scripts/demo_scene.txt
!drama -s /home/hermes01/projects/VoxCPM/test_output/drama_scripts/demo_scene.txt

!weekly -f /home/hermes01/projects/VoxCPM/test_output/weekly_reports/demo_weekly.txt
!weekly -s /home/hermes01/projects/VoxCPM/test_output/weekly_reports/demo_weekly.txt

!mode auto
!mode file
!mode stream
!status
!queue
!voices
!stop
```

## 3) 驗收口徑
- 語音必須可播（file 附件/stream 播放）
- 文字同步必須存在
- `-s` 模式需逐句/逐段同步（不可只摘要）
- 錯誤訊息需可讀（缺檔、空內容、角色不存在）

## 4) 清理（可選）
```bash
python ~/.hermes/scripts/hermes_cleanup.py --dry-run
python ~/.hermes/scripts/hermes_cleanup.py
```