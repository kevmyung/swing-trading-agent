"""
tools/journal/playbook.py — Investment playbook reader for PortfolioAgent.

Provides progressive disclosure of investment methodology.
The LLM navigates via read_playbook(topic):
  1. read_playbook('entry/momentum') → full chapter content
  2. read_playbook('entry/momentum#Sizing') → specific section (h2 header)
  3. read_playbook('entry/momentum, ref/weekly') → multiple at once

Section-level reads use '#' separator: 'chapter/sub#Section Name'.
Topics are discovered dynamically from the playbook/ directory structure.
Registered as a direct @tool on PortfolioAgent.
"""

from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)

from strands import tool

PLAYBOOK_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "playbook")
OVERRIDE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "playbook_overrides")

# Thread-local storage for allowed chapters (parallel eval support).
_local = threading.local()

# Legacy module-level fallback — used only when _local has no value set.
_allowed_chapters: set[str] | None = None

# Chapters shown in TOC and available per cycle.
CYCLE_CHAPTERS = {
    'eod':      {'entry', 'position'},
    'morning':  {'morning'},
    'intraday': {'intraday', 'position'},
}

# Always readable from any cycle but excluded from TOC.
# Accessed via cross-references in main chapters.
_REFERENCE_CHAPTERS = {'references'}


def set_allowed_chapters(cycle: str | None) -> None:
    """Restrict playbook access to chapters relevant to a cycle."""
    global _allowed_chapters
    if cycle and cycle in CYCLE_CHAPTERS:
        val = CYCLE_CHAPTERS[cycle]
    else:
        val = None
    _allowed_chapters = val
    _local.allowed_chapters = val


def _get_allowed_chapters() -> set[str] | None:
    """Get allowed chapters (thread-local first, then module-level fallback)."""
    return getattr(_local, "allowed_chapters", _allowed_chapters)


def _discover_chapters() -> list[str]:
    """Return top-level chapter names (subdirectories containing .md files)."""
    if not os.path.isdir(PLAYBOOK_DIR):
        return []
    chapters = []
    for name in sorted(os.listdir(PLAYBOOK_DIR)):
        chapter_dir = os.path.join(PLAYBOOK_DIR, name)
        if os.path.isdir(chapter_dir) and any(
            f.endswith(".md") and not f.startswith("_")
            for f in os.listdir(chapter_dir)
        ):
            chapters.append(name)
    return chapters


def _discover_subchapters(chapter: str) -> list[str]:
    """Return sub-chapter names within a chapter directory."""
    chapter_dir = os.path.join(PLAYBOOK_DIR, chapter)
    if not os.path.isdir(chapter_dir):
        return []
    subs = []
    for fname in sorted(os.listdir(chapter_dir)):
        if fname.endswith(".md") and not fname.startswith("_"):
            subs.append(fname[:-3])
    return subs


# Backward compatibility alias used by tests
def _discover_topics() -> list[str]:
    """Return all navigable paths (chapters + chapter/subchapter combos)."""
    topics = []
    for ch in _discover_chapters():
        topics.append(ch)
        for sub in _discover_subchapters(ch):
            topics.append(f"{ch}/{sub}")
    return topics


@tool
def read_playbook(topic: str = "", section: str = "") -> str:
    """Read a chapter of the investment playbook.

    Topic formats:
    - "entry/momentum" → full chapter content
    - "references/adx_signals" → detailed criteria referenced by main chapters
    - "entry" → list available sub-chapters in this chapter

    Main chapters link to references/ topics for specific criteria.
    Navigate progressively: read main chapters first, then follow
    read_playbook('references/...') links in the content for deeper criteria.
    You can call this tool across multiple turns.

    Args:
        topic: Chapter to read (e.g. "entry/momentum", "references/adx_signals").

    Returns:
        The playbook content as markdown text.
    """
    # Ablation: if playbook is disabled, return minimal message
    try:
        from config.settings import get_settings
        if not get_settings().enable_playbook:
            return "(Playbook disabled for this experiment run.)"
    except Exception:
        pass

    if not topic or topic.strip() == "":
        return _read_overview()

    # Support legacy '#' syntax: "chapter/sub#Section"
    if "#" in topic and not section:
        topic, section = topic.rsplit("#", 1)
        topic = topic.strip()
        section = section.strip()

    topics = [t.strip().lower().rstrip("/") for t in topic.split(",") if t.strip()]
    if len(topics) == 1:
        return _read_single(topics[0], section_name=section.strip() if section else None)

    results: list[str] = []
    for t in topics:
        content = _read_single(t)
        results.append(f"--- {t} ---\n{content}")
    return "\n\n".join(results)


def _read_single(target: str, section_name: str | None = None) -> str:
    """Read a single playbook topic, optionally a specific section."""
    chapters = _discover_chapters()
    ac = _get_allowed_chapters()
    allowed = ac | _REFERENCE_CHAPTERS if ac else None
    available = [c for c in chapters if allowed is None or c in allowed]

    # Case 1: chapter name only → list sub-chapters
    if "/" not in target:
        if target not in chapters:
            return (
                f"Unknown topic: '{target}'. "
                f"Available chapters: {', '.join(available)}."
            )
        if allowed and target not in allowed:
            return (
                f"Chapter '{target}' is not relevant to this cycle. "
                f"Available: {', '.join(available)}."
            )
        return _list_chapter_contents(target)

    # Case 2: chapter/subchapter → read specific file
    parts = target.split("/", 1)
    chapter, subpath = parts[0], parts[1]
    if allowed and chapter not in allowed:
        available_display = [c for c in available if c not in _REFERENCE_CHAPTERS]
        return (
            f"Chapter '{chapter}' is not relevant to this cycle. "
            f"Available: {', '.join(available_display)}."
        )

    if chapter not in chapters:
        return (
            f"Unknown chapter: '{chapter}'. "
            f"Available chapters: {', '.join(chapters)}."
        )

    # Resolve the file path (supports N-level: chapter/sub1/sub2)
    # Check override first, then default
    override_path = os.path.normpath(
        os.path.join(OVERRIDE_DIR, chapter, f"{subpath}.md")
    )
    file_path = os.path.normpath(
        os.path.join(PLAYBOOK_DIR, chapter, f"{subpath}.md")
    )
    if not file_path.startswith(os.path.normpath(PLAYBOOK_DIR)):
        return f"Invalid path: {target}"

    if not os.path.exists(file_path):
        available_subs = _discover_subchapters(chapter)
        return (
            f"Unknown sub-chapter: '{subpath}' in chapter '{chapter}'. "
            f"Available: {', '.join(available_subs)}."
        )

    effective_path = override_path if os.path.exists(override_path) else file_path
    with open(effective_path, "r", encoding="utf-8") as f:
        content = f.read()

    # If section requested, extract just that h2 section
    if section_name:
        section = _extract_section(content, section_name)
        if section is None:
            h2s = _list_h2_headers(content)
            return (
                f"Section '{section_name}' not found in '{target}'. "
                f"Available sections: {', '.join(h2s)}."
            )
        return section

    return content


def _list_h2_headers(content: str) -> list[str]:
    """Extract all h2 (##) header titles from markdown content."""
    headers = []
    for line in content.splitlines():
        if line.startswith("## "):
            headers.append(line[3:].strip())
    return headers


def _extract_section(content: str, section_name: str) -> str | None:
    """Extract a specific h2 section from markdown content.

    Matches case-insensitively and supports partial prefix matching.
    Returns the h2 header line plus all content until the next h2 or h1.
    """
    lines = content.splitlines()
    section_lower = section_name.lower()
    start = None

    for i, line in enumerate(lines):
        if line.startswith("## "):
            header_text = line[3:].strip().lower()
            if header_text == section_lower or header_text.startswith(section_lower):
                start = i
                break

    if start is None:
        return None

    # Collect lines until next h1/h2 or end of file
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## ") or lines[i].startswith("# "):
            end = i
            break

    return "\n".join(lines[start:end]).strip()


def build_toc(cycle: str | None = None) -> str:
    """Build a table of contents from playbook markdown headers.

    Parses h1/h2 headers (depth 3: category/subchapter/section). Filters by cycle if specified.
    Returns a compact index suitable for prompt injection.
    """
    allowed = CYCLE_CHAPTERS.get(cycle) if cycle else None
    toc_lines = []

    for ch in _discover_chapters():
        if ch in _REFERENCE_CHAPTERS:
            continue
        if allowed and ch not in allowed:
            continue
        for sub in _discover_subchapters(ch):
            topic = f"{ch}/{sub}"
            default_path = os.path.join(PLAYBOOK_DIR, ch, f"{sub}.md")
            override_path = os.path.join(OVERRIDE_DIR, ch, f"{sub}.md")
            file_path = override_path if os.path.exists(override_path) else default_path
            if not os.path.exists(file_path):
                continue
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

            headers = []
            for line in content.splitlines():
                if line.startswith("#"):
                    hashes = line.split()[0]
                    level = len(hashes)
                    text = line[level:].strip()
                    headers.append((level, text))

            if not headers:
                continue

            h1 = headers[0][1] if headers[0][0] == 1 else sub
            h2s = [f"  {text}" for level, text in headers if level == 2]

            toc_lines.append(f"{topic}: {h1}")
            toc_lines.extend(h2s)
            toc_lines.append("")

    # Append references — show names so PM knows what's available
    for ch in sorted(_REFERENCE_CHAPTERS):
        subs = _discover_subchapters(ch)
        if not subs:
            continue
        toc_lines.append(f"{ch}/")
        for sub in subs:
            default_path = os.path.join(PLAYBOOK_DIR, ch, f"{sub}.md")
            override_path = os.path.join(OVERRIDE_DIR, ch, f"{sub}.md")
            file_path = override_path if os.path.exists(override_path) else default_path
            if not os.path.exists(file_path):
                continue
            with open(file_path, "r", encoding="utf-8") as f:
                first_line = f.readline().strip()
            title = first_line.lstrip("# ").strip() if first_line.startswith("#") else sub
            toc_lines.append(f"  {ch}/{sub}: {title}")
        toc_lines.append("")

    return "\n".join(toc_lines).strip()


def _read_overview() -> str:
    """Read overview.md and append a dynamically generated chapter index."""
    override_path = os.path.normpath(os.path.join(OVERRIDE_DIR, "overview.md"))
    overview_path = os.path.normpath(os.path.join(PLAYBOOK_DIR, "overview.md"))
    effective_path = override_path if os.path.exists(override_path) else overview_path
    if not os.path.exists(effective_path):
        return "Playbook overview not found."

    with open(effective_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Append dynamic chapter list (filtered by current cycle)
    chapters = _discover_chapters()
    available = [c for c in chapters
                 if c not in _REFERENCE_CHAPTERS
                 and (_get_allowed_chapters() is None or c in _get_allowed_chapters())]
    if available:
        content += "\n\n## Chapters\n"
        for ch in available:
            subs = _discover_subchapters(ch)
            sub_list = ", ".join(subs)
            content += f"- **{ch}**: {sub_list}\n"

    return content


def _list_chapter_contents(chapter: str) -> str:
    """List sub-chapters in a chapter with their first-line descriptions."""
    subs = _discover_subchapters(chapter)
    if not subs:
        return f"Chapter '{chapter}' has no sub-chapters."

    lines = [f"Sub-chapters in '{chapter}':"]
    lines.append(f"Call read_playbook('{chapter}/<name>') to read.\n")
    for sub in subs:
        desc = _extract_sub_description(chapter, sub)
        lines.append(f"- **{sub}**{f' — {desc}' if desc else ''}")
    return "\n".join(lines)


def _extract_description(chapter: str) -> str:
    """Extract a one-line description from the chapter's first .md file heading."""
    subs = _discover_subchapters(chapter)
    if not subs:
        return ""
    # Use the first sub-chapter's heading as a proxy
    return _extract_sub_description(chapter, subs[0]) if len(subs) == 1 else ""


def _extract_sub_description(chapter: str, subchapter: str) -> str:
    """Extract a one-line description from a sub-chapter's first heading."""
    file_path = os.path.join(PLAYBOOK_DIR, chapter, f"{subchapter}.md")
    if not os.path.exists(file_path):
        return ""
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("# "):
                return line[2:].strip()
            break
    return ""
