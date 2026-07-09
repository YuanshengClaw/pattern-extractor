#!/usr/bin/env python3
"""Phase 1: PR grouping and cluster decision.

Reads classified.json (from rv-optkb-tool step3), groups commits by category,
decides if a group has enough PRs to form a pattern, and (optionally) uses LLM
to refine clusters.
"""

import re
from collections import defaultdict
from typing import Any

from .llm_client import LLMClient, LLMError

# Minimum PRs to form a pattern (configurable, default threshold)
MIN_PRS_FOR_PATTERN = 2

# Quantization threshold: if a single category has more than this many PRs,
# trigger LLM secondary clustering to split into finer sub-categories.
MAX_PRS_BEFORE_SUBCLUSTER = 12


def group_by_category(commits: list[dict]) -> dict[str, list[dict]]:
    """Group commits by their 'idea' field (from CSV's Idea column).

    Falls back to 'uncategorized' when idea is empty.
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for c in commits:
        cat = c.get("idea", "").strip() or "uncategorized"
        groups[cat].append(c)
    return dict(groups)


def compute_group_stats(groups: dict[str, list[dict]],
                       min_prs: int = MIN_PRS_FOR_PATTERN) -> list[dict]:
    """Convert groups to sorted list of group info dicts.

    Args:
        groups: Commit dicts grouped by idea category.
        min_prs: Minimum PRs required for pattern_ready. Override via config.

    Returns:
        [
            {
                "category": "Fence/memory barrier",
                "count": 5,
                "commits": [...],
                "pattern_ready": True,   # count >= min_prs
                "needs_subcluster": False # count <= MAX_PRS_BEFORE_SUBCLUSTER
            },
            ...
        ]
    """
    stats = []
    for cat, comms in groups.items():
        stats.append({
            "category": cat,
            "count": len(comms),
            "commits": comms,
            "pattern_ready": len(comms) >= min_prs,
            "needs_subcluster": len(comms) > MAX_PRS_BEFORE_SUBCLUSTER,
        })
    stats.sort(key=lambda s: s["count"], reverse=True)
    return stats


def llm_subcluster(group_info: dict,
                    llm: LLMClient,
                    prompt_template: str) -> list[dict]:
    """Use LLM to split a large category into sub-clusters.

    Each sub-cluster becomes a candidate pattern on its own.

    Args:
        group_info: Output from compute_group_stats for one group.
        llm: LLM client instance.
        prompt_template: System prompt for sub-clustering.

    Returns:
        List of sub-group dicts, same shape as group_info["commits"] but
        with an added "_sub_category" field.
    """
    # Build user content: list commit titles + first line of messages
    lines = []
    for i, c in enumerate(group_info["commits"], 1):
        msg = c.get("message", "")
        title = msg.split("\n")[0].strip() if msg else "(no message)"
        sha = c.get("sha", "?")[:7]
        lines.append(f"[{i}] {sha}: {title}")
    user_content = (
        f"We have {group_info['count']} commits in category "
        f"'{group_info['category']}'. Assign each to a sub-category name.\n\n"
        + "\n".join(lines)
    )

    try:
        result = llm.chat_json(prompt_template, user_content)
    except LLMError:
        # If LLM clustering fails, keep as one group
        for c in group_info["commits"]:
            c["_sub_category"] = group_info["category"]
        return [group_info["commits"]]

    sub_assignments: dict[str, str] = {}
    raw = result.get("assignments", result)
    if isinstance(raw, dict):
        for key, val in raw.items():
            # key could be "[1]" or "1" or "commit_sha"
            sub_assignments[str(key)] = str(val)
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                idx = item.get("index", "")
                sub = item.get("sub_category", "")
                if idx:
                    sub_assignments[str(idx)] = sub

    # Apply sub-category to commits
    sub_groups: dict[str, list[dict]] = {}
    for i, c in enumerate(group_info["commits"], 1):
        sub = sub_assignments.get(str(i)) or sub_assignments.get(f"[{i}]") or group_info["category"]
        c["_sub_category"] = sub
        sub_groups.setdefault(sub, []).append(c)

    # If LLM returned only one cluster, emit a warning
    if len(sub_groups) == 1:
        print(f"  ⚠ LLM sub-cluster returned single group for "
              f"'{group_info['category']}' — keeping as-is")

    return list(sub_groups.values())


def extract_pr_url(commit: dict) -> str:
    """Extract PR URL from a commit dict. Fallback to commit URL."""
    url = commit.get("url", "")
    if url and "pull" in url:
        return url
    # Try to extract PR number from message
    msg = commit.get("message", "")
    m = re.search(r'#(\d{4,})', msg)
    if m:
        owner = commit.get("owner", "")
        repo = commit.get("repo", "")
        if owner and repo:
            return f"https://github.com/{owner}/{repo}/pull/{m.group(1)}"
        return f"https://github.com/openjdk/jdk/pull/{m.group(1)}"
    return url


def extract_pr_number(commit: dict) -> str:
    """Extract PR number like '#21248' from commit message or URL."""
    url = commit.get("url", "")
    # Match /pull/NNNN or /issues/NNNN in URL
    m = re.search(r'/(?:pull|issues)/(\d{4,})', url)
    if m:
        return f"#{m.group(1)}"
    # Also match #NNNN in URL (commit URL fragments)
    m = re.search(r'#(\d{4,})', url)
    if m:
        return f"#{m.group(1)}"
    msg = commit.get("message", "")
    m = re.search(r'#(\d{4,})', msg)
    return m.group(0) if m else commit.get("sha", "?")[:12]


def build_pattern_title(group_info: dict) -> str:
    """Heuristic: derive a pattern title from the category name."""
    cat = group_info["category"]
    # Clean up common suffixes
    title = re.sub(r'\s*instructions?\s*$', '', cat, flags=re.IGNORECASE)
    title = re.sub(r'\s*optimization\s*$', '', title, flags=re.IGNORECASE)
    title = title.strip()
    return title if title else cat
