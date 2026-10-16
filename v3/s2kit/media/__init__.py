"""
Framework-free media decoding and rendering.

Everything the viewers display is produced here: PNG bytes and a metadata
dict, nothing else. That keeps the Flet layer thin (it only ever assigns bytes
to `ft.Image.src`), makes previews testable from a plain interpreter, and lets
the same code back a headless thumbnailer later.
"""

from s2kit.media.probe import MediaKind, describe, kind_for

__all__ = ["MediaKind", "describe", "kind_for"]
