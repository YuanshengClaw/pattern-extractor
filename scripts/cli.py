#!/usr/bin/env python3
"""Pattern Extractor — CLI entry point.

Extracts structured optimization pattern documents from rv-optkb-tool CSV data.

Input: CSV/xlsx files from rv-optkb-tool's csv-review/main.py
Columns: Idea, Thought, Commit URL, Correct?, Why

Usage:
    # List groups from CSV
    python3 -m scripts.cli list-groups -i ideas.csv

    # List groups from xlsx (multi-sheet)
    python3 -m scripts.cli list-groups -i optimization_ideas.xlsx

    # Generate patterns for all ready groups
    python3 -m scripts.cli generate -i ideas.csv -o output/patterns/

    # Generate with explicit project name
    python3 -m scripts.cli generate -i ideas.csv --project "OpenJDK" -o output/patterns/

    # QA check on existing pattern
    python3 -m scripts.cli qa -p output/patterns/fence-memory-barrier-reduction.md

    # Review and publish (move from patches/review to output)
    python3 -m scripts.cli publish -f patches/review/fence-pattern.md -o output/patterns/
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict

from .log_util import StepLogger
from .llm_client import LLMClient
from .csv_loader import load_any, groups_to_commit_dicts, GroupInfo
from .commit_fetcher import CommitFetcher
from .pr_grouping import (
    group_by_category,
    compute_group_stats,
    llm_subcluster,
    extract_pr_number,
)
from .pattern_generator import generate_pattern_sections, assemble_pattern_markdown
from .pattern_qa import run_all_checks, print_qa_report
from .merge_check import run_merge_check, print_report
from .pattern_writer import (
    write_pattern_md,
    load_pattern_index,
    save_pattern_index,
    update_pattern_index,
    extract_sub_methods_from_fix,
    update_trigger_table,
    ensure_output_dir,
    PATTERN_INDEX_FILENAME,
)


def _load_config(path: str) -> dict:
    if not os.path.exists(path):
        print(f"Error: config not found: {path}", file=sys.stderr)
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["_config_path"] = os.path.abspath(path)

    import re as _re
    def _expand_env(val):
        if isinstance(val, str):
            def _replace(m):
                var = m.group(1)
                return os.environ.get(var, m.group(0))
            return _re.sub(r'\$\{(\w+)\}', _replace, val)
        if isinstance(val, dict):
            return {k: _expand_env(v) for k, v in val.items()}
        if isinstance(val, list):
            return [_expand_env(v) for v in val]
        return val
    cfg = _expand_env(cfg)

    return cfg


def _resolve_prompt_dir(config: dict) -> str:
    """Return absolute path to prompts/ directory."""
    rel = config.get("prompt_dir", "prompts")
    if os.path.isabs(rel):
        return rel
    # Resolve relative to project root (where config.json is)
    config_dir = os.path.dirname(config.get("_config_path", os.path.abspath(".")))
    return os.path.join(config_dir, rel)


# ── Pipeline: CSV → enriched commits → groups ────────────────────────────────


def _load_and_enrich(csv_path: str,
                     project_name: str = "",
                     skip_fetch: bool = False,
                     verbose: bool = False,
                     rate_limit_delay: float = 0.3) -> list[dict]:
    """Full pipeline: load CSV/xlsx → enrich commits via GitHub API.

    Args:
        csv_path: Path to .csv or .xlsx file.
        project_name: Override project name (auto-detected from xlsx sheet name
                      or filename if empty).
        skip_fetch: Skip GitHub API fetching (for testing / offline).
        verbose: Print progress info.

    Returns:
        List of enriched commit dicts (same shape as classified.json commits).
        Each dict has: url, idea (category), message, diff, sha, owner, repo.
    """
    if verbose:
        print(f"\n  Loading: {csv_path}")

    groups: list[GroupInfo] = load_any(csv_path, project_name)
    if not groups:
        print("  No groups found in input file.")
        return []

    if project_name:
        before = len(groups)
        groups = [g for g in groups if g.project.lower().startswith(project_name.lower())]
        if verbose and len(groups) < before:
            print(f"  Project filter '{project_name}': {before} → {len(groups)} groups")

    if verbose:
        total_groups = len(groups)
        total_commits = sum(g.count for g in groups)
        print(f"  Groups: {total_groups}, Total commits: {total_commits}")
        for g in groups:
            print(f"    {g.project}: {g.idea} ({g.count} commits)")

    # Flatten to commit dicts
    commits = groups_to_commit_dicts(groups)

    # Enrich with message + diff from GitHub
    if not skip_fetch:
        fetcher = CommitFetcher(rate_limit_delay=rate_limit_delay)
        start = time.time()
        commits = fetcher.enrich_commits(commits)
        elapsed = time.time() - start
        if verbose:
            s = fetcher.stats
            print(f"  GitHub API: {s['fetched']} fetched, {s['cached']} cached, "
                  f"{s['errors']} errors ({elapsed:.1f}s)")
    else:
        # Skip fetch: set placeholder message for each commit
        if verbose:
            print("  Skipping GitHub fetch (--skip-fetch)")
        for c in commits:
            c["message"] = c.get("thought", c.get("url", ""))
            c["diff"] = ""

    return commits


def _group_commits(commits: list[dict],
                   min_prs: int = 1,
                   max_prs_before_subcluster: int = 100) -> list[dict]:
    """Group enriched commits by 'idea' field, compute stats.

    Args:
        commits: List of enriched commit dicts.
        min_prs: Minimum PRs for pattern_ready (from config or default).
        max_prs_before_subcluster: Threshold for LLM subclustering.

    Returns same shape as compute_group_stats: list of group_info dicts.
    """
    # Group by "idea" field (from CSV's Idea column)
    groups: dict[str, list[dict]] = defaultdict(list)
    for c in commits:
        idea = c.get("idea", "").strip()
        if not idea:
            idea = "uncategorized"
        groups[idea].append(c)

    return compute_group_stats(groups, min_prs=min_prs,
                               max_prs_before_subcluster=max_prs_before_subcluster)


# ── Commands ──────────────────────────────────────────────────────────────────


def cmd_list_groups(args):
    """List PR groups from CSV/xlsx data."""
    commits = _load_and_enrich(args.input, args.project, skip_fetch=args.skip_fetch, verbose=True)
    if not commits:
        return

    stats = _group_commits(commits)
    effective_min_prs = 1  # default, matches _group_commits default

    print(f"\nGroups: {len(stats)}\n")
    print(f"{'Ready':>6} {'Count':>6}  Category / Idea")
    print(f"{'-'*6:>6} {'-'*6:>6}  {'-'*50}")
    for s in stats:
        ready = "✓" if s["pattern_ready"] else " "
        print(f"  {ready}  {s['count']:>4}  {s['category'][:60]}")
    print()
    ready_count = sum(1 for s in stats if s["pattern_ready"])
    print(f"{ready_count} group(s) ready for pattern generation (≥{effective_min_prs} PRs)")


def cmd_generate(args):
    """Generate patterns from CSV/xlsx data."""
    config = _load_config(args.config)
    prompt_dir = _resolve_prompt_dir(config)

    # ── Pipeline config (read before _load_and_enrich uses rate_limit) ─────
    pipeline_cfg = config.get("pipeline", {})
    github_cfg = config.get("github", {})
    qa_cfg = config.get("qa", {})
    license_cfg = config.get("license", {})
    min_prs = pipeline_cfg.get("min_prs_for_pattern", 1)
    max_context = pipeline_cfg.get("max_prs_for_llm_context", 50)
    max_subcluster = pipeline_cfg.get("max_prs_before_subcluster", 100)
    max_files = pipeline_cfg.get("max_files_per_commit", 50)
    rate_limit = github_cfg.get("rate_limit_delay", 0.3)
    review_before_publish = pipeline_cfg.get("review_before_publish", True)
    qa_overlap = qa_cfg.get("why_when_overlap_threshold", 0.7)
    license_text = (
        f'<!-- (C) {license_cfg.get("copyright_year", "2026")} '
        f'{license_cfg.get("copyright_holder", "Intel Corporation")}, '
        f'{license_cfg.get("spdx_identifier", "MIT")} license -->'
    )

    # Load and enrich
    project_name = args.project or config.get("project", {}).get("name", "")
    commits = _load_and_enrich(
        args.input, project_name,
        skip_fetch=args.skip_fetch,
        verbose=args.verbose,
        rate_limit_delay=rate_limit,
    )
    if not commits:
        print("No commits loaded. Nothing to generate.")
        return



    # Resolve review_first: CLI --no-review overrides config, config overrides default
    if args.review is None:
        review_first = review_before_publish
    else:
        review_first = args.review

    stats = _group_commits(commits, min_prs=min_prs,
                           max_prs_before_subcluster=max_subcluster)

    # Filter by group pattern if specified
    if args.group:
        stats = [s for s in stats if args.group.lower() in s["category"].lower()]
        if not stats:
            print(f"No group matching '{args.group}'", file=sys.stderr)
            sys.exit(1)

    # Initialize LLM
    llm_config = config.get("llm", {})
    if not llm_config.get("api_key"):
        llm_config["api_key"] = os.environ.get("OPENAI_API_KEY", "")
    if not llm_config.get("api_key"):
        print("Error: No LLM API key. Set OPENAI_API_KEY env or in config.json.",
              file=sys.stderr)
        sys.exit(1)
    llm = LLMClient(llm_config)

    # ── Output dirs (CLI > config > default) ───────────────────────────────
    output_cfg = config.get("output", {})
    raw_output = args.output or output_cfg.get("pattern_dir", "output/patterns")
    output_dir = ensure_output_dir(raw_output)

    index_dir = args.index_dir
    if not index_dir:
        index_dir = output_cfg.get("index_dir", "")
    if not index_dir:
        index_dir = os.path.join(output_dir, "..", "existing_patterns")

    triggers_dir = output_cfg.get("triggers_dir", "")
    if not triggers_dir:
        triggers_dir = os.path.join(output_dir, "..", "triggers")

    log_dir = args.log_dir or output_cfg.get("log_dir", os.path.join(os.path.dirname(raw_output), "logs"))
    os.makedirs(log_dir, exist_ok=True)

    # Load existing index
    pattern_index = load_pattern_index(index_dir)

    generated_count = 0
    for s in stats:
        if not s["pattern_ready"]:
            print(f"  ⏭ Skipping '{s['category']}' — only {s['count']} PRs (< {min_prs})")
            continue

        if args.max and generated_count >= args.max:
            break

        logger = StepLogger(log_dir, f"gen_{s['category'][:30].replace('/', '_')}") if args.verbose else None
        if logger:
            print(f"\n  Generating: {s['category']} ({s['count']} PRs)")
            print(f"  Log: {logger.log_path}")

        # Generate
        pattern_data = generate_pattern_sections(
            s, llm, prompt_dir, project_name, logger=logger,
            max_context_commits=max_context,
            max_files_per_commit=max_files,
            language=config.get("language", "zh"),
            license_text=license_text,
        )
        pattern_text = assemble_pattern_markdown(pattern_data)

        # Write
        filename = pattern_data["filename"]
        if not filename.endswith(".md"):
            filename += ".md"
        filepath = write_pattern_md(pattern_text, filename, output_dir, review_first=review_first)

        # QA
        if args.qa:
            qa_checks = run_all_checks(pattern_text, overlap_threshold=qa_overlap)
            print_qa_report(qa_checks, f"QA: {s['category']}")

        # Update index
        pr_ids = []
        pr_urls = []
        for c in s["commits"][:20]:
            pid = c.get("_pr_number", "")
            if not pid:
                pid = extract_pr_number(c)
            pr_ids.append(pid)
            url = c.get("url", "")
            if url:
                pr_urls.append(url)

        sub_methods = extract_sub_methods_from_fix(pattern_text)
        update_pattern_index(
            pattern_index,
            filename=filename,
            title_en=pattern_data.get("title_en", s["category"]),
            title_cn=pattern_data.get("title_cn", ""),
            category=s["category"],
            pr_ids=pr_ids,
            pr_urls=pr_urls,
            sub_methods=sub_methods,
        )

        # Update trigger table
        update_trigger_table(pattern_text, filename, triggers_dir)

        if logger:
            logger.log(f"  → Written: {filepath}")
            logger.done()
            logger.close()

        print(f"  ✓ {filename}")
        generated_count += 1

    # Save index
    save_pattern_index(pattern_index, index_dir)
    print(f"\nGenerated: {generated_count} pattern(s)")
    print(f"Index: {os.path.join(index_dir, PATTERN_INDEX_FILENAME)}")


def cmd_merge_check(args):
    """Compare new patterns against existing pattern library for merge candidates."""
    config = _load_config(args.config)
    prompt_dir = _resolve_prompt_dir(config)
    prompt_path = os.path.join(prompt_dir, "merge_judge.txt")
    if not os.path.exists(prompt_path):
        print(f"Error: prompt not found: {prompt_path}", file=sys.stderr)
        sys.exit(1)

    # Init LLM
    llm_config = config.get("llm", {})
    if not llm_config.get("api_key"):
        llm_config["api_key"] = os.environ.get("OPENAI_API_KEY", "")
    if not llm_config.get("api_key"):
        print("Error: No LLM API key.", file=sys.stderr)
        sys.exit(1)
    llm = LLMClient(llm_config)

    merge_cfg = config.get("merge_check", {})
    results = run_merge_check(
        existing_dir=args.existing,
        new_dir=args.new,
        llm=llm,
        prompt_path=prompt_path,
        verbose=args.verbose,
        jaccard_threshold=merge_cfg.get("jaccard_threshold", 0.2),
        max_candidates=merge_cfg.get("max_candidates", 20),
    )
    print_report(results)


def cmd_qa(args):
    """Run quality checks on existing pattern file(s)."""
    config = _load_config(args.config)
    qa_cfg = config.get("qa", {})
    overlap_threshold = qa_cfg.get("why_when_overlap_threshold", 0.7)

    files = args.patterns
    if not files:
        print("Error: specify at least one pattern file", file=sys.stderr)
        sys.exit(1)

    all_pass = True
    for fpath in files:
        if not os.path.exists(fpath):
            print(f"  ✗ File not found: {fpath}", file=sys.stderr)
            all_pass = False
            continue
        with open(fpath, "r", encoding="utf-8") as f:
            text = f.read()
        checks = run_all_checks(text, overlap_threshold=overlap_threshold)
        ok = print_qa_report(checks, f"QA: {os.path.basename(fpath)}")
        if not ok:
            all_pass = False

    sys.exit(0 if all_pass else 1)


def cmd_publish(args):
    """Move a pattern from patches/review to the output directory."""
    config = _load_config(args.config)
    qa_cfg = config.get("qa", {})
    overlap_threshold = qa_cfg.get("why_when_overlap_threshold", 0.7)

    src = args.file
    if not os.path.exists(src):
        print(f"Error: source not found: {src}", file=sys.stderr)
        sys.exit(1)

    dest_dir = ensure_output_dir(args.output)
    filename = os.path.basename(src)
    dest_path = os.path.join(dest_dir, filename)

    with open(src, "r", encoding="utf-8") as f:
        text = f.read()

    # Run QA before publishing
    checks = run_all_checks(text, overlap_threshold=overlap_threshold)
    ok = print_qa_report(checks, f"Pre-publish QA: {filename}")
    if not ok and not args.force:
        print("QA checks failed. Use --force to publish anyway.", file=sys.stderr)
        sys.exit(1)

    with open(dest_path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"Published: {src} → {dest_path}")


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Pattern Extractor — extract optimization patterns from rv-optkb-tool CSV data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 -m scripts.cli list-groups -i ideas.csv\n"
            "  python3 -m scripts.cli generate -i ideas.csv -o output/patterns/\n"
            "  python3 -m scripts.cli list-groups -i optimization_ideas.xlsx\n"
            "  python3 -m scripts.cli merge-check --existing output/patterns/ --new staging/patterns/\n"
            "  python3 -m scripts.cli qa -p output/patterns/*.md\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # list-groups
    lg = subparsers.add_parser("list-groups",
                               help="List PR groups from CSV/xlsx input")
    lg.add_argument("-i", "--input", required=True,
                    help="CSV or xlsx file from rv-optkb-tool csv-review")
    lg.add_argument("--project", default="",
                    help="Project name (auto-detected from xlsx sheet or filename)")
    lg.add_argument("--skip-fetch", action="store_true",
                    help="Skip GitHub API commit fetch (list groups only)")

    # generate
    gen = subparsers.add_parser("generate",
                                help="Generate patterns from CSV/xlsx input")
    gen.add_argument("-i", "--input", required=True,
                     help="CSV or xlsx file from rv-optkb-tool csv-review")
    gen.add_argument("-o", "--output",
                     help="Output directory for pattern .md files"
                          " (default: config output.pattern_dir)")
    gen.add_argument("-c", "--config", default="config.json",
                     help="Config file (default: config.json)")
    gen.add_argument("--project", default="",
                     help="Project name (overrides config.json project.name)")
    gen.add_argument("--group", help="Generate only groups matching this keyword")
    gen.add_argument("--max", type=int, default=0,
                     help="Max patterns to generate (0 = unlimited)")
    gen.add_argument("--skip-fetch", action="store_true",
                     help="Skip GitHub API commit fetch (for testing)")
    gen.add_argument("--no-qa", dest="qa", action="store_false",
                     help="Skip QA after generation")
    gen.add_argument("--no-review", dest="review", action="store_false",
                     default=None,
                     help="Write directly to output dir (skip review staging)"
                          " (default: config pipeline.review_before_publish)")
    gen.add_argument("--index-dir",
                     help="Directory for pattern_index.json"
                          " (default: config output.index_dir)")
    gen.add_argument("--log-dir", help="Log directory (default: <output>/logs/)")
    gen.add_argument("-v", "--verbose", action="store_true",
                     help="Detailed progress logging")
    gen.set_defaults(qa=True)

    # merge-check
    mc = subparsers.add_parser("merge-check",
                               help="Compare new patterns against existing library")
    mc.add_argument("--existing", required=True,
                    help="Directory of existing (published) patterns")
    mc.add_argument("--new", required=True,
                    help="Directory of new (staging) patterns")
    mc.add_argument("-c", "--config", default="config.json",
                    help="Config file (default: config.json)")
    mc.add_argument("-v", "--verbose", action="store_true",
                    help="Detailed progress")

    # qa
    qa = subparsers.add_parser("qa",
                                help="Run quality checks on pattern .md files")
    qa.add_argument("-p", "--patterns", nargs="+", required=True,
                     help="Pattern .md file(s) to check")
    qa.add_argument("-c", "--config", default="config.json",
                     help="Config file (default: config.json)")

    # publish
    pub = subparsers.add_parser("publish",
                                help="Publish pattern from review to output")
    pub.add_argument("-f", "--file", required=True,
                     help="Review file path (patches/review/*.md)")
    pub.add_argument("-o", "--output", required=True,
                     help="Output directory")
    pub.add_argument("--force", action="store_true",
                     help="Publish even if QA fails")
    pub.add_argument("-c", "--config", default="config.json",
                     help="Config file (default: config.json)")

    args = parser.parse_args()

    if args.command == "list-groups":
        cmd_list_groups(args)
    elif args.command == "generate":
        cmd_generate(args)
    elif args.command == "merge-check":
        cmd_merge_check(args)
    elif args.command == "qa":
        cmd_qa(args)
    elif args.command == "publish":
        cmd_publish(args)


if __name__ == "__main__":
    main()
