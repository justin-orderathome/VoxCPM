#!/usr/bin/env python3
"""voice_bot_healthcheck.py

Systemd user-service health check for Wangdom Discord voice bot.
- Verifies service active state
- Verifies MainPID exists
- Emits JSON/text report
- Optional auto-fix: reset-failed + restart service when unhealthy
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

SERVICE_NAME = "voice-bot.service"
PROJECT_ROOT = Path(os.environ.get("VOXCPM_PATH", str(Path(__file__).resolve().parent.parent.parent)))
LOG_PATH = PROJECT_ROOT / "output" / "voice-bot-healthcheck.log"
HEARTBEAT_PATH = PROJECT_ROOT / "output" / "voice-bot-heartbeat.json"
HEARTBEAT_STALE_SECONDS = 120  # heartbeat older than 2 min = stale


@dataclass
class HealthReport:
    timestamp_utc: str
    service: str
    active_state: str
    sub_state: str
    main_pid: int
    pid_alive: bool
    has_established_network_socket: bool
    heartbeat_ok: bool
    heartbeat_age_seconds: float
    healthy: bool
    actions: list[str]
    notes: list[str]


def run(cmd: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def systemctl_show(service: str, key: str) -> str:
    code, out, _ = run(["systemctl", "--user", "show", service, "-p", key, "--value"])
    if code != 0:
        return ""
    return out.strip()


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    return Path(f"/proc/{pid}").exists()


def has_pid_socket(pid: int) -> bool:
    if pid <= 0:
        return False
    code, out, _ = run(["ss", "-tpn", "state", "established"])
    if code != 0:
        return False
    needle = f"pid={pid},"
    for line in out.splitlines():
        if needle in line:
            return True
    return False


def healthcheck(auto_fix: bool = False) -> HealthReport:
    actions: list[str] = []
    notes: list[str] = []

    active_state = systemctl_show(SERVICE_NAME, "ActiveState") or "unknown"
    sub_state = systemctl_show(SERVICE_NAME, "SubState") or "unknown"
    main_pid_raw = systemctl_show(SERVICE_NAME, "MainPID") or "0"
    try:
        main_pid = int(main_pid_raw)
    except ValueError:
        main_pid = 0

    pid_ok = pid_alive(main_pid)
    socket_ok = has_pid_socket(main_pid)

    # Heartbeat check
    heartbeat_ok = False
    heartbeat_age = -1.0
    if HEARTBEAT_PATH.exists():
        try:
            hb = json.loads(HEARTBEAT_PATH.read_text(encoding="utf-8"))
            hb_ts = datetime.fromisoformat(hb.get("timestamp", ""))
            now_utc = datetime.now(timezone.utc)
            hb_utc = hb_ts.astimezone(timezone.utc)
            heartbeat_age = (now_utc - hb_utc).total_seconds()
            heartbeat_ok = heartbeat_age < HEARTBEAT_STALE_SECONDS
        except Exception:
            heartbeat_ok = False

    healthy = active_state == "active" and pid_ok and heartbeat_ok

    if not healthy and auto_fix:
        # Recover from failed-throttle states then restart
        run(["systemctl", "--user", "reset-failed", SERVICE_NAME])
        actions.append("systemctl --user reset-failed voice-bot.service")

        code, _, err = run(["systemctl", "--user", "restart", SERVICE_NAME])
        if code == 0:
            actions.append("systemctl --user restart voice-bot.service")
            # Re-evaluate once
            active_state = systemctl_show(SERVICE_NAME, "ActiveState") or active_state
            sub_state = systemctl_show(SERVICE_NAME, "SubState") or sub_state
            main_pid_raw = systemctl_show(SERVICE_NAME, "MainPID") or str(main_pid)
            try:
                main_pid = int(main_pid_raw)
            except ValueError:
                main_pid = 0
            pid_ok = pid_alive(main_pid)
            socket_ok = has_pid_socket(main_pid)
            healthy = active_state == "active" and pid_ok
        else:
            notes.append(f"restart failed: {err or 'unknown error'}")

    if not socket_ok and healthy:
        notes.append("service active but no established socket observed; may be transient or idle")
    if not heartbeat_ok and pid_ok:
        notes.append(f"heartbeat stale ({heartbeat_age:.0f}s old); Discord gateway may be disconnected")

    return HealthReport(
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        service=SERVICE_NAME,
        active_state=active_state,
        sub_state=sub_state,
        main_pid=main_pid,
        pid_alive=pid_ok,
        has_established_network_socket=socket_ok,
        heartbeat_ok=heartbeat_ok,
        heartbeat_age_seconds=round(heartbeat_age, 1),
        healthy=healthy,
        actions=actions,
        notes=notes,
    )


def append_log(report: HealthReport) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(report), ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Health check for voice-bot.service")
    parser.add_argument("--auto-fix", action="store_true", help="Auto restart service when unhealthy")
    parser.add_argument("--json", action="store_true", help="Print report as JSON")
    args = parser.parse_args()

    report = healthcheck(auto_fix=args.auto_fix)
    append_log(report)

    if args.json:
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    else:
        status = "HEALTHY" if report.healthy else "UNHEALTHY"
        print(
            f"[{status}] {report.service} "
            f"active={report.active_state}/{report.sub_state} "
            f"pid={report.main_pid} pid_alive={report.pid_alive} "
            f"socket={report.has_established_network_socket}"
        )
        if report.actions:
            print("actions:")
            for a in report.actions:
                print(f"  - {a}")
        if report.notes:
            print("notes:")
            for n in report.notes:
                print(f"  - {n}")

    return 0 if report.healthy else 1


if __name__ == "__main__":
    sys.exit(main())
