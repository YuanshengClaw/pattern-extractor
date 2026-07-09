#!/usr/bin/env python3
"""Merge check — compare new patterns against existing pattern library.

Two-phase matching:
  1. Keyword pre-filter: Jaccard similarity on title keywords
  2. LLM judgment: for candidate pairs, decide if they describe the same
     optimization technique and should be merged.

Three output categories:
  - merge (high confidence): clearly the same technique → candidate for merge
  - suspect (medium/low): may overlap, needs human review
  - new (no match): genuinely new optimization pattern
"""

import json
import os
import re
from collections import Counter
from typing import Any

from .llm_client import LLMClient, LLMError

# ── Stop words ─────────────────────────────────────────────────────────────────

STOP_WORDS = {
    "the", "a", "an", "for", "of", "to", "in", "and", "or", "with",
    "on", "at", "by", "is", "are", "was", "were", "be", "been",
    "its", "it", "this", "that", "these", "those", "from", "as",
    "into", "through", "during", "before", "after", "above", "below",
    "between", "out", "off", "over", "under", "again", "further",
    "then", "once", "here", "there", "when", "where", "why", "how",
    "all", "each", "every", "both", "few", "more", "most", "other",
    "some", "such", "no", "nor", "not", "only", "own", "same", "so",
    "than", "too", "very", "just", "also", "has", "have", "had",
    "do", "does", "did", "doing", "but", "if", "because", "while",
    "about", "up", "down",
}

# ── Pattern scanning ───────────────────────────────────────────────────────────


def extract_title(text: str) -> str:
    """Extract pattern title from markdown."""
    m = re.search(r'^#\s+Pattern:\s+(.+)$', text, re.MULTILINE)
    return m.group(1).strip() if m else "(untitled)"


def extract_when_snippet(text: str, max_chars: int = 400) -> str:
    """Extract first paragraph of §4 When to apply."""
    m = re.search(
        r'## When to apply.*?\n(.*?)(?=\n##\s|\Z)', text, re.DOTALL
    )
    if not m:
        return ""
    section = m.group(1).strip()
    # Take first paragraph (up to double newline, or cap)
    para = section.split("\n\n")[0].strip()
    if len(para) > max_chars:
        para = para[:max_chars] + "..."
    return para


def extract_keywords(text: str) -> list[str]:
    """Extract clean keywords from a pattern title for pre-filtering.

    Handles hyphenated terms (RISC-V → risc-v, not risc+v) and
    common compound abbreviations (RISC-V also yields riscv).
    """
    text_lower = text.lower()
    words = re.findall(r'[a-zA-Z0-9+#]+(?:[-][a-zA-Z0-9]+)*', text_lower)
    # Also add joined forms for common hyphenated tech terms
    extra = []
    for w in words:
        if '-' in w:
            extra.append(w.replace('-', ''))
    words.extend(extra)
    return [w for w in words if w not in STOP_WORDS and len(w) > 1]


def jaccard_similarity(a: set[str], b: set[str]) -> float:
    """Jaccard similarity between two keyword sets."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ── Pattern data ──────────────────────────────────────────────────────────────


class PatternInfo:
    """Parsed metadata from a pattern .md file."""

    __slots__ = ("filename", "title", "when_snippet", "keywords", "full_text")

    def __init__(self, filename: str, title: str, when_snippet: str,
                 full_text: str):
        self.filename = filename
        self.title = title
        self.when_snippet = when_snippet
        self.keywords = set(extract_keywords(title))
        self.full_text = full_text

    @property
    def display_name(self) -> str:
        return f"{self.filename}: \"{self.title}\""


def scan_patterns(directory: str) -> list[PatternInfo]:
    """Scan a directory for pattern .md files and parse metadata."""
    if not os.path.isdir(directory):
        print(f"  ⚠ Not a directory: {directory}", file=os.stderr)
        return []

    results: list[PatternInfo] = []
    for fname in sorted(os.listdir(directory)):
        if not fname.endswith(".md"):
            continue
        fpath = os.path.join(directory, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                text = f.read()
        except (IOError, UnicodeDecodeError) as e:
            print(f"  ⚠ Skipping {fname}: {e}", file=os.stderr)
            continue

        title = extract_title(text)
        when = extract_when_snippet(text)
        results.append(PatternInfo(fname, title, when, text))

    return results


# ── Keyword pre-filter ────────────────────────────────────────────────────────


def find_candidates(
    new: PatternInfo,
    existing: list[PatternInfo],
    threshold: float = 0.2,
    max_candidates: int = 5,
) -> list[tuple[PatternInfo, float]]:
    """Find existing patterns whose title overlaps with the new pattern.

    Returns sorted list of (candidate, score), highest score first.
    """
    scored: list[tuple[PatternInfo, float]] = []
    for ex in existing:
        score = jaccard_similarity(new.keywords, ex.keywords)
        if score >= threshold:
            scored.append((ex, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:max_candidates]


# ── LLM judgment ──────────────────────────────────────────────────────────────


def load_prompt(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def llm_judge(
    new_pattern: PatternInfo,
    candidate: PatternInfo,
    llm: LLMClient,
    prompt_text: str,
) -> dict[str, Any]:
    """Ask LLM whether two patterns describe the same optimization technique.

    Returns: { "match": filename | null, "confidence": str, "reason": str }
    """
    # Build user content
    user = (
        f"=== Existing Pattern ===\n"
        f"File: {candidate.filename}\n"
        f"Title: {candidate.title}\n"
        f"When: {candidate.when_snippet}\n\n"
        f"=== New Pattern ===\n"
        f"File: {new_pattern.filename}\n"
        f"Title: {new_pattern.title}\n"
        f"When: {new_pattern.when_snippet}\n"
    )

    try:
        result = llm.chat_json(prompt_text, user)
    except LLMError as e:
        return {
            "match": None,
            "confidence": "low",
            "reason": f"LLM judgment failed: {e}",
        }

    # Normalize: ensure match field is filename or None
    match_val = result.get("match")
    if match_val and isinstance(match_val, str) and match_val.strip():
        # Accept both filename and title reference
        result["match"] = match_val.strip()
    else:
        result["match"] = None

    result.setdefault("confidence", "low")
    result.setdefault("reason", "")
    return result


# ── Orchestrator ──────────────────────────────────────────────────────────────


def run_merge_check(
    existing_dir: str,
    new_dir: str,
    llm: LLMClient,
    prompt_path: str,
    verbose: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Full merge check pipeline.

    Returns:
        {
            "merge": [  # high confidence matches → auto-merge candidate
                { "new": PatternInfo, "existing": PatternInfo,
                  "reason": "...", "score": 0.35 },
                ...
            ],
            "suspect": [  # medium/low confidence → needs human review
                { "new": PatternInfo, "existing": PatternInfo,
                  "reason": "...", "confidence": "medium", "score": 0.3 },
                ...
            ],
            "new": [  # no match → brand new pattern
                PatternInfo,
                ...
            ],
        }
    """
    existing = scan_patterns(existing_dir)
    new_patterns = scan_patterns(new_dir)

    if not new_patterns:
        print("  No new patterns found.")
        return {"merge": [], "suspect": [], "new": []}

    if verbose:
        print(f"  Existing: {len(existing)} patterns, New: {len(new_patterns)} patterns")

    prompt_text = load_prompt(prompt_path)

    merge_results: list[dict[str, Any]] = []
    suspect_results: list[dict[str, Any]] = []
    new_results: list[PatternInfo] = []

    for np in new_patterns:
        candidates = find_candidates(np, existing)

        if not candidates:
            if verbose:
                print(f"  [new] {np.filename} — no keyword overlap")
            new_results.append(np)
            continue

        # Try LLM judgment on best candidates
        judged = False
        for cand, score in candidates:
            verdict = llm_judge(np, cand, llm, prompt_text)
            confidence = verdict.get("confidence", "low")
            match_file = verdict.get("match")

            if match_file and confidence == "high":
                merge_results.append({
                    "new": np,
                    "existing": cand,
                    "score": round(score, 2),
                    "reason": verdict.get("reason", ""),
                })
                judged = True
                if verbose:
                    print(f"  [merge] {np.filename} → {cand.filename} (score={score:.2f}, LLM: high)")
                break
            elif match_file and confidence == "medium":
                suspect_results.append({
                    "new": np,
                    "existing": cand,
                    "score": round(score, 2),
                    "confidence": "medium",
                    "reason": verdict.get("reason", ""),
                })
                judged = True
                if verbose:
                    print(f"  [suspect] {np.filename} → {cand.filename} (score={score:.2f}, LLM: medium)")
                break
            elif confidence == "medium":
                # No match but medium confidence on unrelatedness — flag it
                suspect_results.append({
                    "new": np,
                    "existing": cand,
                    "score": round(score, 2),
                    "confidence": "low",
                    "reason": verdict.get("reason", ""),
                })
                judged = True
                if verbose:
                    print(f"  [suspect] {np.filename} — keyword matched but LLM unsure (score={score:.2f})")
                break
            # confidence=="low" with no match → try next candidate

        if not judged:
            new_results.append(np)
            if verbose:
                print(f"  [new] {np.filename} — no matching candidate after LLM check")

    return {
        "merge": merge_results,
        "suspect": suspect_results,
        "new": new_results,
    }


# ── Report ─────────────────────────────────────────────────────────────────────


def print_report(results: dict[str, list[dict[str, Any]]],
                 border: str = "─") -> None:
    """Print formatted merge check report."""
    merge_items = results.get("merge", [])
    suspect_items = results.get("suspect", [])
    new_items = results.get("new", [])

    width = 60
    sep = f"╔{'═' * width}╗"
    sep_end = f"╚{'═' * width}╝"

    print(f"\n{sep}")
    print(f"║{' Merge Check Report':^{width}}║")
    print(sep_end)

    # Merge candidates
    if merge_items:
        print(f"\n✅ Merge candidates (high confidence):")
        for item in merge_items:
            np = item["new"]
            ep = item["existing"]
            print(f"\n  existing: {ep.display_name}")
            print(f"  new:      {np.display_name}")
            if item.get("reason"):
                print(f"  → {item['reason']}")
            print(f"  → Action: merge new content into existing pattern")
    else:
        print(f"\n✅ No high-confidence merge candidates.")

    # Suspect
    if suspect_items:
        print(f"\n{'─' * 40}")
        print(f"❓ Suspected matches (needs human review):")
        for item in suspect_items:
            np = item["new"]
            ep = item["existing"]
            conf = item.get("confidence", "low")
            print(f"\n  existing: {ep.display_name}")
            print(f"  new:      {np.display_name}")
            print(f"  confidence: {conf}")
            if item.get("reason"):
                print(f"  → {item['reason']}")
            print(f"  keyword score: {item.get('score', '?')}")
            print(f"  → Action: review manually")
    else:
        print(f"\n{'─' * 40}")
        print(f"❓ No suspected matches.")

    # New
    if new_items:
        print(f"\n{'─' * 40}")
        print(f"🆕 New patterns (no match found):")
        for np in new_items:
            print(f"  {np.display_name}")
        print(f"  → Action: QA → publish as new patterns")
    else:
        print(f"\n{'─' * 40}")
        print(f"🆕 No new patterns (all matched existing).")

    # Summary
    print(f"\n{'─' * 40}")
    print(f"  Summary:")
    print(f"    Merge candidates: {len(merge_items)}")
    print(f"    Suspected:        {len(suspect_items)}")
    print(f"    New patterns:     {len(new_items)}")
    print(f"{'─' * 40}\n")
