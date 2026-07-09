# Pattern Extractor

**Extract structured optimization pattern documents from classified optimization data — automatically.**

This tool implements a **CSV → Pattern Document** pipeline. It reads CSV/XLSX files in a compatible format (e.g., from [rv-optkb-tool](https://github.com/YuanshengClaw/rv-optkb-tool)), enriches commits via the GitHub API, groups them by optimization category, and generates structured 9-section pattern documents (`.md`) using LLM-driven multi-phase generation.

Each output pattern captures *when* an optimization applies, *why* the original code is slow (microarchitecture root cause), *how* to fix it with code Before/After examples, and *how* to verify the fix.

## Quick Start

### Prerequisites

- Python 3.10+
- GitHub API access: `gh auth login` strongly recommended (for commit enrichment)
- An LLM API endpoint compatible with the OpenAI chat completions format

### Install

```bash
pip install -r requirements.txt
```

For XLSX input support:

```bash
pip install openpyxl
```

### Configure

Edit `config.json` with your LLM API endpoint and project settings:

```json
{
    "llm": {
        "api_key": "sk-your-api-key",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o",
        "temperature": 0.1,
        "max_tokens": 4096
    },
    "project": {
        "name": "OpenJDK"
    },
    "language": "zh",
    "pipeline": {
        "min_prs_for_pattern": 2,
        "max_prs_for_llm_context": 8,
        "review_before_publish": true
    },
    "output": {
        "pattern_dir": "output/patterns",
        "index_dir": "existing_patterns",
        "triggers_dir": "triggers"
    }
}
```

`language` controls the output language of generated patterns:
- `"zh"` — Chinese (默认), technical terms remain in English
- `"en"` — English
- Unset → defaults to `"zh"` for backward compatibility

### Run the full pipeline

```bash
# List groups from CSV/xlsx (quick overview)
python3 -m scripts.cli list-groups -i ideas.csv

# Generate patterns for all ready groups
python3 -m scripts.cli generate -i ideas.csv -o output/patterns/
```

## Usage Guide

### Commands

```bash
python3 -m scripts.cli <command> [options]
```

| Command        | Step | Description                                                   |
| -------------- | ---- | ------------------------------------------------------------- |
| `list-groups`  | 1    | Load CSV/xlsx, group by Idea, show which groups are pattern-ready |
| `generate`     | 2    | Generate pattern .md documents for all ready groups           |
| `merge-check`  | 3    | Compare new (staging) patterns against existing library for merge candidates |
| `qa`           | 4    | Run 9-item quality checklist on existing pattern .md files    |
| `publish`      | 5    | Move a pattern from `patches/review/` to `output/patterns/`   |

### Detailed examples

```bash
# Step 1: Explore what's in your CSV data
python3 -m scripts.cli list-groups -i optimization_ideas.xlsx -v

# Filter by project (for multi-project xlsx)
python3 -m scripts.cli list-groups -i optimization_ideas.xlsx --project OpenJDK

# Step 2: Generate patterns
python3 -m scripts.cli generate -i ideas.csv -o output/patterns/ -v

# Generate with explicit project name
python3 -m scripts.cli generate -i openjdk_ideas.csv --project OpenJDK -o output/patterns/

# Generate only one specific group (by keyword match)
python3 -m scripts.cli generate -i ideas.csv -o output/patterns/ --group "fence"

# Generate directly to output (skip review staging)
python3 -m scripts.cli generate -i ideas.csv -o output/patterns/ --no-review

# Step 3: QA an existing pattern
python3 -m scripts.cli qa -p output/patterns/memory-barrier-reduction.md

# QA multiple patterns
python3 -m scripts.cli qa -p output/patterns/*.md

# Step 4: Merge check — find overlap with existing pattern library
python3 -m scripts.cli merge-check \
    --existing output/patterns/ \
    --new staging/patterns/ \
    -v

# Step 5: Publish from review to output
python3 -m scripts.cli publish -f patches/review/fence-pattern.md -o output/patterns/
python3 -m scripts.cli publish -f patches/review/fence-pattern.md -o output/patterns/ --force
```

### Cross-project merge check

New software's optimization patterns often overlap with existing ones across projects.
Use separate staging directories per project, then merge-check against the shared library:

```bash
# Phase 1: Extract OpenSSL patterns in isolation
python3 -m scripts.cli generate -i openssl.csv -o staging/openssl/ --no-review

# Phase 2: Compare against published pattern library (LLM semantic matching)
python3 -m scripts.cli merge-check \
    --existing output/patterns/ \
    --new staging/openssl/

# Result categories:
#    Merge candidates — LLM confirmed same technique → merge new PRs into existing pattern
#    Suspected matches — keyword overlap but LLM unsure → manual review
#    New patterns — no match → QA → publish as separate patterns
```

### Important flags

| Flag                        | Applies to        | Description                                           |
| --------------------------- | ----------------- | ----------------------------------------------------- |
| `--skip-fetch`              | `list-groups`, `generate` | Skip GitHub API commit fetching (offline/testing) |
| `--no-qa`                   | `generate`        | Skip QA checks after generation                       |
| `--no-review`               | `generate`        | Write directly to output, skip `patches/review/`      |
| `--group KEYWORD`           | `generate`        | Generate only groups matching a keyword               |
| `--max N`                   | `generate`        | Max patterns to generate (0 = unlimited)              |
| `--project NAME`            | `list-groups`, `generate` | Filter/set project name (xlsx sheet filter, CSV override) |
| `-v` / `--verbose`          | All commands      | Detailed progress and per-step logging                |
| `--force`                   | `publish`         | Publish even if QA fails                              |

## How It Works

```
                 ┌──────────────────────────────────────┐
                 │         Generation Pipeline           │
                 │  (per project, isolated staging)      │
                 └──────────────────────────────────────┘
                               │
         CSV / XLSX (classified optimization data)
                    │
                    ▼
          ┌─────────────────────┐
          │ Step 1: Load &      │  Reads CSV (or multi-sheet XLSX).
          │ Enrich              │  Fetches commit message + diff
          │                     │  from GitHub API via `gh` CLI.
          └─────────┬───────────┘
                    ▼
          ┌─────────────────────┐
          │ Step 2: Group       │  Groups commits by "Idea" column.
          │                     │  Ignores "correct=no" (human-rejected).
          │                     │  Threshold: configurable (≥2 default).
          └─────────┬───────────┘
                    ▼
          ┌─────────────────────┐
          │ LLM Phase A: Title  │  Determine bilingual title + see-also
          └─────────┬───────────┘
                    ▼
          ┌─────────────────────┐
          │ LLM Phase B: When   │  §4 When to apply — profile signals,
          │                     │  tool commands, typical table
          └─────────┬───────────┘
                    ▼
          ┌─────────────────────┐
          │ LLM Phase C: Why    │  §5 Why this is slow — microarchitecture
          │                     │  root cause analysis
          └─────────┬───────────┘
                    ▼
          ┌─────────────────────┐
          │ LLM Phase D: Fix    │  §6 The fix — Before/After code blocks
          │                     │  with file paths, sub-method headings
          └─────────┬───────────┘
                    ▼
          ┌─────────────────────┐
          │ LLM Phase E: Verify │  §7 Verification — tool commands,
          │                     │  test files, boundary conditions
          └─────────┬───────────┘
                    ▼
          ┌─────────────────────┐
          │ LLM Phase F: Present│  §8 Presenting + §9 Related PRs table
          │     + Related       │  (table is deterministic from commit data)
          └─────────┬───────────┘
                    ▼
          ┌─────────────────────┐
          │ QA Check (9 items)  │  License, title, profile signals,
          │                     │  architecture reasoning, code blocks,
          │                     │  verification, presenting, PR links
          └─────────┬───────────┘
                    │
                    ▼
          ┌─────────────────────┐
          │ Write to disk       │  → patches/review/ (review staging)
          │                     │  → output/patterns/ (after publish)
          │                     │  → existing_patterns/pattern_index.json
          │                     │  → triggers/from-source.md
          └─────────┬───────────┘
                    │
                    ▼
┌──────────────────────────────────────────────────────────────┐
│                  Merge Check (cross-project)                 │
│                                                              │
│  Keyword pre-filter (Jaccard) → LLM semantic judgment        │
│                                                              │
│  Results:                                                    │
│     Merge candidate  →  manually merge into existing         │
│     Suspected match  →  human review                         │
│     New pattern      →  QA → publish as standalone           │
└──────────────────────────────────────────────────────────────┘
```

### Pattern document sections

| §   | Section                        | Description                                         |
| --- | ------------------------------ | --------------------------------------------------- |
| §1  | License                        | Fixed copyright + license header                    |
| §2  | Title                          | Bilingual EN/CN pattern name                        |
| §3  | See also                       | Optional cross-reference to companion patterns       |
| §4  | When to apply / 何时适用       | Profile signals (tool→output), typical table         |
| §5  | Why this is slow / 为什么慢    | Microarchitecture-level root cause analysis          |
| §6  | The fix / 修复方式              | Sub-method headings + Before/After code blocks       |
| §7  | Verification / 验证             | Tool commands, test files, boundary conditions       |
| §8  | Presenting to the user / 如何呈现给用户 | User-facing summary of the optimization       |
| §9  | Related PRs / 关联提交          | PR table with categories and performance data        |

### QA checklist (9 items)

Each generated pattern passes through an automated QA pipeline:

1. **§1 License header** — correct format `<!-- (C) YYYY ... -->`?
2. **§2 Title format** — starts with `# Pattern:`?
3. **§4 Profile signals** — do signals start with a tool name in backticks?
4. **§5 Architecture reasoning** — does the Why section contain microarchitecture-level keywords, and is it distinct from the When section?
5. **§6 Code blocks** — do code blocks have file path + `// Before:` / `// After:` annotations?
6. **§7 Verification** — at least 2 of: tool command, test file, boundary condition
7. **§8 Presenting length** — 3–5 sentences?
8. **§9 PR links** — are PR links present in the table?
9. **§9 No duplicates** — no duplicate PRs in the table?

### Index and trigger tables

Two cross-reference files are maintained automatically:

- **`existing_patterns/pattern_index.json`** — maps each pattern filename to its title, category, PR IDs, URLs, and sub-methods. Grows append-only as patterns are merged/updated. Used by `merge-check` for cross-referencing.
- **`triggers/from-source.md`** — signal → pattern reverse index. Each profile signal from §4 gets a row so you can look up which optimization pattern applies when you see a specific tool output. Example: seeing `perf annotate` show `fence.i` → look up this table → find "Memory barrier / fence overhead reduction" pattern.

## Input Format

The input CSV/XLSX follows a simple schema (compatible with [rv-optkb-tool](https://github.com/YuanshengClaw/rv-optkb-tool)'s `csv-review/main.py` output). Required columns:

| Column      | Description                                         |
| ----------- | --------------------------------------------------- |
| `Commit URL` | Full GitHub commit URL                              |
| `Idea`      | Optimization category (→ pattern group)              |
| `Thought`   | AI-extracted optimization technique description      |
| `Correct?`  | Human review verdict: "yes" or "no" (rows marked "no" are filtered out) |
| `Why`       | Optional annotation for rejected entries             |

For XLSX input, each sheet is treated as a separate project (sheet name = project name).

## Project Structure

```
pattern-extractor/
├── config.json                  # LLM + project configuration
├── requirements.txt             # Python dependencies (openai)
├── scripts/
│   ├── cli.py                   # Unified CLI entry point
│   ├── csv_loader.py            # CSV/XLSX reader + Idea grouping
│   ├── commit_fetcher.py        # GitHub commit enrichment via `gh` CLI
│   ├── pr_grouping.py           # Group stats, sub-clustering, PR extraction
│   ├── pattern_generator.py     # Multi-phase LLM pattern generation (6 phases)
│   ├── pattern_writer.py        # Pattern .md writer + index + trigger table
│   ├── pattern_qa.py            # 9-item automated QA checklist
│   ├── merge_check.py           # New vs. existing pattern comparison (keyword + LLM)
│   ├── diff_parser.py           # Unified diff → Before/After pairs
│   ├── llm_client.py            # OpenAI-compatible LLM client wrapper
│   └── log_util.py              # Per-step file + terminal logger
├── prompts/                     # LLM phase prompt templates
│   ├── phase_title.txt          # Phase A: bilingual title
│   ├── phase_when.txt           # Phase B: When to apply
│   ├── phase_why.txt            # Phase C: Why this is slow
│   ├── phase_fix.txt            # Phase D: The fix (code blocks)
│   ├── phase_verify.txt         # Phase E: Verification
│   ├── phase_present.txt        # Phase F: Presenting
│   └── merge_judge.txt          # merge-check: LLM semantic comparison prompt
├── output/
│   └── patterns/                # Final published pattern .md files
├── patches/
│   └── review/                  # Review-staging area (before publish)
├── existing_patterns/
│   └── pattern_index.json       # Cross-reference index (grows append-only)
├── triggers/
│   └── from-source.md           # Signal → pattern matching table
└── test_data/                   # Sample data (classified.json + example .xlsx input)
```

## Examples

### Generate a single pattern from CSV

```bash
python3 -m scripts.cli generate \
    -i openjdk_ideas.csv \
    --project OpenJDK \
    --group "fence" \
    -o output/patterns/ \
    -v
```

### Review and publish a pattern

```bash
# Patterns are written to patches/review/ by default
ls patches/review/

# QA before publishing
python3 -m scripts.cli qa -p patches/review/fence-memory-barrier-reduction.md

# Publish to output/patterns/
python3 -m scripts.cli publish \
    -f patches/review/fence-memory-barrier-reduction.md \
    -o output/patterns/
```

### Cross-project merge check

```bash
# Generate new patterns in isolated staging directory
python3 -m scripts.cli generate -i openssl.csv -o staging/patterns/ --no-review

# Compare against existing published patterns
python3 -m scripts.cli merge-check \
    --existing output/patterns/ \
    --new staging/patterns/ \
    -v

# Output example:
#    Merge candidates (high confidence):
#     existing: fence-memory-barrier-reduction.md — "Memory barrier / fence overhead reduction"
#     new:      riscv-fence-membar-opt.md — "RISC-V fence/membar optimization"
#     → Merge OpenSSL content into existing pattern
#
#    New patterns (no match found):
#     aes-gcm-vector-acceleration.md
```

### Offline mode (no GitHub API)

```bash
python3 -m scripts.cli list-groups -i ideas.csv --skip-fetch
python3 -m scripts.cli generate -i ideas.csv --skip-fetch -o output/patterns/
```

## Related

- [rv-optkb-tool](https://github.com/YuanshengClaw/rv-optkb-tool) — RISC-V optimization knowledge extraction (Patch → Thought → Idea). Its `csv-review/main.py` output is a compatible input for this tool.
- The **EoK 3-layer knowledge model**: Patch → Thought → Idea → **Pattern** (this tool adds the fourth layer: structured optimization pattern documents).
