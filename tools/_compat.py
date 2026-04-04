"""
tools/_compat.py — Portable @tool decorator import.

Re-exports ``strands.tool`` for consistent import across tool modules.
"""

from __future__ import annotations

from strands import tool

__all__ = ["tool"]
