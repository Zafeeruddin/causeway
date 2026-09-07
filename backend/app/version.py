"""The single place the version is written down.

Everything else reads it from here: ``pyproject.toml``, the frontend's
``package.json``, ``/api/health``, and the tag on a built image. A version that
lives in four files is a version that disagrees with itself, and the question it
exists to answer -- *which build is on that machine* -- is asked precisely when
nobody can afford to guess.

Semantic versioning, and deliberately still 0.x: the database schema has no
migrations yet (see ROADMAP.md entry 10), so a release can still require a
rebuild rather than an upgrade. 1.0.0 is the version that promises otherwise.
"""

from __future__ import annotations

__version__ = "0.3.0"
