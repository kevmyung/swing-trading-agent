"""
tools/research/research_library.py — Research paper library for the PortfolioAgent.

ResearchAnalystAgent writes each cycle's output as a structured .md file.
PortfolioAgent reads from that library via these tools — pulling only what it
needs rather than having research injected into every prompt.

File naming: {research_dir}/{cycle_type_lower}_{YYYY-MM-DD}.md
  e.g.  state/research/morning_research_2026-03-08.md
        state/research/eod_research_2026-03-08.md

Markdown structure:
  ---
  id: morning_research_2026-03-08
  cycle: MORNING_RESEARCH
  date: 2026-03-08
  generated_at: 2026-03-08T08:45:23Z
  tickers: AAPL, NVDA, MSFT
  abstract: One-line summary of key findings.
  sections: Macro Context, Sector Context, AAPL, NVDA, MSFT
  ---

  # MORNING_RESEARCH — 2026-03-08 ...

  ## Macro Context
  ...
  ## AAPL
  ...
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from pathlib import Path

from tools._compat import tool

logger = logging.getLogger(__name__)


def _get_research_dir() -> Path:
    from config.settings import get_settings
    return Path(get_settings().research_dir)


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse simple key: value frontmatter delimited by ---. Returns (meta, body)."""
    if not content.startswith('---'):
        return {}, content
    end = content.find('\n---\n', 3)
    if end == -1:
        return {}, content
    fm_text = content[3:end].strip()
    body = content[end + 5:]
    meta: dict[str, str] = {}
    for line in fm_text.splitlines():
        if ':' in line:
            key, _, val = line.partition(':')
            meta[key.strip()] = val.strip()
    return meta, body


def _extract_sections(body: str, sections: list[str]) -> str:
    """Return only the requested ## sections from a markdown body."""
    # Prepend newline so the split regex catches a leading ## too
    parts = re.split(r'\n(?=## )', '\n' + body)
    chosen = []
    for part in parts:
        first_line = part.strip().split('\n')[0].lstrip('#').strip()
        if any(s.lower() == first_line.lower() for s in sections):
            chosen.append(part.strip())
    if not chosen:
        return f"Sections not found: {sections}. Use list_research_papers() to see available section names."
    return '\n\n'.join(chosen)


@tool
def list_research_papers(days_back: int = 3) -> dict:
    """List research papers written in the last N days.

    Use this to discover which papers and sections are available before
    calling read_research_paper(). Returns metadata only — no full content.

    Args:
        days_back: How many calendar days back to search (default: 3).

    Returns:
        Dict with keys:
          - ``papers`` (list): Each entry contains:
              - ``id`` (str): Paper ID to pass to read_research_paper()
              - ``cycle`` (str): MORNING_RESEARCH or EOD_RESEARCH
              - ``date`` (str): YYYY-MM-DD
              - ``generated_at`` (str): ISO timestamp
              - ``tickers`` (list[str]): Tickers covered
              - ``abstract`` (str): One-line summary
              - ``sections`` (list[str]): Available section names
          - ``count`` (int): Number of papers found
    """
    research_dir = _get_research_dir()
    if not research_dir.exists():
        return {'papers': [], 'count': 0}

    cutoff = date.today() - timedelta(days=days_back)
    papers = []

    for path in sorted(research_dir.glob('*.md'), reverse=True):
        try:
            content = path.read_text(encoding='utf-8')
            meta, _ = _parse_frontmatter(content)
            if not meta:
                continue
            paper_date_str = meta.get('date', '')
            if paper_date_str:
                from datetime import datetime
                paper_date = datetime.strptime(paper_date_str, '%Y-%m-%d').date()
                if paper_date < cutoff:
                    continue
            tickers_str = meta.get('tickers', '')
            tickers = [t.strip() for t in tickers_str.split(',') if t.strip()]
            sections_str = meta.get('sections', '')
            sections = [s.strip() for s in sections_str.split(',') if s.strip()]
            papers.append({
                'id': meta.get('id', path.stem),
                'cycle': meta.get('cycle', ''),
                'date': meta.get('date', ''),
                'generated_at': meta.get('generated_at', ''),
                'tickers': tickers,
                'abstract': meta.get('abstract', ''),
                'sections': sections,
            })
        except Exception as exc:
            logger.debug("Could not parse research paper %s: %s", path, exc)

    return {'papers': papers, 'count': len(papers)}


@tool
def read_research_paper(paper_id: str, sections: list[str] | None = None) -> dict:
    """Read a research paper, optionally filtered to specific sections.

    Call list_research_papers() first to discover paper IDs and section names.
    Reading only the sections you need (e.g. ['AAPL', 'Macro Context']) is
    more efficient than reading the full paper.

    Args:
        paper_id: Paper ID from list_research_papers(), e.g.
                  ``'morning_research_2026-03-08'`` or ``'eod_research_2026-03-08'``.
        sections: Section names to read, e.g. ``['Macro Context', 'AAPL', 'NVDA']``.
                  If None or omitted, returns the full paper.

    Returns:
        Dict with keys:
          - ``paper_id`` (str)
          - ``cycle`` (str): MORNING_RESEARCH or EOD_RESEARCH
          - ``generated_at`` (str): ISO timestamp of when research was written
          - ``content`` (str): Markdown content (full or filtered sections)
          - ``error`` (str | None): Present if paper not found or read failed
    """
    research_dir = _get_research_dir()
    file_path = research_dir / f"{paper_id}.md"

    if not file_path.exists():
        return {
            'paper_id': paper_id,
            'cycle': '',
            'generated_at': '',
            'content': '',
            'error': (
                f"Paper '{paper_id}' not found. "
                "Call list_research_papers() to see available papers."
            ),
        }

    try:
        content = file_path.read_text(encoding='utf-8')
        meta, body = _parse_frontmatter(content)
        filtered = _extract_sections(body, sections) if sections else body.strip()
        return {
            'paper_id': paper_id,
            'cycle': meta.get('cycle', ''),
            'generated_at': meta.get('generated_at', ''),
            'content': filtered,
            'error': None,
        }
    except Exception as exc:
        logger.error("Failed to read research paper %s: %s", paper_id, exc)
        return {
            'paper_id': paper_id,
            'cycle': '',
            'generated_at': '',
            'content': '',
            'error': str(exc),
        }
