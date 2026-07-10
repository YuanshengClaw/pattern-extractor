#!/usr/bin/env python3
"""Phase 4: Automated quality checking for pattern documents.

Implements the 9-item QA checklist from SKILL.md, plus additional checks
for consistency between sections.
"""

import re
from typing import Any

# ── Individual Quality Checks ─────────────────────────────────────────────────


def check_license(text: str) -> dict:
    """§1 Check: License header exists and has correct format."""
    has_license = bool(re.search(r'<!--\s*\(C\)\s+\d{4}\s+.*?,\s*.*?license\s*-->', text, re.IGNORECASE))
    return {
        "check": "§1 License header",
        "pass": has_license,
        "detail": "License header found" if has_license else "Missing or malformed license header",
    }


def check_title(text: str) -> dict:
    """§2 Check: Title has 'Pattern: ' prefix and proper format."""
    has_pattern_prefix = bool(re.search(r'^#\s+Pattern:\s+', text, re.MULTILINE))
    has_english = bool(re.search(r'^#\s+Pattern:\s+[A-Za-z]', text, re.MULTILINE))
    # Optional Chinese part after ' / '
    return {
        "check": "§2 Title format",
        "pass": has_pattern_prefix and has_english,
        "detail": (
            "Title OK" if has_pattern_prefix and has_english
            else "Missing 'Pattern: ' prefix or English title"
        ),
    }


def check_when_signals(text: str) -> dict:
    """§4 Check: Profile signals start with tool name."""
    # Find lines in the Profile signals section
    in_signals = False
    signal_lines = []
    for line in text.split('\n'):
        stripped = line.strip()
        if stripped == 'Profile signals：' or stripped == 'Profile signals:':
            in_signals = True
            continue
        if in_signals:
            # Stop at next section heading or empty line that ends signals
            if stripped.startswith('## ') or stripped.startswith('### '):
                break
            if stripped.startswith('- `'):
                signal_lines.append(stripped)
            elif stripped == '' and signal_lines:
                # Empty line after signals started → check if more signals follow
                pass
            elif stripped and not stripped.startswith('-') and signal_lines:
                break

    if not signal_lines:
        # Try alternate: look for backtick tool name patterns
        signal_lines = re.findall(r'^- `[^`]+`', text, re.MULTILINE)
        if not signal_lines:
            return {
                "check": "§4 Profile signals start with tool name",
                "pass": False,
                "detail": "No profile signals found (expecting lines like '- `perf annotate` ...')",
            }

    # Match: `tool-name` or `tool name with spaces` or `-XX:+FlagName`
    tool_signals = [s for s in signal_lines if re.match(r'^- `[^`]+`', s)]
    bad_signals = [s for s in signal_lines if not re.match(r'^- `[^`]+`', s)]

    return {
        "check": "§4 Profile signals start with tool name",
        "pass": len(tool_signals) > 0,
        "detail": (
            f"{len(tool_signals)} signal(s) start with tool name"
            if len(tool_signals) > 0
            else f"No signals start with tool name (bad: {len(bad_signals)})"
        ),
    }


def check_why_not_when(text: str,
                       overlap_threshold: float = 0.7) -> dict:
    """§5 Check: Why section doesn't just restate When section content."""
    # Extract Why section
    why_match = re.search(r'## Why this is slow.*?\n(.*?)(?=\n## )', text, re.DOTALL)
    when_match = re.search(r'## When to apply.*?\n(.*?)(?=\n## )', text, re.DOTALL)

    if not why_match:
        return {"check": "§5 Why not restating When", "pass": False, "detail": "Why section not found"}

    why_text = why_match.group(1).lower()
    when_text = when_match.group(1).lower() if when_match else ""

    # Check for sign of architecture-level reasoning (English + Chinese)
    has_arch_keywords = any(
        kw in why_text for kw in [
            "cycle", "pipeline", "latency", "throughput", "cache miss",
            "register pressure", "memory access", "drain", "stall",
            "overhead", "bottleneck", "microarchitectur",
            "流水线", "延迟", "缓存", "瓶颈", "吞吐",
            "寄存器", "指令选择", "向量化", "开销",
        ]
    )

    # Check if Why is just repeating When
    why_words = set(why_text.split())
    when_words = set(when_text.split())
    if when_words and len(why_words) > 10:
        overlap = len(why_words & when_words) / len(why_words)
        too_similar = overlap > overlap_threshold
    else:
        too_similar = False

    return {
        "check": "§5 Why has architecture reasoning",
        "pass": has_arch_keywords and not too_similar,
        "detail": (
            "Architecture reasoning detected" if has_arch_keywords
            else "No microarchitecture-level explanation found"
        ) + ("; may overlap with When section" if too_similar else ""),
    }


def check_fix_code_blocks(text: str) -> dict:
    """§6 Check: Code blocks have file path annotations and Before/After pairs."""
    # Count code blocks
    code_blocks = re.findall(r'```\w*\n(.*?)```', text, re.DOTALL)
    if not code_blocks:
        return {"check": "§6 The fix code blocks", "pass": False, "detail": "No code blocks found"}

    # Check for file path comments
    with_path = 0
    with_before = 0
    with_after = 0
    for block in code_blocks:
        if re.search(r'//\s*[\w./]+\.[\w]+', block):
            with_path += 1
        if re.search(r'//\s*Before:', block, re.IGNORECASE):
            with_before += 1
        if re.search(r'//\s*After:', block, re.IGNORECASE):
            with_after += 1

    return {
        "check": "§6 Code blocks have path + Before/After",
        "pass": with_path > 0 and with_before > 0 and with_after > 0,
        "detail": (
            f"{with_path} block(s) with file path, "
            f"{with_before} with 'Before:', {with_after} with 'After:'"
        ),
    }


def check_verify_completeness(text: str) -> dict:
    """§7 Check: Verification has tool command + test file + boundary."""
    verify_match = re.search(r'## Verification.*?\n(.*?)(?=\n## )', text, re.DOTALL)
    if not verify_match:
        return {"check": "§7 Verification completeness", "pass": False, "detail": "Verification section not found"}

    v = verify_match.group(1)
    has_tool = bool(re.search(r'`[a-z]+', v))
    has_test = bool(re.search(r'(test|Test|benchmark|Benchmark)', v))
    has_boundary = bool(re.search(r'(NaN|overflow|alignment|boundary|edge case|zero length)', v, re.IGNORECASE))

    checks_passed = sum([has_tool, has_test, has_boundary])
    return {
        "check": "§7 Verification completeness",
        "pass": checks_passed >= 2,
        "detail": (
            f"Tool: {'✓' if has_tool else '✗'}, "
            f"Test: {'✓' if has_test else '✗'}, "
            f"Boundary: {'✓' if has_boundary else '✗'}"
        ),
    }


def check_present_length(text: str) -> dict:
    """§8 Check: Presenting section is 2-8 sentences (approximate)."""
    present_match = re.search(r'## Presenting.*?\n(.*?)(?=\n## )', text, re.DOTALL)
    if not present_match:
        return {"check": "§8 Presenting length", "pass": False, "detail": "Presenting section not found"}

    p = present_match.group(1).strip()
    # Rough sentence count
    sentences = re.split(r'[.。!！?？\n]', p)
    sentences = [s.strip() for s in sentences if s.strip()]
    count = len(sentences)

    return {
        "check": "§8 Presenting length (2-8 sentences)",
        "pass": 2 <= count <= 8,
        "detail": f"~{count} sentence(s) found" if count > 0 else "No content",
    }


def check_related_pr_links(text: str) -> dict:
    """§9 Check: Related PRs table has proper links.

    Accepts both PR-number links (`[#12345](https://github.com/...)`)
    and commit SHA links (`[3d9a89b05733](https://github.com/.../commit/...)).
    """
    # Check for any markdown links (GitHub, GitLab, Gitee, self-hosted, etc.)
    pr_links = re.findall(r'\[[^\]]+\]\(https?://[^\)\s]+', text)
    return {
        "check": "§9 Related PRs have links",
        "pass": len(pr_links) > 0,
        "detail": f"{len(pr_links)} link(s) found" if pr_links else "No links found in Related PRs table",
    }


def check_related_dedup(text: str) -> dict:
    """§9 Check: No duplicate PR links in the table."""
    prs = re.findall(r'\[(#\d+)\]', text)
    seen = set()
    dupes = set()
    for p in prs:
        if p in seen:
            dupes.add(p)
        seen.add(p)
    return {
        "check": "§9 No duplicate PRs",
        "pass": len(dupes) == 0,
        "detail": f"Duplicate PRs: {', '.join(dupes)}" if dupes else "No duplicates",
    }


# ── Full QA Suite ─────────────────────────────────────────────────────────────


def run_all_checks(text: str,
                   overlap_threshold: float = 0.7) -> list[dict]:
    """Run all QA checks and return results."""
    checks = [
        check_license(text),
        check_title(text),
        check_when_signals(text),
        check_why_not_when(text, overlap_threshold=overlap_threshold),
        check_fix_code_blocks(text),
        check_verify_completeness(text),
        check_present_length(text),
        check_related_pr_links(text),
        check_related_dedup(text),
    ]
    return checks


def compute_quality_score(checks: list[dict]) -> str:
    """Compute quality badge from passes.

    Returns format like '●●●●●○○○○○' (5/9).
    """
    passed = sum(1 for c in checks if c["pass"])
    total = len(checks)
    filled = "●" * passed
    empty = "○" * (total - passed)
    return f"{filled}{empty} {passed}/{total}"


def print_qa_report(checks: list[dict], title: str = "Quality Check Report"):
    """Print a formatted QA report."""
    print(f"\n{'=' * 50}")
    print(f" {title}")
    print(f"{'=' * 50}")
    all_pass = all(c["pass"] for c in checks)
    for c in checks:
        status = "✓" if c["pass"] else "✗"
        print(f"  {status} {c['check']}")
        print(f"    {c['detail']}")
    print(f"\n  Score: {compute_quality_score(checks)}")
    print(f"  Verdict: {'PASS' if all_pass else 'NEEDS WORK'}")
    print(f"{'=' * 50}\n")
    return all_pass
