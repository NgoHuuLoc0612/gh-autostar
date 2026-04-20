"""
OS-level startup registration.

Registers `gh-autostar daemon start` to run automatically when the user
logs in, using the platform-appropriate mechanism:

  Linux   →  systemd user service (~/.config/systemd/user/)
  macOS   →  launchd LaunchAgent (~/Library/LaunchAgents/)
  Windows →  Windows Task Scheduler (via schtasks.exe)
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

from gh_autostar.logging_setup import get_logger

logger = get_logger("startup")

_UNIT_NAME = "gh-autostar"
_PLIST_LABEL = "com.gh-autostar.daemon"


class StartupRegistrar:
    """Handles cross-platform startup registration."""

    def __init__(self) -> None:
        self._platform = platform.system()

    def is_registered(self) -> bool:
        try:
            match self._platform:
                case "Linux":
                    return self._systemd_service_path().exists()
                case "Darwin":
                    return self._plist_path().exists()
                case "Windows":
                    return self._windows_task_exists()
                case _:
                    return False
        except Exception as exc:
            logger.debug("Error checking startup registration: %s", exc)
            return False

    def register(self) -> None:
        logger.info("Registering startup entry on %s.", self._platform)
        match self._platform:
            case "Linux":
                self._register_systemd()
            case "Darwin":
                self._register_launchd()
            case "Windows":
                self._register_windows()
            case _:
                logger.warning("Unsupported platform for startup: %s", self._platform)

    def unregister(self) -> None:
        logger.info("Removing startup entry on %s.", self._platform)
        match self._platform:
            case "Linux":
                self._unregister_systemd()
            case "Darwin":
                self._unregister_launchd()
            case "Windows":
                self._unregister_windows()
            case _:
                logger.warning("Unsupported platform for startup: %s", self._platform)

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _executable() -> str:
        """Return the full path to the gh-autostar CLI."""
        exe = shutil.which("gh-autostar") or sys.executable
        return str(exe)

    # ── Linux / systemd ───────────────────────────────────────────────────────

    @staticmethod
    def _systemd_dir() -> Path:
        p = Path.home() / ".config" / "systemd" / "user"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @staticmethod
    def _systemd_service_path() -> Path:
        return Path.home() / ".config" / "systemd" / "user" / f"{_UNIT_NAME}.service"

    def _register_systemd(self) -> None:
        exe = self._executable()
        unit = dedent(f"""\
            [Unit]
            Description=gh-autostar automated GitHub repo starring daemon
            After=network-online.target
            Wants=network-online.target

            [Service]
            Type=simple
            ExecStart={exe} daemon start
            Restart=on-failure
            RestartSec=30
            StandardOutput=journal
            StandardError=journal
            Environment=PATH={os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}

            [Install]
            WantedBy=default.target
        """)
        path = self._systemd_service_path()
        path.write_text(unit)
        logger.info("Written systemd unit: %s", path)

        # Enable and start
        _run(["systemctl", "--user", "daemon-reload"])
        _run(["systemctl", "--user", "enable", f"{_UNIT_NAME}.service"])
        _run(["systemctl", "--user", "start", f"{_UNIT_NAME}.service"])
        logger.info("systemd service enabled and started.")

    def _unregister_systemd(self) -> None:
        _run(["systemctl", "--user", "stop", f"{_UNIT_NAME}.service"], check=False)
        _run(["systemctl", "--user", "disable", f"{_UNIT_NAME}.service"], check=False)
        path = self._systemd_service_path()
        if path.exists():
            path.unlink()
        _run(["systemctl", "--user", "daemon-reload"])

    # ── macOS / launchd ───────────────────────────────────────────────────────

    @staticmethod
    def _plist_path() -> Path:
        p = Path.home() / "Library" / "LaunchAgents"
        p.mkdir(parents=True, exist_ok=True)
        return p / f"{_PLIST_LABEL}.plist"

    def _register_launchd(self) -> None:
        exe = self._executable()
        log_dir = Path.home() / "Library" / "Logs" / "gh-autostar"
        log_dir.mkdir(parents=True, exist_ok=True)

        plist = dedent(f"""\
            <?xml version="1.0" encoding="UTF-8"?>
            <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
              "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
            <plist version="1.0">
            <dict>
                <key>Label</key>
                <string>{_PLIST_LABEL}</string>
                <key>ProgramArguments</key>
                <array>
                    <string>{exe}</string>
                    <string>daemon</string>
                    <string>start</string>
                </array>
                <key>RunAtLoad</key>
                <true/>
                <key>KeepAlive</key>
                <true/>
                <key>StandardOutPath</key>
                <string>{log_dir}/stdout.log</string>
                <key>StandardErrorPath</key>
                <string>{log_dir}/stderr.log</string>
                <key>EnvironmentVariables</key>
                <dict>
                    <key>PATH</key>
                    <string>{os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}</string>
                </dict>
            </dict>
            </plist>
        """)
        path = self._plist_path()
        path.write_text(plist)
        logger.info("Written LaunchAgent plist: %s", path)

        _run(["launchctl", "load", "-w", str(path)])
        logger.info("LaunchAgent loaded.")

    def _unregister_launchd(self) -> None:
        path = self._plist_path()
        if path.exists():
            _run(["launchctl", "unload", "-w", str(path)], check=False)
            path.unlink()

    # ── Windows / Task Scheduler ──────────────────────────────────────────────

    @staticmethod
    def _windows_task_exists() -> bool:
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", _UNIT_NAME],
            capture_output=True,
        )
        return result.returncode == 0

    def _register_windows(self) -> None:
        exe = self._executable()
        _run([
            "schtasks", "/Create",
            "/TN", _UNIT_NAME,
            "/TR", f'"{exe}" daemon start',
            "/SC", "ONLOGON",
            "/RL", "HIGHEST",
            "/F",  # force overwrite
        ])
        logger.info("Windows scheduled task created: %s", _UNIT_NAME)

    def _unregister_windows(self) -> None:
        _run(["schtasks", "/Delete", "/TN", _UNIT_NAME, "/F"], check=False)


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=check
        )
        if result.returncode != 0 and check:
            logger.warning("Command %r failed:\n%s", cmd, result.stderr)
        return result
    except FileNotFoundError:
        logger.debug("Command not found: %s", cmd[0])
        return subprocess.CompletedProcess(cmd, returncode=127, stdout="", stderr="")
