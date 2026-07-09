#!/usr/bin/env python3
"""Diff parser — extract structured Before/After code blocks from commit diffs.

Given a unified diff string, this module:
1. Splits by file (diff --git markers)
2. Extracts the removed (-) and added (+) lines per hunk
3. Organizes them into (file_path, before_lines, after_lines) tuples
4. Provides a text representation suitable for LLM prompt context
"""

import re
from typing import Optional

# Match: diff --git a/<path> b/<path>
DIFF_HEADER_RE = re.compile(r'^diff --git a/(.+?) b/(.+?)$')
# Match: --- a/<path>  or  /dev/null
ORIG_FILE_RE = re.compile(r'^--- a/(.+)$')
# Match: +++ b/<path>
NEW_FILE_RE = re.compile(r'^\+\+\+ b/(.+)$')
# Match hunk header: @@ -start,count +start,count @@
HUNK_HEADER_RE = re.compile(r'^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@')


class FileDiff:
    """Structured representation of one file's diff."""

    def __init__(self, file_path: str):
        self.file_path = file_path
        self.hunks: list['Hunk'] = []

    @property
    def has_changes(self) -> bool:
        return len(self.hunks) > 0


class Hunk:
    """One hunk within a file diff."""

    def __init__(self, old_start: int, new_start: int):
        self.old_start = old_start
        self.new_start = new_start
        self.before_lines: list[str] = []
        self.after_lines: list[str] = []
        # Context lines (unchanged, shown for orientation)
        self.context_lines: list[str] = []


def parse_diff(diff_text: str) -> list[FileDiff]:
    """Parse a unified diff string into a list of FileDiff objects.

    Args:
        diff_text: Standard git unified diff output.

    Returns:
        List of FileDiff, one per file.
    """
    if not diff_text or not diff_text.strip():
        return []

    files: list[FileDiff] = []
    current_file: Optional[FileDiff] = None
    current_hunk: Optional[Hunk] = None
    in_diff = False

    for line in diff_text.split('\n'):
        # File header
        m = DIFF_HEADER_RE.match(line)
        if m:
            current_file = FileDiff(m.group(2))  # use b/ path
            files.append(current_file)
            current_hunk = None
            in_diff = False
            continue

        # --- a/file (useful for new/deleted files)
        m = ORIG_FILE_RE.match(line)
        if m and current_file and not current_file.file_path:
            current_file.file_path = m.group(1)

        # +++ b/file
        m = NEW_FILE_RE.match(line)
        if m and current_file:
            current_file.file_path = m.group(1)

        # Hunk header
        m = HUNK_HEADER_RE.match(line)
        if m and current_file:
            current_hunk = Hunk(int(m.group(1)), int(m.group(2)))
            current_file.hunks.append(current_hunk)
            in_diff = True
            continue

        if not in_diff or not current_hunk:
            continue

        # Content lines within a hunk
        if line.startswith('+') and not line.startswith('+++'):
            current_hunk.after_lines.append(line[1:])
        elif line.startswith('-') and not line.startswith('---'):
            current_hunk.before_lines.append(line[1:])
        elif line.startswith(' '):
            current_hunk.context_lines.append(line[1:])
        # else: binary marker or other, skip

    return files


def extract_before_after_pairs(file_diffs: list[FileDiff]) -> list[dict]:
    """Convert parsed diffs to simplified Before/After pairs.

    Returns:
        [
            {
                "file": "src/hotspot/cpu/riscv/safepoint_riscv.cpp",
                "before": ["...code lines..."],
                "after": ["...code lines..."],
                "context": ["...unchanged surrounding lines..."],
            },
            ...
        ]
    """
    pairs = []
    for fd in file_diffs:
        all_before: list[str] = []
        all_after: list[str] = []
        all_context: list[str] = []
        for hunk in fd.hunks:
            all_before.extend(hunk.before_lines)
            all_after.extend(hunk.after_lines)
            all_context.extend(hunk.context_lines)
        if all_before or all_after:
            pairs.append({
                "file": fd.file_path,
                "before": all_before,
                "after": all_after,
                "context": all_context,
            })
    return pairs


def format_pairs_for_prompt(pairs: list[dict], max_files: int = 5) -> str:
    """Format Before/After pairs as readable text for an LLM prompt.

    Truncates at max_files to avoid exceeding token limits.
    """
    parts = []
    for i, p in enumerate(pairs[:max_files]):
        file_path = p["file"]
        before = p.get("before", [])
        after = p.get("after", [])
        context = p.get("context", [])

        block = f"=== File: {file_path} ===\n"
        if context:
            # Show first few context lines for orientation
            ctx_sample = context[:3]
            block += "// Context:\n"
            for cl in ctx_sample:
                block += f"// {cl}\n"
            if len(context) > 3:
                block += f"// ... ({len(context)} context lines total)\n"

        if before:
            block += "// Before:\n"
            for bl in before:
                block += f"- {bl}\n"

        if after:
            block += "// After:\n"
            for al in after:
                block += f"+ {al}\n"

        parts.append(block)

    if len(pairs) > max_files:
        parts.append(f"... ({len(pairs) - max_files} more files omitted)")

    return "\n".join(parts)


def extract_code_blocks_for_pattern(commit: dict, max_files: int = 5) -> dict:
    """One-shot extraction: commit dict → structured code block info for §6.

    Returns:
        {
            "has_code_changes": True/False,
            "file_count": N,
            "pairs": [...],
            "prompt_text": "formatted text for LLM prompt"
        }
    """
    diff = commit.get("diff", "")
    if not diff.strip():
        return {"has_code_changes": False, "file_count": 0, "pairs": [], "prompt_text": ""}

    file_diffs = parse_diff(diff)
    pairs = extract_before_after_pairs(file_diffs)
    prompt_text = format_pairs_for_prompt(pairs, max_files)

    return {
        "has_code_changes": len(pairs) > 0,
        "file_count": len(pairs),
        "pairs": pairs[:max_files],
        "prompt_text": prompt_text,
    }


def summarize_diffs_for_group(commits: list[dict],
                             max_files_per_commit: int = 50,
                             max_commits: int = 50) -> str:
    """Summarize diffs across multiple commits for LLM pattern generation.

    Produces a compact text block that shows the key code changes across
    all related commits.
    """
    parts = []
    for i, c in enumerate(commits[:max_commits]):
        sha = c.get("sha", "?")[:7]
        msg = c.get("message", "").split("\n")[0].strip()[:80]
        diff_info = extract_code_blocks_for_pattern(c, max_files=max_files_per_commit)
        if not diff_info["has_code_changes"]:
            continue

        section = f"--- Commit {sha}: {msg} ---\n"
        section += diff_info["prompt_text"]
        parts.append(section)

    return "\n\n".join(parts)
