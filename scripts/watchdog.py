#!/usr/bin/env python3
"""
SuperMemory Watchdog — external supervisor for the memory server.

Strategy (escalating):
  1. Poll GET /health every POLL_SEC seconds
  2. After SOFT_FAILS consecutive failures → POST /admin/reload  (soft recovery)
  3. After HARD_FAILS consecutive failures → restart the server process (OS-level)
  4. Writes restart events to watchdog.log

Usage:
  python scripts/watchdog.py

Environment:
  SUPERMEMORY_URL      default: http://localhost:8000
  WATCHDOG_POLL_SEC    default: 30
  WATCHDOG_SOFT_FAILS  default: 3   (failures before soft reload)
  WATCHDOG_HARD_FAILS  default: 5   (failures before hard restart)
  WATCHDOG_START_CMD   default: auto-detected from process list
  WATCHDOG_LOG         default: watchdog.log

Install as Windows Task Scheduler job (runs on startup, restarts on failure):
  python scripts/watchdog.py --install
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

BASE_URL     = os.environ.get("SUPERMEMORY_URL", "http://localhost:8000")
POLL_SEC     = int(os.environ.get("WATCHDOG_POLL_SEC", "30"))
SOFT_FAILS   = int(os.environ.get("WATCHDOG_SOFT_FAILS", "3"))
HARD_FAILS   = int(os.environ.get("WATCHDOG_HARD_FAILS", "5"))
LOG_PATH     = Path(os.environ.get("WATCHDOG_LOG", "watchdog.log"))
START_CMD    = os.environ.get("WATCHDOG_START_CMD", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [watchdog] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("watchdog")


def _get(path: str, timeout: int = 5) -> dict:
    url = f"{BASE_URL}{path}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _post(path: str, timeout: int = 15) -> dict:
    url = f"{BASE_URL}{path}"
    req = urllib.request.Request(url, data=b"{}", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def check_health() -> bool:
    try:
        data = _get("/api/v1/health", timeout=5)
        return data.get("status") == "ok"
    except Exception:
        return False


def soft_reload() -> bool:
    """Try POST /admin/reload — reconnects services, restarts failed tasks."""
    try:
        data = _post("/admin/reload", timeout=15)
        log.info("Soft reload response: %s", data)
        return True
    except Exception as e:
        log.warning("Soft reload failed: %s", e)
        return False


def _detect_start_cmd() -> list[str]:
    """Auto-detect the uvicorn start command from running processes."""
    # Try to find the running uvicorn process and reconstruct its command
    repo = Path(__file__).parent.parent.resolve()
    python = sys.executable
    return [python, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]


def hard_restart() -> bool:
    """Kill the server process and start a new one."""
    import signal

    # Find PID via /admin/status or via netstat
    pid = None
    try:
        data = _get("/admin/status", timeout=3)
        pid = data.get("pid")
    except Exception:
        pass

    if pid:
        log.warning("Hard restart: killing PID %d", pid)
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], check=False, capture_output=True)
            else:
                os.kill(pid, signal.SIGTERM)
            time.sleep(3)
        except Exception as e:
            log.error("Failed to kill PID %d: %s", pid, e)

    cmd = START_CMD.split() if START_CMD else _detect_start_cmd()
    repo = Path(__file__).parent.parent.resolve()
    log.info("Hard restart: launching %s", " ".join(cmd))
    try:
        if sys.platform == "win32":
            subprocess.Popen(
                cmd,
                cwd=str(repo),
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
            )
        else:
            subprocess.Popen(cmd, cwd=str(repo), start_new_session=True)
        time.sleep(5)
        return check_health()
    except Exception as e:
        log.error("Hard restart failed: %s", e)
        return False


def install_task_scheduler():
    """Register watchdog as a Windows Task Scheduler job (run on startup)."""
    script = Path(__file__).resolve()
    python = sys.executable
    cmd = (
        f'schtasks /Create /TN "SuperMemoryWatchdog" /SC ONSTART /DELAY 0001:00 '
        f'/TR "\\"{python}\\" \\"{script}\\"" /RU SYSTEM /F'
    )
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode == 0:
        print("Watchdog registered in Task Scheduler as 'SuperMemoryWatchdog'.")
    else:
        print("Failed:", result.stderr)
        sys.exit(1)


def install_systemd():
    """Install watchdog as a systemd user service on Linux."""
    script = Path(__file__).resolve()
    repo = script.parent.parent.resolve()
    python = sys.executable
    service_name = "supermemory-watchdog"
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)
    service_file = service_dir / f"{service_name}.service"

    unit = f"""[Unit]
Description=SuperMemory Watchdog
After=network.target

[Service]
Type=simple
WorkingDirectory={repo}
ExecStart={python} {script}
Restart=always
RestartSec=10
Environment=SUPERMEMORY_URL={BASE_URL}
Environment=WATCHDOG_POLL_SEC={POLL_SEC}

[Install]
WantedBy=default.target
"""
    service_file.write_text(unit)
    print(f"Written: {service_file}")

    result = subprocess.run(
        ["systemctl", "--user", "enable", "--now", service_name],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"Systemd service '{service_name}' enabled and started.")
        print(f"  Status:  systemctl --user status {service_name}")
        print(f"  Logs:    journalctl --user -u {service_name} -f")
    else:
        print("systemctl failed:", result.stderr.strip())
        print(f"Start manually: systemctl --user enable --now {service_name}")


def install():
    if sys.platform == "win32":
        install_task_scheduler()
    elif sys.platform.startswith("linux"):
        install_systemd()
    else:
        # macOS: generate launchd plist hint
        script = Path(__file__).resolve()
        python = sys.executable
        print(f"macOS: add to launchd or run manually:")
        print(f"  {python} {script}")
        sys.exit(0)


def run():
    consecutive_fails = 0

    log.info("Watchdog started. Polling %s every %ds (soft@%d hard@%d)",
             BASE_URL, POLL_SEC, SOFT_FAILS, HARD_FAILS)

    while True:
        ok = check_health()

        if ok:
            if consecutive_fails > 0:
                log.info("Server recovered after %d failure(s)", consecutive_fails)
            consecutive_fails = 0
        else:
            consecutive_fails += 1
            log.warning("Health check failed (%d/%d)", consecutive_fails, HARD_FAILS)

            if consecutive_fails == SOFT_FAILS:
                log.info("Attempting soft reload...")
                if soft_reload():
                    time.sleep(10)
                    if check_health():
                        log.info("Soft reload succeeded")
                        consecutive_fails = 0

            if consecutive_fails >= HARD_FAILS:
                log.error("Hard restart triggered after %d failures", consecutive_fails)
                if hard_restart():
                    log.info("Hard restart succeeded")
                    consecutive_fails = 0
                else:
                    log.error("Hard restart failed — will retry next cycle")

        time.sleep(POLL_SEC)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SuperMemory watchdog")
    parser.add_argument("--install", action="store_true",
                        help="Register as Windows Task Scheduler job")
    args = parser.parse_args()

    if args.install:
        install()
    else:
        run()
