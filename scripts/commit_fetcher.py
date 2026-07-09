#!/usr/bin/env python3
"""Commit data fetcher — enrich CSV rows with commit message + diff from GitHub.

CSV data only has `commit_url, idea, thought`. For pattern generation we need:
    - sha (short hash)
    - message (full commit message)
    - diff (unified diff)
    - pr_number (if applicable)

This module uses the `gh` CLI (authenticated) to fetch commit data via GitHub API.

Usage:
    fetcher = CommitFetcher()
    enriched = fetcher.enrich_commits([commit_dict, ...])
    # Each commit_dict gets: message, diff, pr_number fields added
"""

import json
import os
import re
import subprocess
import time
from typing import Any

# PR URL pattern: https://github.com/{owner}/{repo}/pull/{number}
PR_URL_RE = re.compile(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)")
# PR reference in commit message: (#12345) or #12345
PR_MSG_RE = re.compile(r'#(\d{4,})')


class CommitFetcher:
    """Fetch commit data from GitHub via `gh` CLI with caching."""

    def __init__(self, cache_dir: str = "", rate_limit_delay: float = 0.2):
        """
        Args:
            cache_dir: Directory for JSON cache (default: /tmp/gh_commit_cache/).
            rate_limit_delay: Seconds between API calls (default: 0.2).
        """
        self.cache_dir = cache_dir or "/tmp/gh_commit_cache"
        self.rate_limit_delay = rate_limit_delay
        self._stats = {"fetched": 0, "cached": 0, "errors": 0}
        os.makedirs(self.cache_dir, exist_ok=True)

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    def _cache_path(self, owner: str, repo: str, sha: str) -> str:
        safe = f"{owner}_{repo}_{sha}.json".replace("/", "_")
        return os.path.join(self.cache_dir, safe)

    def fetch_commit(self, owner: str, repo: str, sha: str) -> dict | None:
        """Fetch commit data + diff via gh CLI.

        Returns dict with keys: sha, message, diff, url, stats
        or None on failure.
        """
        # Check cache
        cache_path = self._cache_path(owner, repo, sha)
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._stats["cached"] += 1
                return data
            except (json.JSONDecodeError, IOError):
                pass  # stale cache, re-fetch

        # Fetch commit info via gh CLI
        url = f"repos/{owner}/{repo}/commits/{sha}"
        try:
            result = subprocess.run(
                ["gh", "api", url, "--jq",
                 '{sha: .sha, message: .commit.message, html_url: .html_url, '
                 'author: .commit.author.name, date: .commit.author.date}'],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                self._stats["errors"] += 1
                return None
            commit_info = json.loads(result.stdout)
        except (subprocess.TimeoutExpired, json.JSONDecodeError,
                FileNotFoundError) as e:
            self._stats["errors"] += 1
            return None

        # Fetch diff separately
        try:
            diff_result = subprocess.run(
                ["gh", "api", f"{url}", "--header", "Accept: application/vnd.github.v3.diff",
                 "--jq", "."],
                capture_output=True, text=True, timeout=60,
            )
            diff_text = diff_result.stdout if diff_result.returncode == 0 else ""
        except (subprocess.TimeoutExpired, FileNotFoundError):
            diff_text = ""

        commit_data = {
            "sha": commit_info.get("sha", sha),
            "message": commit_info.get("message", ""),
            "url": commit_info.get("html_url", f"https://github.com/{owner}/{repo}/commit/{sha}"),
            "diff": diff_text,
            "stats": None,  # could be computed from diff
        }

        # Cache
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(commit_data, f, indent=2)
        except IOError:
            pass

        # Rate limit
        time.sleep(self.rate_limit_delay)
        self._stats["fetched"] += 1
        return commit_data

    def enrich_commits(self,
                       commits: list[dict[str, Any]],
                       max_workers: int = 4) -> list[dict[str, Any]]:
        """Enrich a list of partial commit dicts with message + diff.

        Input dicts need: owner, repo, sha, url (from CSV loader).
        Output dicts add: message, diff.

        Args:
            commits: List of partial commit dicts (min: owner, repo, sha).
            max_workers: Not used (sequential for rate limiting).

        Returns:
            List of enriched commit dicts (order preserved). Failed fetches
            still included but with empty message/diff.
        """
        results: list[dict[str, Any]] = []

        for c in commits:
            owner = c.get("owner", "")
            repo = c.get("repo", "")
            sha = c.get("sha", "")

            if owner and repo and sha:
                data = self.fetch_commit(owner, repo, sha)
                if data:
                    c["sha"] = data["sha"][:12]
                    c["message"] = data["message"]
                    c["diff"] = data["diff"]
                    c["url"] = data["url"]
                else:
                    c["message"] = c.get("message", "(fetch failed)")
                    c["diff"] = ""
            else:
                c["message"] = c.get("message", "(missing owner/repo/sha)")
                c["diff"] = ""

            # Try to extract PR number
            pr_num = self._extract_pr_number(c)
            if pr_num:
                c["_pr_number"] = pr_num

            results.append(c)

        return results

    def _extract_pr_number(self, commit: dict) -> str:
        """Extract PR number like '#21248' from URL or message."""
        url = commit.get("url", "")
        m = re.search(r'/(?:pull|issues)/(\d{4,})', url)
        if m:
            return f"#{m.group(1)}"
        msg = commit.get("message", "")
        m = PR_MSG_RE.search(msg)
        if m:
            return f"#{m.group(1)}"
        sha = commit.get("sha", "")[:7]
        return f"({sha})"

    def clear_cache(self):
        """Clear all cached commit data."""
        import shutil
        if os.path.exists(self.cache_dir):
            shutil.rmtree(self.cache_dir)
            os.makedirs(self.cache_dir, exist_ok=True)
