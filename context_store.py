"""Persistent candidate context (resume, JD, projects) for AI prompts."""

import json
import os
import threading

import config
from logutil import log

CONTEXT_FILE = os.path.join(config._BASE_DIR, "context.json")

MAX_RESUME = 4000
MAX_JD = 3000
MAX_PROJECTS = 4000

_lock = threading.Lock()
_cache = {"resume": "", "jd": "", "projects": ""}


def load() -> dict:
    """Read context from disk; return empty defaults if missing (never raises)."""
    empty = {"resume": "", "jd": "", "projects": ""}
    try:
        if not os.path.exists(CONTEXT_FILE):
            return dict(empty)
        with open(CONTEXT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "resume": data.get("resume") or "",
            "jd": data.get("jd") or "",
            "projects": data.get("projects") or "",
        }
    except Exception:
        return dict(empty)


def _write_disk(data: dict) -> None:
    config.ensure_dirs()
    with open(CONTEXT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def save(resume=None, jd=None, projects=None) -> dict:
    """Partial update; only overwrite fields that are not None."""
    with _lock:
        merged = dict(_cache)
        if resume is not None:
            if len(resume) > MAX_RESUME:
                log(
                    f"context_store: resume truncated from {len(resume)} "
                    f"to {MAX_RESUME} chars"
                )
                resume = resume[:MAX_RESUME]
            merged["resume"] = resume
        if jd is not None:
            if len(jd) > MAX_JD:
                log(
                    f"context_store: jd truncated from {len(jd)} to {MAX_JD} chars"
                )
                jd = jd[:MAX_JD]
            merged["jd"] = jd
        if projects is not None:
            if len(projects) > MAX_PROJECTS:
                log(
                    f"context_store: projects truncated from {len(projects)} "
                    f"to {MAX_PROJECTS} chars"
                )
                projects = projects[:MAX_PROJECTS]
            merged["projects"] = projects
        _cache.clear()
        _cache.update(merged)
        _write_disk(merged)
        return dict(merged)


def get_context() -> dict:
    with _lock:
        return dict(_cache)


def build_prompt_block() -> str:
    ctx = get_context()
    resume = (ctx.get("resume") or "").strip()
    jd = (ctx.get("jd") or "").strip()
    projects = (ctx.get("projects") or "").strip()
    if not resume and not jd and not projects:
        return ""
    return (
        "=== CANDIDATE CONTEXT ===\n"
        "RESUME:\n"
        f"{resume or '(not provided)'}\n\n"
        'KEY PROJECTS (use these for "tell me about a project" / technical '
        'deep-dive / "walk me through your code" questions):\n'
        f"{projects or '(not provided)'}\n\n"
        "TARGET ROLE / JOB DESCRIPTION (match terminology, seniority, and "
        "priorities to this role):\n"
        f"{jd or '(not provided)'}\n"
        "=== END CONTEXT ===\n\n"
        "Ground behavioral and project-related answers in the resume/projects "
        "above wherever relevant. Use the job description to choose which "
        "skills and angle to emphasize. Do NOT invent experience, companies, "
        "or projects that are not present in the context above — if asked about "
        "something not covered, answer generically and naturally rather than "
        "fabricating specifics."
    )


with _lock:
    _cache.update(load())
