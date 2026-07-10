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
import shutil
import subprocess
import time
from typing import Any

# PR reference in commit message: (#12345) or #12345
PR_MSG_RE = re.compile(r'#(\d{4,})')

# Default directories
DEFAULT_CACHE_DIR = "/tmp/gh_commit_cache"
DEFAULT_GIT_CACHE_DIR = "/tmp/pattern_git_cache"


def _safe_name(text: str) -> str:
    """Convert arbitrary text to a filesystem-safe name."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text.replace("://", "_"))


def _run_cmd(cmd: list[str], timeout: int = 600) -> str:
    """Run a command, return stdout. Raises RuntimeError on failure."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except FileNotFoundError as e:
        raise RuntimeError(f"Command not found: {cmd[0]}") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Command timed out: {' '.join(cmd[:3])}") from e
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed (exit {result.returncode}): {' '.join(cmd[:4])}\n"
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


class CommitFetcher:
    """Fetch commit data from GitHub API (`gh`) or generic Git repos.

    For GitHub commits: uses `gh api` (authenticated CLI).
    For non-GitHub repos: uses local `git clone --bare` + `git show`.
    """

    def __init__(self, cache_dir: str = "",
                 git_cache_dir: str = "",
                 rate_limit_delay: float = 0.2):
        self.cache_dir = cache_dir or DEFAULT_CACHE_DIR
        self.git_cache_dir = git_cache_dir or DEFAULT_GIT_CACHE_DIR
        self.rate_limit_delay = rate_limit_delay
        self._stats: dict[str, int] = {"fetched": 0, "cached": 0, "errors": 0}
        os.makedirs(self.cache_dir, exist_ok=True)

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    def _cache_path(self, owner: str, repo: str, sha: str) -> str:
        safe = f"{owner}_{repo}_{sha}.json".replace("/", "_")
        return os.path.join(self.cache_dir, safe)

    def _git_cache_path(self, repo_url: str, sha: str) -> str:
        safe = f"{_safe_name(repo_url)}_{sha}.json"
        return os.path.join(self.cache_dir, safe)

    def _read_cache(self, path: str) -> dict | None:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self._stats["cached"] += 1
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return None

    def _write_cache(self, path: str, data: dict) -> None:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except IOError:
            pass

    def _bare_repo_path(self, repo_url: str) -> str:
        safe = _safe_name(repo_url)
        return os.path.join(self.git_cache_dir, f"{safe}.git")

    def _ensure_bare_repo(self, repo_url: str) -> str:
        """Clone or update a bare git cache for the given repo URL."""
        cache_path = self._bare_repo_path(repo_url)
        os.makedirs(self.git_cache_dir, exist_ok=True)

        if os.path.isdir(cache_path):
            _run_cmd(
                ["git", "--git-dir", cache_path, "fetch", "--prune", "origin"],
                timeout=1800,
            )
        else:
            _run_cmd(
                ["git", "clone", "--bare", "--single-branch",
                 "--no-tags", repo_url, cache_path],
                timeout=1800,
            )
        return cache_path

    def _git_message(self, bare_path: str, sha: str) -> str:
        return _run_cmd(
            ["git", "--git-dir", bare_path, "log", "-1", "--format=%B", sha],
            timeout=300,
        )

    def _git_diff(self, bare_path: str, sha: str) -> str:
        return _run_cmd(
            ["git", "--git-dir", bare_path, "show", "--no-ext-diff",
             "--format=", "--patch", sha],
            timeout=300,
        )

    def fetch_commit(self, owner: str, repo: str, sha: str) -> dict | None:
        """Fetch commit data + diff via gh CLI.

        Returns dict with keys: sha, message, diff, url, stats
        or None on failure.
        """
        # Check cache
        cache_path = self._cache_path(owner, repo, sha)
        cached = self._read_cache(cache_path)
        if cached:
            return cached

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
                ["gh", "api", f"{url}", "--header", "Accept: application/vnd.github.v3.diff"],
                capture_output=True, text=True, timeout=60,
            )
            diff_text = diff_result.stdout
            if diff_result.returncode != 0:
                diff_text = ""
        except (subprocess.TimeoutExpired, FileNotFoundError):
            diff_text = ""

        commit_data = {
            "sha": commit_info.get("sha", sha),
            "message": commit_info.get("message", ""),
            "url": commit_info.get("html_url",
                                   f"https://github.com/{owner}/{repo}/commit/{sha}"),
            "diff": diff_text,
            "stats": None,
        }

        # Cache
        self._write_cache(cache_path, commit_data)
        time.sleep(self.rate_limit_delay)
        self._stats["fetched"] += 1
        return commit_data

    def fetch_commit_from_git(self, repo_url: str, sha: str) -> dict | None:
        """Fetch commit message + diff via local git bare clone.

        Args:
            repo_url: Base repository URL (e.g., https://code.ffmpeg.org/FFmpeg/FFmpeg).
            sha: Full commit SHA.

        Returns dict with keys: sha, message, diff, url, stats
        or None on failure.
        """
        # Check cache
        cache_path = self._git_cache_path(repo_url, sha)
        cached = self._read_cache(cache_path)
        if cached:
            return cached

        try:
            bare_path = self._ensure_bare_repo(repo_url)
        except RuntimeError as e:
            self._stats["errors"] += 1
            return None

        try:
            msg = self._git_message(bare_path, sha)
        except RuntimeError:
            self._stats["errors"] += 1
            return None

        try:
            diff = self._git_diff(bare_path, sha)
        except RuntimeError:
            diff = ""

        commit_data = {
            "sha": sha,
            "message": msg.strip(),
            "url": f"{repo_url.rstrip('/')}/commit/{sha}",
            "diff": diff.strip(),
            "stats": None,
        }

        self._write_cache(cache_path, commit_data)
        self._stats["fetched"] += 1
        return commit_data

    def enrich_commits(self,
                       commits: list[dict[str, Any]],
                       max_workers: int = 4) -> list[dict[str, Any]]:
        """Enrich commit dicts with message + diff from their source.

        Routing (per commit):
          - owner + repo + sha  → GitHub API (gh CLI)
          - repo_url + sha      → generic git (bare clone)
          - otherwise           → skipped

        Args:
            commits: List of partial commit dicts.
            max_workers: Not used (sequential for rate limiting).

        Returns:
            List of enriched commit dicts (order preserved).
        """
        results: list[dict[str, Any]] = []

        for c in commits:
            owner = c.get("owner", "")
            repo = c.get("repo", "")
            repo_url = c.get("repo_url", "")
            sha = c.get("sha", "")

            if not sha:
                c["message"] = c.get("message", "(missing sha)")
                c["diff"] = ""
            elif owner and repo:
                data = self.fetch_commit(owner, repo, sha)
                if data:
                    c["sha"] = data["sha"][:12]
                    c["message"] = data["message"]
                    c["diff"] = data["diff"]
                    c["url"] = data["url"]
                else:
                    c["message"] = "(fetch failed)"
                    c["diff"] = ""
            elif repo_url:
                data = self.fetch_commit_from_git(repo_url, sha)
                if data:
                    c["sha"] = data["sha"][:12]
                    c["message"] = data["message"]
                    c["diff"] = data["diff"]
                    c["url"] = data["url"]
                else:
                    c["message"] = "(fetch failed)"
                    c["diff"] = ""
            else:
                c["message"] = c.get("message", "(missing repo info)")
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
        """Clear all cached commit data and git bare clones."""
        if os.path.exists(self.cache_dir):
            shutil.rmtree(self.cache_dir)
            os.makedirs(self.cache_dir, exist_ok=True)
        if os.path.exists(self.git_cache_dir):
            shutil.rmtree(self.git_cache_dir)
            os.makedirs(self.git_cache_dir, exist_ok=True)
