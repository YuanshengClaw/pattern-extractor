#!/usr/bin/env python3
"""Phase 5: Pattern output — write .md files and update trigger index.

Handles:
- Writing pattern .md to target directory (overwrite or append-incremental)
- Updating existing_patterns/pattern_index.json mapping
- Updating triggers/from-source.md signal → pattern cross-ref
"""

import json
import os
import re
from typing import Any
from datetime import datetime, timezone

PATTERN_INDEX_FILENAME = "pattern_index.json"
TRIGGER_FILENAME = "from-source.md"


def ensure_output_dir(path: str) -> str:
    """Ensure output directory exists, return path."""
    os.makedirs(path, exist_ok=True)
    return path


def write_pattern_md(pattern_text: str,
                     filename: str,
                     output_dir: str,
                     review_first: bool = True) -> str:
    """Write pattern .md file.

    Args:
        pattern_text: Full markdown content of the pattern.
        filename: e.g., 'fence-memory-barrier-reduction.md'.
        output_dir: Target directory for .md files.
        review_first: If True, write to patches/review/ for manual review
                      instead of directly to output_dir.

    Returns:
        Absolute path to the written file.
    """
    if review_first:
        target_dir = ensure_output_dir(os.path.join(output_dir, "..", "patches", "review"))
    else:
        target_dir = ensure_output_dir(output_dir)

    filepath = os.path.join(target_dir, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(pattern_text)
    return filepath


def load_pattern_index(index_dir: str) -> dict:
    """Load pattern_index.json, return empty dict if not found.

    Schema:
    {
        "version": "1.0",
        "patterns": {
            "<filename>": {
                "title_en": "...",
                "title_cn": "...",
                "category": "...",
                "pr_ids": ["#21248", "#24035", ...],
                "pr_urls": ["https://...", ...],
                "sub_methods": ["Safepoint Polling fence 消除", ...],
                "created": "2026-01-15T...",
                "updated": "2026-01-15T..."
            },
            ...
        }
    }
    """
    path = os.path.join(index_dir, PATTERN_INDEX_FILENAME)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"version": "1.0", "patterns": {}}


def save_pattern_index(index: dict, index_dir: str):
    """Write pattern_index.json."""
    os.makedirs(index_dir, exist_ok=True)
    path = os.path.join(index_dir, PATTERN_INDEX_FILENAME)
    index["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)


def update_pattern_index(index: dict,
                         filename: str,
                         title_en: str,
                         title_cn: str,
                         category: str,
                         pr_ids: list[str],
                         pr_urls: list[str],
                         sub_methods: list[str] | None = None):
    """Add or update an entry in the pattern index.

    If the pattern already exists (by filename), merges new PRs into it.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    patterns = index.setdefault("patterns", {})

    if filename in patterns:
        # Update existing
        entry = patterns[filename]
        entry["title_en"] = title_en
        entry["title_cn"] = title_cn
        entry["category"] = category
        entry["updated"] = now
        # Merge PRs
        existing_prs = set(entry.get("pr_ids", []))
        for pid in pr_ids:
            if pid not in existing_prs:
                entry["pr_ids"].append(pid)
                existing_prs.add(pid)
        # Merge URLs
        existing_urls = set(entry.get("pr_urls", []))
        for purl in pr_urls:
            if purl not in existing_urls:
                entry["pr_urls"].append(purl)
                existing_urls.add(purl)
        # Merge sub_methods
        if sub_methods:
            existing_methods = set(entry.get("sub_methods", []))
            for sm in sub_methods:
                if sm not in existing_methods:
                    entry["sub_methods"].append(sm)
    else:
        # Create new entry
        patterns[filename] = {
            "title_en": title_en,
            "title_cn": title_cn,
            "category": category,
            "pr_ids": list(pr_ids),
            "pr_urls": list(pr_urls),
            "sub_methods": list(sub_methods) if sub_methods else [],
            "created": now,
            "updated": now,
        }


def extract_sub_methods_from_fix(pattern_text: str) -> list[str]:
    """Parse §6 The fix section for sub-method titles (### N. <title>)."""
    fix_match = re.search(r'## The fix.*?\n(.*?)(?=\n## )', pattern_text, re.DOTALL)
    if not fix_match:
        return []

    fix_section = fix_match.group(1)
    # Find ### N. <title> patterns
    methods = re.findall(r'^###\s+\d+\.\s+(.+)$', fix_section, re.MULTILINE)
    return [m.strip() for m in methods]


def update_trigger_table(
    pattern_text: str,
    filename: str,
    triggers_dir: str,
):
    """Update or create triggers/from-source.md with signal → pattern mapping.

    Appends new signals (from §4) to the trigger matching table.
    """
    os.makedirs(triggers_dir, exist_ok=True)
    trigger_path = os.path.join(triggers_dir, TRIGGER_FILENAME)

    # Extract signals from §4
    when_match = re.search(r'## When to apply.*?\n(.*?)(?=\n## Why)', pattern_text, re.DOTALL)
    if not when_match:
        return

    signals = re.findall(r'^- `([^`]+)`', when_match.group(1))

    # Build new table rows
    title_match = re.search(r'^# Pattern:\s+(.+)$', pattern_text, re.MULTILINE)
    pattern_title = title_match.group(1).strip() if title_match else filename

    new_rows = []
    for sig in signals:
        new_rows.append(f"| `{sig}` | {pattern_title} | [{filename}]({filename}) |")

    if not new_rows:
        return

    # Read or create trigger file
    if os.path.exists(trigger_path):
        with open(trigger_path, "r", encoding="utf-8") as f:
            existing = f.read()
    else:
        existing = (
            "# Signal → Pattern Matching Table\n\n"
            "| Signal (from §4) | Pattern | Document |\n"
            "|---|---|---|\n"
        )

    # Check for duplicate rows
    for row in new_rows:
        if row not in existing:
            existing += row + "\n"

    with open(trigger_path, "w", encoding="utf-8") as f:
        f.write(existing)
