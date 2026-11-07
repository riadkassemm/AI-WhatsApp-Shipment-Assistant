"""Run the committed offline adapter as the data owner; never start a bridge."""
import argparse
import grp
import os
from pathlib import Path
import pwd
import subprocess


def main():
    from dotenv import dotenv_values
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "finalize"])
    parser.add_argument("target")
    parser.add_argument("record")
    parser.add_argument("old_release")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("ROOT_OPERATOR_REQUIRED")
    values = dotenv_values("/etc/whatsapp-support/app.env")
    directory = Path(values["WHATSAPP_LINKED_DEVICE_DATA_DIRECTORY"])
    if not directory.is_absolute():
        raise SystemExit("ABSOLUTE_DATA_DIRECTORY_REQUIRED")
    owner = pwd.getpwuid(directory.stat().st_uid)
    journal = grp.getgrnam("systemd-journal").gr_gid
    env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": owner.pw_dir,
           **{k: v for k, v in values.items() if v is not None}}

    def drop_privileges():
        os.setgroups([owner.pw_gid, journal])
        os.setgid(owner.pw_gid)
        os.setuid(owner.pw_uid)
        os.umask(0o077)

    tool = Path(__file__).resolve().parents[1] / "whatsapp_bridge/src/legacy-decommission-transition.js"
    result = subprocess.run(["node", str(tool), args.command, args.target, args.record, args.old_release],
                            env=env, preexec_fn=drop_privileges, check=False)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
