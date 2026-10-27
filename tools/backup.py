"""
Back up and restore the bot's memory: everything durable that is not in git.

  python tools/backup.py create [--with-env] [--keep 14]
  python tools/backup.py list
  python tools/backup.py restore latest|<file> [--force]

A backup holds: agent/memory, agent/jobs.json, learned skills (agent/.claude/skills), all extra agents
(agents/), and state/ (sessions, chat history, inbox). `.env` only with --with-env (it holds secrets).
Creating a backup is safe while the bot runs (state files are written atomically).
Restore needs the bot stopped, first backs up the current state ("pre-restore"), then replaces each
backed-up path as a whole, so no files from two different states get mixed.
"""

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUP_DIR = ROOT / "backups"
LAYOUT = "multi-agent-v2"  # state/agents/<name>/ layout; v1-telegram backups do not fit it
PATHS = ["agent/memory", "agent/jobs.json", "agent/.claude/skills", "agents", "state"]
SKIP_NAMES = {"inbox_files", ".playwright-mcp", "__pycache__"}  # attachments and browser artifacts: large, not memory


def _skip(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    return None if SKIP_NAMES & set(Path(info.name).parts) else info


def create(root: Path = ROOT, backup_dir: Path = BACKUP_DIR, with_env: bool = False,
           keep: int = 14, label: str = "") -> Path:
    paths = [p for p in PATHS + ([".env"] if with_env else []) if (root / p).exists()]
    backup_dir.mkdir(parents=True, exist_ok=True)
    # Milliseconds: a restore makes a pre-restore backup right away, which must never overwrite the archive being read.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    name = f"protobot-{stamp}{'-' + label if label else ''}.tar.gz"
    target = backup_dir / name
    manifest = {"layout": LAYOUT, "created": datetime.now().isoformat(timespec="seconds"), "paths": paths}
    tmp = target.with_suffix(".tmp")
    with tarfile.open(tmp, "w:gz") as tar:
        data = json.dumps(manifest, indent=2).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
        for p in paths:
            tar.add(root / p, arcname=p, filter=_skip)
    os.replace(tmp, target)  # a half-written archive never looks like a valid backup
    rotate(backup_dir, keep)
    return target


def rotate(backup_dir: Path, keep: int) -> None:
    for old in list_backups(backup_dir)[keep:]:
        old.unlink()


def list_backups(backup_dir: Path = BACKUP_DIR) -> list[Path]:
    """Newest first."""
    return sorted(backup_dir.glob("protobot-*.tar.gz"), reverse=True)


def read_manifest(archive: Path) -> dict:
    with tarfile.open(archive, "r:gz") as tar:
        return json.load(tar.extractfile("manifest.json"))


def bot_running() -> bool:
    if os.name != "posix":
        return False  # no reliable check on Windows; the caller warns instead
    return subprocess.run(["pgrep", "-f", "dispatch.py"], capture_output=True).returncode == 0


def restore(archive: Path, root: Path = ROOT, backup_dir: Path = BACKUP_DIR) -> Path:
    """Returns the pre-restore backup of the current state."""
    manifest = read_manifest(archive)
    if manifest.get("layout") != LAYOUT:
        raise ValueError(f"Backup layout {manifest.get('layout')!r} does not match this version ({LAYOUT}).")
    safety = create(root, backup_dir, with_env=".env" in manifest["paths"], keep=10**6, label="pre-restore")
    for p in manifest["paths"]:
        target = root / p
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    with tarfile.open(archive, "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.name != "manifest.json"]
        tar.extractall(root, members=members, filter="data")  # "data": nothing outside root, no links out
    return safety


def main() -> None:
    parser = argparse.ArgumentParser(description="Back up / restore ProtoBot memory.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--with-env", action="store_true", help="also back up .env (secrets)")
    c.add_argument("--keep", type=int, default=14, help="how many backups to keep")
    sub.add_parser("list")
    r = sub.add_parser("restore")
    r.add_argument("archive", help="'latest' or a path to a backup file")
    r.add_argument("--force", action="store_true", help="skip the 'bot is running' check")
    args = parser.parse_args()

    if args.cmd == "create":
        print(create(with_env=args.with_env, keep=args.keep))
    elif args.cmd == "list":
        for b in list_backups():
            m = read_manifest(b)
            print(f"{b.name}  {b.stat().st_size / 1024:.0f} KB  {', '.join(m['paths'])}")
    else:
        if args.archive == "latest":
            backups = [b for b in list_backups() if "pre-restore" not in b.name]
            if not backups:
                sys.exit("No backups.")
            archive = backups[0]
        else:
            archive = Path(args.archive)
        if not args.force:
            if bot_running():
                sys.exit("The bot is running (dispatch.py). Stop it first, or pass --force.")
            if os.name != "posix":
                print("Warning: cannot check if the bot runs on this OS. Make sure it is stopped.")
        safety = restore(archive)
        print(f"Restored {archive.name}. Previous state saved as {safety.name}.")


if __name__ == "__main__":
    main()
