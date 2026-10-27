import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import backup  # noqa: E402


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    files = {
        "agent/memory/MEMORY.md": "index v1",
        "agent/jobs.json": "[]",
        "agent/.claude/skills/learned/SKILL.md": "learned",
        "agent/inbox_files/photo.jpg": "big",
        "agent/CLAUDE.md": "tracked in git",
        "agents/research/memory/MEMORY.md": "research v1",
        "state/agents/main/session.json": '{"session_id": "s1"}',
        ".env": "TOKEN=secret",
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root, tmp_path / "backups"


def names(archive):
    with tarfile.open(archive) as tar:
        return set(tar.getnames())


def test_backup_holds_memory_of_all_agents_but_not_secrets_attachments_or_tracked_code(repo):
    root, bdir = repo
    archive = backup.create(root, bdir)
    content = names(archive)

    for rel in ["agent/memory/MEMORY.md", "agent/jobs.json", "agent/.claude/skills/learned/SKILL.md",
                "agents/research/memory/MEMORY.md", "state/agents/main/session.json", "manifest.json"]:
        assert rel in content
    assert not {".env", "agent/inbox_files/photo.jpg", "agent/CLAUDE.md"} & content


def test_env_only_on_request(repo):
    root, bdir = repo
    assert ".env" in names(backup.create(root, bdir, with_env=True))


def test_restore_brings_back_old_state_and_removes_newer_files(repo):
    root, bdir = repo
    archive = backup.create(root, bdir)
    (root / "agent/memory/MEMORY.md").write_text("index v2")
    (root / "agents/later/memory").mkdir(parents=True)  # agent created after the backup

    safety = backup.restore(archive, root, bdir)

    assert (root / "agent/memory/MEMORY.md").read_text() == "index v1"
    assert not (root / "agents/later").exists()
    assert (root / "agent/CLAUDE.md").exists() and (root / "agent/inbox_files/photo.jpg").exists()
    assert "pre-restore" in safety.name


def test_restore_can_be_undone_with_the_pre_restore_backup(repo):
    root, bdir = repo
    archive = backup.create(root, bdir)
    (root / "agent/memory/MEMORY.md").write_text("index v2")
    safety = backup.restore(archive, root, bdir)

    backup.restore(safety, root, bdir)

    assert (root / "agent/memory/MEMORY.md").read_text() == "index v2"


def test_restore_rejects_backup_from_other_layout(repo, monkeypatch):
    root, bdir = repo
    monkeypatch.setattr(backup, "LAYOUT", "v1")
    archive = backup.create(root, bdir)
    monkeypatch.setattr(backup, "LAYOUT", "multi-agent-v2")

    with pytest.raises(ValueError):
        backup.restore(archive, root, bdir)
    assert (root / "agent/memory/MEMORY.md").exists()  # nothing touched


def test_rotation_keeps_newest(repo, monkeypatch):
    root, bdir = repo
    for i in range(4):
        backup.create(root, bdir, keep=2, label=f"n{i}")
    kept = [b.name for b in backup.list_backups(bdir)]
    assert len(kept) == 2 and all(n in " ".join(kept) for n in ("n2", "n3"))
