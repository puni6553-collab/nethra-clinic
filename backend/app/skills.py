"""Iteration 6, R4/R5 — the skills loader (about 40 lines of real logic).

A *skill* is a folder under `<project>/skills/` holding a `SKILL.md` with YAML
front matter (`name`, `description`) and, in some skills, one extra file:

    skills/cataract-surgery-care/SKILL.md
    skills/cataract-surgery-care/post-op-guide.md

The loader reads only the front matter at startup, so the patient assistant's
system prompt carries every skill's *name and description* and nothing else —
the body only ever reaches the model when it decides a task needs one and
calls `load_skill(name)`.

Two rules the tools never break (handout R4):

* an unknown skill is refused, and
* `read_skill_file` reads only files *inside that skill's folder* — every path
  is resolved and checked against the folder, so `../`, absolute paths and
  anything else outside it never come back.

Skill files are clinic-approved content: answers that use one cite it. The
clinic's safety rules are not in here, and never were — a skill only runs if
the LLM chooses to load it, so it can never be what keeps a patient safe.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import frontmatter  # python-frontmatter: YAML front matter + body

from . import config

# name -> {"dir": Path, "description": str, "path": Path}
_CATALOG: dict[str, dict[str, Any]] | None = None


def _skills_dir() -> Path:
    return Path(config.SKILLS_DIR)


def _scan() -> dict[str, dict[str, Any]]:
    """Front matter of every skill folder. Ignores folders without SKILL.md."""
    found: dict[str, dict[str, Any]] = {}
    root = _skills_dir()
    if not root.is_dir():
        return found
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        page = folder / "SKILL.md"
        if not page.is_file():
            continue
        try:
            post = frontmatter.load(str(page))
        except Exception as exc:  # noqa: BLE001 — one broken skill never hides the rest
            print(f"[skills] cannot read {page}: {exc}", flush=True)
            continue
        name = str(post.metadata.get("name") or folder.name).strip() or folder.name
        found[name] = {
            "dir": folder,
            "path": page,
            "description": str(post.metadata.get("description") or "").strip(),
        }
    return found


def catalog(refresh: bool = False) -> dict[str, dict[str, Any]]:
    """Every skill's front matter, read once and cached."""
    global _CATALOG
    if _CATALOG is None or refresh:
        _CATALOG = _scan()
    return _CATALOG


def names() -> list[str]:
    return list(catalog())


def prompt_block() -> str:
    """The "Available skills" list for the system prompt: name + description."""
    rows = catalog()
    if not rows:
        return "Available skills: none."
    lines = [f"- {name}: {info['description'] or 'clinic know-how'}"
             for name, info in rows.items()]
    return "Available skills (load one only when the task needs it):\n" + "\n".join(lines)


def _entry(name: str) -> dict[str, Any] | None:
    return catalog().get((name or "").strip())


def load(name: str) -> tuple[str | None, str]:
    """The body of one SKILL.md (front matter stripped).

    Returns (body, "") on success, or (None, why) for an unknown skill.
    """
    info = _entry(name)
    if info is None:
        return None, f"There is no skill called {name!r}."
    try:
        post = frontmatter.load(str(info["path"]))
    except Exception as exc:  # noqa: BLE001
        return None, f"Skill {name!r} could not be read: {exc}"
    return str(post.content or "").strip(), ""


def read_file(name: str, file: str) -> tuple[str | None, str]:
    """One file from a skill's folder, with the path contained inside it.

    Returns (text, "") on success, or (None, why) for an unknown skill, an
    unknown file, or any path that escapes the skill's folder.
    """
    info = _entry(name)
    if info is None:
        return None, f"There is no skill called {name!r}."

    folder: Path = info["dir"]
    raw = (file or "").strip()
    if not raw:
        return None, "Give a file name from the skill's folder."
    if raw.startswith(("/", "\\")) or (len(raw) > 1 and raw[1] == ":"):
        return None, "That file is outside this skill's folder."

    try:
        target = (folder / raw).resolve()
    except OSError:
        return None, "That file is outside this skill's folder."
    root = folder.resolve()
    if target != root and root not in target.parents:
        # "../", a symlink out, or any other way of stepping outside.
        return None, "That file is outside this skill's folder."
    if not target.is_file():
        return None, f"{name} has no file called {os.path.basename(raw)}."
    try:
        return target.read_text(encoding="utf-8"), ""
    except OSError as exc:
        return None, f"Skill file could not be read: {exc}"
