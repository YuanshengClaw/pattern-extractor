#!/usr/bin/env python3
"""Phase 3: Multi-phase LLM pattern generation.

Takes a group of classified PRs and generates each section of the 9-section
pattern template using focused LLM prompts.
"""

import json
import os
import re
from typing import Any

from .llm_client import LLMClient, LLMError
from .pr_grouping import (
    extract_pr_number,
    extract_pr_url,
    group_by_category,
    compute_group_stats,
)
from .diff_parser import summarize_diffs_for_group


def _load_prompt(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def _language_instruction(lang: str) -> str:
    """Return a language instruction snippet appended to every system prompt.

    Supported: "zh" (Chinese), "en" (English, default if empty/unknown).
    """
    if lang == "zh":
        return (
            "\n\n## 语言要求\n"
            "使用中文撰写所有内容。专业术语（指令名、工具名称、寄存器名、项目特定 API 名）保留英文原文，"
            "但解释说明、分析、描述全部使用中文。标题采用「英文 / 中文」双语格式。"
        )
    return ""


def _section_file_name(idea: str) -> str:
    """Convert Idea (or category) to filename.

    'Branch Avoidance via Specialized Arithmetic Instructions'
    → 'branch_avoidance_via_specialized_arithmetic_instructions.md'
    """
    name = re.sub(r'[^a-zA-Z0-9 _/-]', '', idea)
    name = name.strip().lower().replace(' ', '_').replace('-', '_').replace('/', '_')
    name = re.sub(r'_+', '_', name)
    name = name.strip('_')
    return name


def generate_pattern_sections(
    group_info: dict,
    llm: LLMClient,
    prompt_dir: str,
    project_name: str = "",
    logger: Any = None,
    max_context_commits: int = 50,
    max_files_per_commit: int = 50,
    language: str = "zh",
    license_text: str = '<!-- (C) 2026 Intel Corporation, MIT license -->',
) -> dict:
    """Run all LLM phases to generate a complete pattern document.

    Args:
        group_info: Group dict from compute_group_stats.
        llm: LLM client.
        prompt_dir: Directory containing prompt templates (*.txt).
        logger: Optional StepLogger.
        max_context_commits: Max commits to include in LLM context (configurable
                             via pipeline.max_prs_for_llm_context).

    Returns:
        {
            "title_en": "...",
            "title_cn": "...",
            "filename": "pattern-name.md",
            "license": "<!-- (C) ... -->",
            "see_also": "",  # or full line
            "when_text": "...",
            "why_text": "...",
            "fix_text": "...",
            "verify_text": "...",
            "present_text": "...",
            "related_table": "...",
            "raw_sections": { ... },  # per-phase raw output
        }
    """
    commits = group_info["commits"]
    category = group_info["category"]
    if logger:
        logger.log(f"Generating pattern for '{category}' ({len(commits)} commits)")

    lang_inst = _language_instruction(language)

    # Phase A: Determine title and structure (no code yet)
    title_info = _phase_title(category, commits, llm, prompt_dir, logger, lang_inst=lang_inst)

    # Phase B: Generate §4 When to apply + profile signals + typical table
    when_text = _phase_when(title_info, commits, llm, prompt_dir, logger,
                            max_context=max_context_commits,
                            max_files_per_commit=max_files_per_commit,
                            lang_inst=lang_inst)

    # Phase C: Generate §5 Why this is slow
    why_text = _phase_why(title_info, commits, llm, prompt_dir, logger,
                          max_context=max_context_commits, lang_inst=lang_inst)

    # Phase D: Generate §6 The fix with code blocks
    fix_text = _phase_fix(title_info, commits, llm, prompt_dir, logger,
                          max_context=max_context_commits,
                          max_files_per_commit=max_files_per_commit,
                          lang_inst=lang_inst)

    # Phase E: Generate §7 Verification
    verify_text = _phase_verify(title_info, commits, llm, prompt_dir, logger,
                                max_context=max_context_commits, lang_inst=lang_inst)

    # Phase F: Generate §8 Presenting + §9 Related PRs
    present_text = _phase_present(title_info, commits, llm, prompt_dir, logger,
                                  max_context=max_context_commits, lang_inst=lang_inst)
    related_table = _phase_related(title_info, commits, llm, prompt_dir, project_name, logger)

    # Compile
    pattern = {
        "title_en": title_info.get("title_en", category),
        "title_cn": title_info.get("title_cn", ""),
        "filename": _section_file_name(category),
        "license": license_text,
        "see_also": title_info.get("see_also", ""),
        "when_text": when_text,
        "why_text": why_text,
        "fix_text": fix_text,
        "verify_text": verify_text,
        "present_text": present_text,
        "related_table": related_table,
        "raw_sections": {
            "title": title_info,
            "when": when_text[:200] if when_text else "",
            "why": why_text[:200] if why_text else "",
            "fix": fix_text[:200] if fix_text else "",
        },
    }
    return pattern


def _phase_title(category: str, commits: list[dict],
                 llm: LLMClient, prompt_dir: str,
                 logger: Any = None,
                 lang_inst: str = "") -> dict:
    """Phase A: Determine pattern title, Chinese name, optional See also."""
    prompt_path = os.path.join(prompt_dir, "phase_title.txt")
    if not os.path.exists(prompt_path):
        return _fallback_title(category, commits)

    system_prompt = _load_prompt(prompt_path) + lang_inst

    # Build commit summary
    lines = []
    for c in commits[:10]:
        msg = c.get("message", "")
        title_line = msg.split("\n")[0].strip() if msg else "(no message)"
        sha = c.get("sha", "?")[:7]
        lines.append(f"- {sha}: {title_line}")
    user_content = (
        f"Category: {category}\n\n"
        f"Commits in this group:\n" + "\n".join(lines)
    )

    try:
        result = llm.chat_json(system_prompt, user_content)
    except LLMError as e:
        if logger:
            logger.log(f"  ⚠ Title phase failed: {e}")
        return _fallback_title(category, commits)

    title_en = result.get("title_en", "").strip()
    title_cn = result.get("title_cn", "").strip()
    see_also = result.get("see_also", "").strip()

    if not title_en:
        return _fallback_title(category, commits)

    return {
        "title_en": title_en,
        "title_cn": title_cn,
        "see_also": see_also,
        "filename": _section_file_name(category),
    }


def _fallback_title(category: str, commits: list[dict]) -> dict:
    """Fallback: derive title from category name."""
    cat = category.strip()
    # Clean up prefix
    title_en = re.sub(r'^instruction[s]?\s+', '', cat, flags=re.IGNORECASE)
    title_en = re.sub(r'\s+for RISC-V', '', title_en, flags=re.IGNORECASE)
    return {
        "title_en": title_en,
        "title_cn": "",
        "see_also": "",
        "filename": _section_file_name(category),
    }


def _build_commit_context(commits: list[dict], max_commits: int = 50) -> str:
    """Build a rich context string from a group of commits for LLM prompts."""
    parts = []
    for i, c in enumerate(commits[:max_commits], 1):
        msg = c.get("message", "")
        sha = c.get("sha", "?")[:7]
        title = msg.split("\n")[0].strip() if msg else "(no message)"
        body = msg.split("\n", 1)[1].strip() if "\n" in msg else ""
        # Truncate body
        if len(body) > 500:
            body = body[:500] + "..."
        parts.append(
            f"=== Commit {i}: {sha} ===\n"
            f"Title: {title}\n"
            f"Body: {body}\n"
        )
    return "\n".join(parts)


def _phase_when(title_info: dict, commits: list[dict],
                llm: LLMClient, prompt_dir: str,
                logger: Any = None,
                max_context: int = 50,
                max_files_per_commit: int = 50,
                lang_inst: str = "") -> str:
    """Phase B: Generate §4 When to apply."""
    prompt_path = os.path.join(prompt_dir, "phase_when.txt")
    if not os.path.exists(prompt_path):
            return ""

    system_prompt = _load_prompt(prompt_path) + lang_inst
    ctx = _build_commit_context(commits, max_commits=max_context)
    diff_summary = summarize_diffs_for_group(commits, max_files_per_commit=max_files_per_commit, max_commits=max_context)

    user_content = (
        f"Pattern title: {title_info.get('title_en', '')} / {title_info.get('title_cn', '')}\n\n"
        f"## Commit Descriptions\n{ctx}\n\n"
        f"## Code Diffs\n{diff_summary}\n"
    )

    try:
        return llm.chat(system_prompt, user_content)
    except LLMError as e:
        if logger:
            logger.log(f"  ⚠ When phase failed: {e}")
        return ""


def _phase_why(title_info: dict, commits: list[dict],
               llm: LLMClient, prompt_dir: str,
               logger: Any = None,
               max_context: int = 50,
               lang_inst: str = "") -> str:
    """Phase C: Generate §5 Why this is slow."""
    prompt_path = os.path.join(prompt_dir, "phase_why.txt")
    if not os.path.exists(prompt_path):
            return ""

    system_prompt = _load_prompt(prompt_path) + lang_inst
    ctx = _build_commit_context(commits, max_commits=max_context)

    user_content = (
        f"Pattern title: {title_info.get('title_en', '')} / {title_info.get('title_cn', '')}\n\n"
        f"## Commit Descriptions\n{ctx}\n"
    )

    try:
        return llm.chat(system_prompt, user_content)
    except LLMError as e:
        if logger:
            logger.log(f"  ⚠ Why phase failed: {e}")
        return ""


def _phase_fix(title_info: dict, commits: list[dict],
               llm: LLMClient, prompt_dir: str,
               logger: Any = None,
               max_context: int = 50,
               max_files_per_commit: int = 50,
               lang_inst: str = "") -> str:
    """Phase D: Generate §6 The fix with code blocks."""
    prompt_path = os.path.join(prompt_dir, "phase_fix.txt")
    if not os.path.exists(prompt_path):
            return ""

    system_prompt = _load_prompt(prompt_path) + lang_inst
    diff_summary = summarize_diffs_for_group(commits, max_files_per_commit=max_files_per_commit, max_commits=max_context)
    ctx = _build_commit_context(commits, max_commits=max_context)

    user_content = (
        f"Pattern title: {title_info.get('title_en', '')} / {title_info.get('title_cn', '')}\n\n"
        f"## Commit Descriptions\n{ctx}\n\n"
        f"## Code Diffs\n{diff_summary}\n"
    )

    try:
        raw = llm.chat(system_prompt, user_content)
        return raw
    except LLMError as e:
        if logger:
            logger.log(f"  ⚠ Fix phase failed: {e}")
        return ""


def _phase_verify(title_info: dict, commits: list[dict],
                  llm: LLMClient, prompt_dir: str,
                  logger: Any = None,
                  max_context: int = 50,
                  lang_inst: str = "") -> str:
    """Phase E: Generate §7 Verification."""
    prompt_path = os.path.join(prompt_dir, "phase_verify.txt")
    if not os.path.exists(prompt_path):
            return ""

    system_prompt = _load_prompt(prompt_path) + lang_inst
    ctx = _build_commit_context(commits, max_commits=max_context)

    user_content = (
        f"Pattern title: {title_info.get('title_en', '')} / {title_info.get('title_cn', '')}\n\n"
        f"## Commit Details\n{ctx}\n"
    )

    try:
        return llm.chat(system_prompt, user_content)
    except LLMError as e:
        if logger:
            logger.log(f"  ⚠ Verify phase failed: {e}")
        return ""


def _phase_present(title_info: dict, commits: list[dict],
                   llm: LLMClient, prompt_dir: str,
                   logger: Any = None,
                   max_context: int = 50,
                   lang_inst: str = "") -> str:
    """Phase F1: Generate §8 Presenting."""
    prompt_path = os.path.join(prompt_dir, "phase_present.txt")
    if not os.path.exists(prompt_path):
            return ""

    system_prompt = _load_prompt(prompt_path) + lang_inst
    ctx = _build_commit_context(commits, max_commits=max_context)

    user_content = (
        f"Pattern title: {title_info.get('title_en', '')} / {title_info.get('title_cn', '')}\n\n"
        f"## Commit Details\n{ctx}\n"
    )

    try:
        return llm.chat(system_prompt, user_content)
    except LLMError as e:
        if logger:
            logger.log(f"  ⚠ Present phase failed: {e}")
        return ""


def _phase_related(title_info: dict, commits: list[dict],
                   llm: LLMClient, prompt_dir: str,
                   project_name: str = "",
                   logger: Any = None) -> str:
    """Phase F2: Generate §9 Related PRs table."""
    # Build the table directly from PR data (deterministic, no LLM needed)
    rows = []
    seen_keys = set()
    for c in commits:
        label = _commit_label(c)
        pr_url = extract_pr_url(c)
        if not pr_url:
            pr_url = c.get("url", "")

        # Dedup by URL
        if pr_url in seen_keys:
            continue
        seen_keys.add(pr_url)

        perf = _extract_perf_data(c)

        rows.append({
            "label": label,
            "pr_url": pr_url,
            "perf": perf,
        })

    if not rows:
        return ""

    # Use title_en as the sub-category group name
    group_name = title_info.get("title_en", "").strip() or project_name or "Related"

    # Format as 4-column markdown table (includes Project for cross-project knowledge base)
    lines = [
        "| 类别 (Category) | 项目 (Project) | 提交 (Commit) | 性能数据 (Performance) |",
        "|---|---|---|---|",
    ]

    for i, r in enumerate(rows):
        link = f"[{r['label']}]({r['pr_url']})"
        if i == 0:
            lines.append(f"| {group_name} | {project_name} | {link} | {r['perf']} |")
        else:
            lines.append(f"| ↳ | {project_name} | {link} | {r['perf']} |")

    return "\n".join(lines)


def _commit_label(commit: dict) -> str:
    """Best human-readable label for a commit — codebase-agnostic.

    Priority:
      1. PR number (e.g. '#12345') from URL or message
      2. Short SHA fallback
    """
    url = commit.get("url", "")
    msg = commit.get("message", "")

    # PR number from URL first
    m = re.search(r'/(?:pull|issues)/(\d{4,})', url)
    if m:
        return f"#{m.group(1)}"

    # PR number from message
    m = re.search(r'#(\d{4,})', msg)
    if m:
        return f"#{m.group(1)}"

    # Generic SHA fallback — works for any git-based project
    return commit.get("sha", "?")[:12]


def _extract_perf_data(commit: dict) -> str:
    """Extract performance data from commit message."""
    msg = commit.get("message", "")
    # Look for common perf indicators
    patterns = [
        r'(\d+[–-]\d+%\s*(improvement|speedup|gain))',
        r'(improvement\s+of\s+\d+[–-]\d+%)',
        r'(JMH.*?\d+\s*measurement)',
        r'(\d+[–-]\d+x\s*(faster|speedup))',
    ]
    for pat in patterns:
        m = re.search(pat, msg, re.IGNORECASE)
        if m:
            return m.group(1)
    # Check for perf section (body, not just subject)
    body = msg.split("\n", 1)[1] if "\n" in msg else ""
    if re.search(r'performance?\s*(result|data|test|benchmark)', body, re.IGNORECASE):
        return "Has perf section"
    if re.search(r'(improvement|speedup|throughput)', msg, re.IGNORECASE):
        return "Has perf data"
    return ""


def assemble_pattern_markdown(pattern: dict) -> str:
    """Assemble all sections into a complete markdown pattern document.

    Returns the full text ready to write to a .md file.
    """
    lines = []

    # §1 License
    lines.append(pattern.get("license", ""))
    lines.append("")

    # §2 Title
    title_en = pattern.get("title_en", "Untitled Pattern")
    title_cn = pattern.get("title_cn", "")
    if title_cn:
        lines.append(f"# Pattern: {title_en} / {title_cn}")
    else:
        lines.append(f"# Pattern: {title_en}")
    lines.append("")

    # §3 See also (optional)
    see_also = pattern.get("see_also", "")
    if see_also:
        lines.append(f"> **See also**: {see_also}")
        lines.append("")

    # §4 When to apply
    when = pattern.get("when_text", "")
    if when:
        # Remove leading/trailing whitespace
        when = when.strip()
        # Ensure heading is correct
        if not when.startswith("## When to apply"):
            lines.append("## When to apply / 何时适用")
            lines.append("")
        lines.append(when)
        if not when.endswith("\n"):
            lines.append("")
    else:
        lines.append("## When to apply / 何时适用")
        lines.append("")
        lines.append("*To be populated from profile signals.*")
        lines.append("")

    # §5 Why this is slow
    why = pattern.get("why_text", "")
    if why:
        why = why.strip()
        if not why.startswith("## Why this is slow"):
            lines.append("## Why this is slow / 为什么慢")
            lines.append("")
        lines.append(why)
        if not why.endswith("\n"):
            lines.append("")
    else:
        lines.append("## Why this is slow / 为什么慢")
        lines.append("")
        lines.append("*Architectural root cause analysis.*")
        lines.append("")

    # §6 The fix
    fix = pattern.get("fix_text", "")
    if fix:
        fix = fix.strip()
        if not fix.startswith("## The fix"):
            lines.append("## The fix / 修复方式")
            lines.append("")
        lines.append(fix)
        if not fix.endswith("\n"):
            lines.append("")
    else:
        lines.append("## The fix / 修复方式")
        lines.append("")
        lines.append("*Code examples from related PRs.*")
        lines.append("")

    # §7 Verification
    verify = pattern.get("verify_text", "")
    if verify:
        verify = verify.strip()
        if not verify.startswith("## Verification"):
            lines.append("## Verification / 验证")
            lines.append("")
        lines.append(verify)
        if not verify.endswith("\n"):
            lines.append("")
    else:
        lines.append("## Verification / 验证")
        lines.append("")
        lines.append("*Validation steps and test references.*")
        lines.append("")

    # §8 Presenting
    present = pattern.get("present_text", "")
    if present:
        present = present.strip()
        if not present.startswith("## Presenting"):
            lines.append("## Presenting to the user / 如何呈现给用户")
            lines.append("")
        lines.append(present)
        if not present.endswith("\n"):
            lines.append("")
    else:
        lines.append("## Presenting to the user / 如何呈现给用户")
        lines.append("")

    # §9 Related PRs
    related = pattern.get("related_table", "")
    if related:
        if not related.startswith("## Related PRs"):
            lines.append("## Related PRs / 关联提交")
            lines.append("")
        lines.append(related)
        lines.append("")
    else:
        lines.append("")
        lines.append("## Related PRs / 关联提交")
        lines.append("")
        lines.append("*Reference to related commits and PRs.*")
        lines.append("")

    return "\n".join(lines)
