"""
config — Application configuration package.

Exports the Settings class and the get_settings() factory function.
All environment variables and tuneable parameters live here.
"""

from .settings import Settings, get_settings

__all__ = ["Settings", "get_settings"]
