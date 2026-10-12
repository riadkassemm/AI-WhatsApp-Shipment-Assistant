"""Install the local bridge as a persistent systemd user service.

Run from any directory with the project's Python environment. The service reads
the existing private .env itself; credentials are never copied into its unit.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
UNIT_NAME = "whatsapp-support-bridge.service"


def unit_path(value: Path, *, executable: bool = False) -> str:
    text = str(value)
    if any(character in text for character in "\r\n\x00"):
        raise ValueError("Service paths cannot contain newlines or null bytes.")
    text = text.replace("\\", "\\\\").replace("%", "%%")
    if executable:
        text = text.replace('"', '\\"').replace("$", "$$")
        return f'"{text}"'
    # WorkingDirectory consumes a single path and does not remove surrounding
    # quotes, unlike the argument parser used by ExecStart.
    return text.replace('"', "\\x22").replace(" ", "\\x20").replace("\t", "\\x09")


def build_unit(node: Path) -> str:
    return f"""[Unit]
Description=WhatsApp Support Linked Device Bridge
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory={unit_path(ROOT / "whatsapp_bridge")}
ExecStart={unit_path(node, executable=True)} {unit_path(ROOT / "whatsapp_bridge/src/index.js", executable=True)}
Restart=always
RestartSec=5
TimeoutStopSec=35
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=default.target
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print", action="store_true", help="Preview the unit without installing or starting it.")
    args = parser.parse_args()
    node_binary = shutil.which("node")
    if not node_binary:
        raise RuntimeError("Install Node.js 22 or newer first.")
    node = Path(node_binary).resolve()
    version = subprocess.check_output([str(node), "--version"], text=True).strip()
    if int(version.removeprefix("v").split(".")[0]) < 22:
        raise RuntimeError("The bridge service requires Node.js 22 or newer.")
    unit = build_unit(node)
    if args.print:
        print(unit, end="")
        return
    if not (ROOT / ".env").is_file():
        raise RuntimeError("Configure the project's private .env first.")
    if not (ROOT / "whatsapp_bridge/node_modules/@whiskeysockets/baileys").is_dir():
        raise RuntimeError("Run npm ci --prefix whatsapp_bridge --include=optional --ignore-scripts first.")
    subprocess.run(["systemctl", "--user", "show-environment"], check=True, stdout=subprocess.DEVNULL)
    config_root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    if not config_root.is_absolute():
        raise RuntimeError("XDG_CONFIG_HOME must be an absolute path.")
    directory = config_root / "systemd/user"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / UNIT_NAME
    if destination.is_symlink():
        raise RuntimeError(f"Refusing to replace a symlink at {destination}.")
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, suffix=".service", delete=False) as handle:
            temporary = handle.name
            os.chmod(temporary, 0o600)
            handle.write(unit)
            handle.flush()
            os.fsync(handle.fileno())
        subprocess.run(["systemd-analyze", "--user", "verify", temporary], check=True)
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", UNIT_NAME], check=True)
    print(f"Installed and started {UNIT_NAME} for this user.")
    print(f"Status: systemctl --user status {UNIT_NAME}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Bridge service setup failed: {exc}", file=sys.stderr)
        sys.exit(1)
