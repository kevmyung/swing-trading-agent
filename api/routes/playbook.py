"""Playbook routes — /api/playbook/*

CRUD for investment playbook markdown files with override layer and version history.
Original files in playbook/ are never modified; edits are saved as overrides.
"""

import json
import logging
import os
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/playbook", tags=["playbook"])

BASE_DIR = Path(__file__).resolve().parent.parent.parent
PLAYBOOK_DIR = BASE_DIR / "playbook"
OVERRIDE_DIR = BASE_DIR / "playbook_overrides"
HISTORY_DIR = BASE_DIR / "playbook_history"

MAX_HISTORY = 10


# ─── Helpers ───────────────────────────────────────────────────────────────


def _topic_path(chapter: str, topic: str) -> str:
    """Validate and return safe relative path."""
    safe = os.path.normpath(f"{chapter}/{topic}")
    if ".." in safe or safe.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid path")
    return safe


def _default_path(chapter: str, topic: str) -> Path:
    return PLAYBOOK_DIR / chapter / f"{topic}.md"


def _override_path(chapter: str, topic: str) -> Path:
    return OVERRIDE_DIR / chapter / f"{topic}.md"


def _history_dir(chapter: str, topic: str) -> Path:
    return HISTORY_DIR / chapter / topic


def _effective_content(chapter: str, topic: str) -> str | None:
    """Read override if exists, else default."""
    override = _override_path(chapter, topic)
    if override.exists():
        return override.read_text(encoding="utf-8")
    default = _default_path(chapter, topic)
    if default.exists():
        return default.read_text(encoding="utf-8")
    return None


def _save_history(chapter: str, topic: str, content: str):
    """Save current content to history before overwriting."""
    hdir = _history_dir(chapter, topic)
    hdir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    (hdir / f"{ts}.md").write_text(content, encoding="utf-8")

    # Rotate: keep only MAX_HISTORY most recent
    files = sorted(hdir.glob("*.md"), key=lambda f: f.stem, reverse=True)
    for old in files[MAX_HISTORY:]:
        old.unlink()


def _list_history(chapter: str, topic: str) -> list[dict]:
    """List version history for a topic."""
    hdir = _history_dir(chapter, topic)
    if not hdir.exists():
        return []
    files = sorted(hdir.glob("*.md"), key=lambda f: f.stem, reverse=True)
    return [{"ts": int(f.stem), "file": f.name} for f in files]


# ─── Tree ──────────────────────────────────────────────────────────────────


@router.get("/tree")
def get_tree():
    """Return playbook tree with modification status."""
    if not PLAYBOOK_DIR.is_dir():
        return []

    tree = []
    # Include overview.md as a special entry
    overview = PLAYBOOK_DIR / "overview.md"
    if overview.exists():
        ov_override = OVERRIDE_DIR / "overview.md"
        tree.append({
            "chapter": "_root",
            "topic": "overview",
            "title": "Overview",
            "modified": ov_override.exists(),
        })

    for chapter_dir in sorted(PLAYBOOK_DIR.iterdir()):
        if not chapter_dir.is_dir():
            continue
        chapter = chapter_dir.name
        topics = []
        for f in sorted(chapter_dir.glob("*.md")):
            if f.name.startswith("_"):
                continue
            topic_name = f.stem
            # Extract title from first H1 line
            title = topic_name
            with open(f, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("# "):
                        title = line[2:].strip()
                        break
            override = OVERRIDE_DIR / chapter / f.name
            topics.append({
                "chapter": chapter,
                "topic": topic_name,
                "title": title,
                "modified": override.exists(),
            })
        if topics:
            tree.append({
                "chapter": chapter,
                "topics": topics,
            })
    return tree


# ─── Read ──────────────────────────────────────────────────────────────────


@router.get("/{chapter}/{topic}")
def get_topic(chapter: str, topic: str):
    """Get current effective content (override > default)."""
    _topic_path(chapter, topic)

    # Handle overview special case
    if chapter == "_root" and topic == "overview":
        override = OVERRIDE_DIR / "overview.md"
        default = PLAYBOOK_DIR / "overview.md"
        content = override.read_text(encoding="utf-8") if override.exists() else (
            default.read_text(encoding="utf-8") if default.exists() else None
        )
        if content is None:
            raise HTTPException(status_code=404, detail="overview.md not found")
        return {
            "content": content,
            "is_modified": override.exists(),
            "history": _list_overview_history(),
        }

    content = _effective_content(chapter, topic)
    if content is None:
        raise HTTPException(status_code=404, detail=f"{chapter}/{topic} not found")

    override = _override_path(chapter, topic)
    return {
        "content": content,
        "is_modified": override.exists(),
        "history": _list_history(chapter, topic),
    }


@router.get("/{chapter}/{topic}/default")
def get_topic_default(chapter: str, topic: str):
    """Get original default content (for diff)."""
    _topic_path(chapter, topic)

    if chapter == "_root" and topic == "overview":
        default = PLAYBOOK_DIR / "overview.md"
        if not default.exists():
            raise HTTPException(status_code=404)
        return {"content": default.read_text(encoding="utf-8")}

    default = _default_path(chapter, topic)
    if not default.exists():
        raise HTTPException(status_code=404, detail=f"Default {chapter}/{topic} not found")
    return {"content": default.read_text(encoding="utf-8")}


# ─── Write ─────────────────────────────────────────────────────────────────


class SaveBody(BaseModel):
    content: str


@router.put("/{chapter}/{topic}")
def save_topic(chapter: str, topic: str, body: SaveBody):
    """Save override. Backs up current version to history."""
    _topic_path(chapter, topic)

    if chapter == "_root" and topic == "overview":
        override = OVERRIDE_DIR / "overview.md"
        # Save history
        current = override.read_text(encoding="utf-8") if override.exists() else (
            (PLAYBOOK_DIR / "overview.md").read_text(encoding="utf-8")
            if (PLAYBOOK_DIR / "overview.md").exists() else None
        )
        if current:
            _save_overview_history(current)
        OVERRIDE_DIR.mkdir(parents=True, exist_ok=True)
        override.write_text(body.content, encoding="utf-8")
        return {"status": "saved"}

    # Ensure default exists
    if not _default_path(chapter, topic).exists():
        raise HTTPException(status_code=404, detail=f"Default {chapter}/{topic} not found")

    # Back up current content before overwriting
    current = _effective_content(chapter, topic)
    if current:
        _save_history(chapter, topic, current)

    # Write override
    override = _override_path(chapter, topic)
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(body.content, encoding="utf-8")

    return {"status": "saved"}


# ─── Reset ─────────────────────────────────────────────────────────────────


@router.delete("/{chapter}/{topic}")
def reset_topic(chapter: str, topic: str):
    """Reset to default by removing override. Saves current to history first."""
    _topic_path(chapter, topic)

    if chapter == "_root" and topic == "overview":
        override = OVERRIDE_DIR / "overview.md"
        if override.exists():
            _save_overview_history(override.read_text(encoding="utf-8"))
            override.unlink()
        return {"status": "reset"}

    override = _override_path(chapter, topic)
    if override.exists():
        # Save to history before deleting
        _save_history(chapter, topic, override.read_text(encoding="utf-8"))
        override.unlink()
    return {"status": "reset"}


# ─── History ───────────────────────────────────────────────────────────────


@router.get("/{chapter}/{topic}/history/{ts}")
def get_history_version(chapter: str, topic: str, ts: int):
    """Get a specific historical version."""
    _topic_path(chapter, topic)

    if chapter == "_root" and topic == "overview":
        hdir = HISTORY_DIR / "_root"
        hfile = hdir / f"{ts}.md"
        if not hfile.exists():
            raise HTTPException(status_code=404, detail="Version not found")
        return {"content": hfile.read_text(encoding="utf-8"), "ts": ts}

    hfile = _history_dir(chapter, topic) / f"{ts}.md"
    if not hfile.exists():
        raise HTTPException(status_code=404, detail="Version not found")
    return {"content": hfile.read_text(encoding="utf-8"), "ts": ts}


# ─── Overview history helpers ──────────────────────────────────────────────


def _save_overview_history(content: str):
    hdir = HISTORY_DIR / "_root"
    hdir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    (hdir / f"{ts}.md").write_text(content, encoding="utf-8")
    files = sorted(hdir.glob("*.md"), key=lambda f: f.stem, reverse=True)
    for old in files[MAX_HISTORY:]:
        old.unlink()


def _list_overview_history() -> list[dict]:
    hdir = HISTORY_DIR / "_root"
    if not hdir.exists():
        return []
    files = sorted(hdir.glob("*.md"), key=lambda f: f.stem, reverse=True)
    return [{"ts": int(f.stem), "file": f.name} for f in files]
