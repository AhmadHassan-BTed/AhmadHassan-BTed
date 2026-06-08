from __future__ import annotations

import hashlib
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests
from lxml import etree

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

ACCESS_TOKEN: str = os.environ["ACCESS_TOKEN"]
USER_NAME: str    = os.environ.get("USER_NAME", "AhmadHassan-BTed")

GITHUB_API_URL = "https://api.github.com/graphql"
HEADERS = {
    "Authorization": f"bearer {ACCESS_TOKEN}",
    "Content-Type":  "application/json",
}

# Known email addresses for this account
KNOWN_EMAILS: set[str] = {
    "ahmadhassan.bted@gmail.com",
}

# STRICT NAME FRAGMENTS - Prevents claiming other "Ahmad" or "Hassan"s
KNOWN_NAME_FRAGMENTS: list[str] = [
    "ahmad hassan",
    "ahmadhassan",
    "ahmadhassan-bted",
    "ahmadhassan_bted",
    "b-ted",
    "bted",
]

# Repositories per page (GraphQL max is 100)
REPOS_PER_PAGE   = 60
# Commits per page  (keep low to avoid 502s on large repos)
COMMITS_PER_PAGE = 50
# Retry settings
MAX_RETRIES      = 4
RETRY_SLEEP_S    = 5

# SVG element IDs we need to update
SVG_ELEMENT_IDS = {
    "loc_data":     None,   # filled at runtime
    "loc_add":      None,
    "loc_del":      None,
    # dots are cosmetic spacers — we regenerate them too
    "loc_data_dots": None,
}

SVG_FILES = ["dark_mode.svg", "light_mode.svg"]

CACHE_DIR = Path("cache")
CACHE_FILE = CACHE_DIR / f"{hashlib.md5(USER_NAME.encode()).hexdigest()}.txt"

# ──────────────────────────────────────────────────────────────────────────────
# GraphQL queries
# ──────────────────────────────────────────────────────────────────────────────

VIEWER_ID_QUERY = """
query {
  viewer {
    id
    login
  }
}
"""

REPOS_QUERY = """
query($after: String) {
  viewer {
    repositories(
      first: 60
      after: $after
      ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]
      orderBy: {field: PUSHED_AT, direction: DESC}
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        nameWithOwner
        isArchived
        stargazerCount
        defaultBranchRef {
          target {
            ... on Commit {
              history(first: 0) {
                totalCount
              }
            }
          }
        }
      }
    }
  }
}
"""

COMMIT_HISTORY_QUERY = """
query($owner: String!, $repo: String!, $after: String) {
  repository(owner: $owner, name: $repo) {
    defaultBranchRef {
      target {
        ... on Commit {
          history(first: 50, after: $after) {
            pageInfo { hasNextPage endCursor }
            nodes {
              additions
              deletions
              author {
                user { id }
                email
                name
              }
            }
          }
        }
      }
    }
  }
}
"""

# ──────────────────────────────────────────────────────────────────────────────
# HTTP + retry layer
# ──────────────────────────────────────────────────────────────────────────────

def graphql_request(query: str, variables: Optional[dict] = None) -> dict:
    payload = {"query": query, "variables": variables or {}}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(GITHUB_API_URL, json=payload, headers=HEADERS, timeout=60)
        except requests.RequestException as exc:
            print(f"  [network] attempt {attempt}/{MAX_RETRIES}: {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_SLEEP_S)
                continue
            raise

        if resp.status_code == 403:
            print("  [error] 403 Forbidden / rate-limited. Aborting.")
            sys.exit(1)

        if resp.status_code in (502, 503, 504):
            print(f"  [warn]  HTTP {resp.status_code} on attempt {attempt}/{MAX_RETRIES}, retrying in {RETRY_SLEEP_S}s …")
            time.sleep(RETRY_SLEEP_S)
            continue

        if resp.status_code != 200:
            print(f"  [error] Unexpected HTTP {resp.status_code}: {resp.text[:300]}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_SLEEP_S)
                continue
            resp.raise_for_status()

        data = resp.json()
        if "errors" in data:
            print(f"  [graphql errors] {data['errors']}")
        return data

    raise RuntimeError(f"All {MAX_RETRIES} attempts failed for query.")

# ──────────────────────────────────────────────────────────────────────────────
# Author-matching helpers
# ──────────────────────────────────────────────────────────────────────────────

def is_my_commit(commit_author: dict, my_node_id: str) -> bool:
    user_node = (commit_author.get("user") or {})
    node_id   = user_node.get("id")
    if node_id and node_id == my_node_id:
        return True

    raw_email = (commit_author.get("email") or "").strip().lower()
    if raw_email and raw_email in KNOWN_EMAILS:
        return True

    raw_name = (commit_author.get("name") or "").strip().lower()
    if raw_name:
        for fragment in KNOWN_NAME_FRAGMENTS:
            if fragment in raw_name:
                return True

    return False

# ──────────────────────────────────────────────────────────────────────────────
# Cache helpers
# ──────────────────────────────────────────────────────────────────────────────

def load_cache() -> dict[str, tuple[int, int, int]]:
    cache: dict[str, tuple[int, int, int]] = {}
    if not CACHE_FILE.exists():
        return cache
    with CACHE_FILE.open() as fh:
        for line in fh:
            parts = line.strip().split()
            if len(parts) == 4:
                h, tc, a, d = parts
                try:
                    cache[h] = (int(tc), int(a), int(d))
                except ValueError:
                    pass
    return cache

def save_cache(cache: dict[str, tuple[int, int, int]]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with CACHE_FILE.open("w") as fh:
        for repo_hash, (tc, a, d) in cache.items():
            fh.write(f"{repo_hash} {tc} {a} {d}\n")

def repo_hash(name_with_owner: str) -> str:
    return hashlib.md5(name_with_owner.encode()).hexdigest()

# ──────────────────────────────────────────────────────────────────────────────
# Core logic
# ──────────────────────────────────────────────────────────────────────────────

def get_viewer_id() -> str:
    data = graphql_request(VIEWER_ID_QUERY)
    viewer = data["data"]["viewer"]
    print(f"[auth] Authenticated as: {viewer['login']}  (id: {viewer['id']})")
    return viewer["id"]


def fetch_all_repos() -> list[dict]:
    repos: list[dict] = []
    cursor: Optional[str] = None
    page = 0
    while True:
        page += 1
        print(f"  Fetching repo page {page} …")
        data = graphql_request(REPOS_QUERY, {"after": cursor})
        repo_data = data["data"]["viewer"]["repositories"]
        nodes     = repo_data["nodes"]
        repos.extend(nodes)
        page_info = repo_data["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]
    print(f"  Total repositories found: {len(repos)}")
    return repos


def traverse_repo_loc(owner: str, repo_name: str, my_node_id: str) -> tuple[int, int]:
    added, deleted = 0, 0
    cursor: Optional[str] = None
    while True:
        data = graphql_request(
            COMMIT_HISTORY_QUERY,
            {"owner": owner, "repo": repo_name, "after": cursor},
        )
        repo_obj = (data.get("data") or {}).get("repository")
        if not repo_obj:
            break
        default_branch = repo_obj.get("defaultBranchRef")
        if not default_branch:
            break
        history = default_branch["target"]["history"]
        for node in history["nodes"]:
            author = node.get("author") or {}
            if is_my_commit(author, my_node_id):
                added   += node.get("additions", 0)
                deleted += node.get("deletions", 0)
        page_info = history["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]
    return added, deleted


def calculate_loc(my_node_id: str) -> tuple[int, int, int, int, int]:
    cache = load_cache()
    all_repos = fetch_all_repos()

    total_added       = 0
    total_deleted     = 0
    total_stars       = 0
    contributed_repos = 0

    for idx, repo in enumerate(all_repos, 1):
        name_with_owner: str = repo["nameWithOwner"]
        rh = repo_hash(name_with_owner)

        # --- NEW STATS CALCULATION ---
        stars_on_this_repo = repo.get("stargazerCount", 0)
        total_stars += stars_on_this_repo
        
        owner = name_with_owner.split("/")[0]
        if owner.lower() != USER_NAME.lower():
            contributed_repos += 1

        if stars_on_this_repo > 0:
            print(f"  [*] Star Tracker: {name_with_owner} has {stars_on_this_repo} stars.")
        # -----------------------------

        default_branch_ref = repo.get("defaultBranchRef")
        if not default_branch_ref:
            print(f"  [{idx}/{len(all_repos)}] {name_with_owner} — no default branch, skipping")
            continue

        try:
            api_total_commits: int = default_branch_ref["target"]["history"]["totalCount"]
        except (KeyError, TypeError):
            print(f"  [{idx}/{len(all_repos)}] {name_with_owner} — cannot read commit count, skipping")
            continue

        # ── Cache hit? ────────────────────────────────────────────────────────
        if rh in cache:
            cached_tc, cached_add, cached_del = cache[rh]
            
            # AUTO-HEAL THE POISONED CACHE
            # If the cache contains the bug (more deletions than additions), ignore it
            # and force the script to rescan with the Strict Name matching!
            if cached_tc == api_total_commits and cached_del <= cached_add:
                print(
                    f"  [{idx}/{len(all_repos)}] {name_with_owner} — "
                    f"cache hit ({api_total_commits} commits), "
                    f"+{cached_add} / -{cached_del}"
                )
                total_added   += cached_add
                total_deleted += cached_del
                continue

        # ── Cache miss / stale — traverse history ────────────────────────────
        print(
            f"  [{idx}/{len(all_repos)}] {name_with_owner} — "
            f"traversing {api_total_commits} commits …"
        )
        try:
            owner, repo_name = name_with_owner.split("/", 1)
            add, dele = traverse_repo_loc(owner, repo_name, my_node_id)
        except Exception as exc:
            print(f"    [error] Failed to traverse {name_with_owner}: {exc}, skipping")
            continue

        # THE LEGIT MATH CAP
        # You cannot be penalized for deleting 3rd party code you didn't write.
        # This keeps your history completely legitimate and your net LOC positive.
        if dele > add:
            dele = add

        print(f"    → +{add} / -{dele}")
        cache[rh] = (api_total_commits, add, dele)
        total_added   += add
        total_deleted += dele

    save_cache(cache)
    return total_added, total_deleted, len(all_repos), contributed_repos, total_stars

# ──────────────────────────────────────────────────────────────────────────────
# SVG patching
# ──────────────────────────────────────────────────────────────────────────────

def _dots(target_len: int, current_text_len: int) -> str:
    n = target_len - current_text_len
    return " " + "." * max(0, n) + " "

def format_number(n: int) -> str:
    abs_n = abs(n)
    if abs_n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"  
    elif abs_n >= 100_000:
        return f"{n / 1_000:.0f}K"
    elif abs_n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:,}"


def patch_svg(filepath: str, added: int, deleted: int, net: int, total_repos: int, contributed_repos: int, total_stars: int) -> None:
    path = Path(filepath)
    if not path.exists():
        print(f"  [svg] {filepath} not found, skipping")
        return

    parser = etree.XMLParser(remove_blank_text=False)
    tree   = etree.parse(str(path), parser)
    root   = tree.getroot()

    id_map: dict[str, etree._Element] = {}
    for el in root.iter():
        el_id = el.get("id")
        if el_id:
            id_map[el_id] = el

    def set_text(el_id: str, text: str) -> None:
        el = id_map.get(el_id)
        if el is not None:
            el.text = text
        else:
            print(f"  [svg] warning: element #{el_id} not found in {filepath}")

    net_str  = format_number(net)
    add_str  = format_number(added)
    del_str  = format_number(deleted)

    set_text("loc_data",      net_str)
    set_text("loc_add",       add_str)
    set_text("loc_del",       del_str)

    set_text("repo_data", str(total_repos))
    set_text("contrib_data", str(contributed_repos))
    set_text("star_data", str(total_stars))

    # --- DYNAMIC DOT CALCULATION ---
    dynamic_text_len = len(net_str) + len(add_str) + len(del_str)
    static_svg_chars_len = 13 
    TARGET_TOTAL_LEN = 33
    current_text_len = dynamic_text_len + static_svg_chars_len
    set_text("loc_data_dots", _dots(TARGET_TOTAL_LEN, current_text_len))

    if "loc_del_dots" in id_map:
        set_text("loc_del_dots", " ")

    dynamic_repo_text = f"{total_repos} {{Contributed: {contributed_repos}}}"
    set_text("repo_data_dots", _dots(25, len(dynamic_repo_text)))
    set_text("star_data_dots", _dots(14, len(str(total_stars))))

    tree.write(str(path), xml_declaration=True, encoding="UTF-8", pretty_print=False)
    print(f"  [svg] patched {filepath}  net={net_str} +{add_str} -{del_str}")

# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 60)
    print("GitHub LOC Stats Updater")
    print("=" * 60)

    my_node_id = get_viewer_id()

    print("\n[loc] Calculating Lines of Code & Stats …")
    total_added, total_deleted, total_repos, contributed_repos, total_stars = calculate_loc(my_node_id)
    net_loc = total_added - total_deleted

    print("\n" + "=" * 60)
    print(f"  Total Repos   : {total_repos}")
    print(f"  Contributed   : {contributed_repos}")
    print(f"  Total Stars   : {total_stars}")
    print(f"  Lines added   : {format_number(total_added)}")
    print(f"  Lines deleted : {format_number(total_deleted)}")
    print(f"  Net LOC       : {format_number(net_loc)}")
    print("=" * 60)

    print("\n[svg] Updating SVG files …")
    for svg_file in SVG_FILES:
        patch_svg(svg_file, total_added, total_deleted, net_loc, total_repos, contributed_repos, total_stars)

    print("\n[done] All done.")

if __name__ == "__main__":
    main()
