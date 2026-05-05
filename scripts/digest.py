#!/usr/bin/env python3
"""
Generate a lifetime + weekly git activity digest as an SVG.

Headline numbers are lifetime totals (all-time commits, repos with activity,
PRs merged). Languages are computed from total code bytes across all owned,
non-fork repos. "This week" is a smaller freshness indicator. PEAK and LAST
reflect the past 7 days.

Required env vars:
    STATS_TOKEN  - GitHub PAT with read-only 'repo' scope
    GITHUB_USER  - GitHub username (e.g. Hea092024)
"""

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from collections import Counter

import requests

USER = os.environ.get("GITHUB_USER", "").strip()
TOKEN = os.environ.get("STATS_TOKEN", "").strip()

if not USER or not TOKEN:
    sys.stderr.write("ERROR: GITHUB_USER and STATS_TOKEN env vars are required\n")
    sys.exit(1)

GRAPHQL_URL = "https://api.github.com/graphql"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
WINDOW_DAYS = 7


def gql(query, variables=None, max_retries=5):
    body = {"query": query, "variables": variables or {}}
    last_response = None
    for attempt in range(max_retries):
        try:
            r = requests.post(GRAPHQL_URL, headers=HEADERS, json=body, timeout=30)
            last_response = r
            # Retry on transient server errors
            if r.status_code in (502, 503, 504) and attempt < max_retries - 1:
                time.sleep(min(2 ** attempt, 8))  # 1, 2, 4, 8, 8
                continue
            r.raise_for_status()
            payload = r.json()
            if "errors" in payload:
                sys.stderr.write(f"GraphQL errors: {payload['errors']}\n")
                sys.exit(1)
            return payload["data"]
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if attempt < max_retries - 1:
                time.sleep(min(2 ** attempt, 8))
                continue
            raise
    sys.stderr.write(
        f"GraphQL request failed after {max_retries} attempts: "
        f"HTTP {last_response.status_code if last_response else '?'}\n"
    )
    sys.exit(1)


def fetch_user_and_repos(user):
    """List the user's owned (non-fork) repos via REST API.

    REST is more reliable than GraphQL for bulk listing — GitHub's GraphQL
    backend is known to return 502 (query timeout) on user.repositories(first:100)
    queries. We use REST here, then fall back to GraphQL for per-repo stats.
    """
    rest_headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    # /user returns the authenticated user — gives us node_id (the GraphQL ID)
    # needed later to filter commits by author in GraphQL.
    r = requests.get("https://api.github.com/user", headers=rest_headers, timeout=30)
    r.raise_for_status()
    user_data = r.json()
    user_id = user_data["node_id"]

    repos = []
    page = 1
    while True:
        r = requests.get(
            "https://api.github.com/user/repos",
            headers=rest_headers,
            params={
                "per_page": 100,
                "page": page,
                "affiliation": "owner",
            },
            timeout=30,
        )
        r.raise_for_status()
        page_data = r.json()
        if not page_data:
            break
        for repo_data in page_data:
            if repo_data.get("fork"):
                continue
            default_branch = repo_data.get("default_branch")
            repos.append({
                "name": repo_data["name"],
                "isPrivate": repo_data.get("private", False),
                "defaultBranchRef": (
                    {"name": default_branch} if default_branch else None
                ),
            })
        if len(page_data) < 100:
            break
        page += 1

    return user_id, repos


def fetch_commit_stats(owner, repo, branch, since_iso, author_id):
    """Returns (lifetime_count, recent_count, recent_commit_nodes, languages_edges).

    Raises on unrecoverable errors. Caller should NOT swallow these — letting
    them propagate ensures we never deploy partial data. The retry logic in
    gql() already absorbs transient failures.
    """
    query = """
    query($owner: String!, $repo: String!, $branch: String!,
          $since: GitTimestamp!, $authorId: ID!) {
      repository(owner: $owner, name: $repo) {
        languages(first: 10) {
          edges {
            size
            node { name }
          }
        }
        ref(qualifiedName: $branch) {
          target {
            ... on Commit {
              lifetime: history(author: { id: $authorId }) {
                totalCount
              }
              recent: history(since: $since, author: { id: $authorId }, first: 100) {
                totalCount
                nodes { committedDate }
              }
            }
          }
        }
      }
    }
    """
    data = gql(query, {
        "owner": owner, "repo": repo, "branch": branch,
        "since": since_iso, "authorId": author_id,
    })
    repo_data = data.get("repository") or {}
    lang_edges = (repo_data.get("languages") or {}).get("edges", [])
    ref = repo_data.get("ref")
    if not ref or not ref.get("target"):
        # Repo has no default branch / empty repo — legitimate, not a failure.
        return 0, 0, [], lang_edges
    target = ref["target"]
    lifetime_count = (target.get("lifetime") or {}).get("totalCount", 0)
    recent = target.get("recent") or {}
    return (
        lifetime_count,
        recent.get("totalCount", 0),
        recent.get("nodes", []),
        lang_edges,
    )


def fetch_merged_prs(user, since_iso=None):
    if since_iso:
        since_date = since_iso.split("T")[0]
        q = f"author:{user} is:pr is:merged merged:>={since_date} user:{user}"
    else:
        q = f"author:{user} is:pr is:merged user:{user}"
    query = "query($q: String!) { search(query: $q, type: ISSUE) { issueCount } }"
    return gql(query, {"q": q})["search"]["issueCount"]


def humanize_ago(dt):
    delta = datetime.now(timezone.utc) - dt
    s = int(delta.total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        m = s // 60
        return f"{m} min{'s' if m != 1 else ''} ago"
    if s < 86400:
        h = s // 3600
        return f"{h} hour{'s' if h != 1 else ''} ago"
    d = s // 86400
    return f"{d} day{'s' if d != 1 else ''} ago"


def find_peak_window(commit_times):
    if not commit_times:
        return "no recent activity"
    hour_counts = Counter(dt.hour for dt in commit_times)
    best_start, best_count = 0, -1
    for start in range(24):
        c = sum(hour_counts.get((start + i) % 24, 0) for i in range(4))
        if c > best_count:
            best_count = c
            best_start = start
    end = (best_start + 4) % 24
    base = f"{best_start:02d}:00 – {end:02d}:00"
    weekdays = sum(1 for dt in commit_times if dt.weekday() < 5)
    weekends = sum(1 for dt in commit_times if dt.weekday() >= 5)
    if weekdays >= weekends * 2:
        return f"{base} · weekdays"
    if weekends > weekdays * 2:
        return f"{base} · weekends"
    # No clear day-pattern — describe time of day instead.
    if 6 <= best_start < 12:
        return f"{base} · mornings"
    if 12 <= best_start < 17:
        return f"{base} · afternoons"
    if 17 <= best_start < 22:
        return f"{base} · evenings"
    return f"{base} · late nights"


def top_languages(counter, limit=3, min_other_pct=2):
    """Top N languages + 'Other' if remainder >= min_other_pct.

    Returns a list of (name, percent) tuples. Length is variable: typically 3
    when the user has 3 dominant languages, or 4 when there's a meaningful
    long tail. Pads to minimum 3 entries with placeholders if data is sparse.
    """
    total = sum(counter.values())
    if total == 0:
        return [("—", 0), ("—", 0), ("—", 0)]
    sorted_items = sorted(counter.items(), key=lambda x: x[1], reverse=True)
    top = []
    other = 0
    for i, (name, count) in enumerate(sorted_items):
        if i < limit:
            top.append((name, round(100 * count / total)))
        else:
            other += count
    other_pct = round(100 * other / total)
    if other_pct >= min_other_pct:
        top.append(("Other", other_pct))
    while len(top) < 3:
        top.append(("—", 0))
    return top


def render_headline(stats):
    """Lifetime headline with visual hierarchy: commits is the hero (22pt),
    repos and PRs are subordinated (18pt). All baseline-aligned at y=132."""
    # (value, label, num_font_size, num_char_w, gap_to_label)
    items = [
        (stats["lifetime_commits"], "commits", 22, 14, 8),
        (stats["lifetime_repos"], "repos", 18, 11, 6),
        (stats["lifetime_prs"], "PRs merged", 18, 11, 6),
    ]
    parts = []
    cursor = 22
    y = 132
    for num, label, num_size, num_char_w, gap in items:
        num_str = str(num)
        num_w = len(num_str) * num_char_w
        label_x = cursor + num_w + gap
        label_w = len(label) * 8
        parts.append(
            f'<text x="{cursor}" y="{y}" fill="#FBA94A" font-size="{num_size}" font-weight="700">{num}</text>'
            f'<text x="{label_x}" y="{y}" fill="#E8ECF1" font-size="13">{label}</text>'
        )
        cursor = label_x + label_w + 28
    return "\n".join(parts)


def render(stats):
    BAR_MAX = 380
    langs = stats["languages"]

    def bw(p):
        return max(0, min(BAR_MAX, int(round(BAR_MAX * p / 100))))

    headline = render_headline(stats)

    # Render language bars dynamically (3 or 4 rows depending on tail).
    lang_rows = []
    row_top = 192
    for name, pct in langs:
        text_y = row_top + 10
        lang_rows.append(
            f'<text x="22" y="{text_y}" fill="#E8ECF1" font-size="12">{name}</text>'
            f'<rect x="140" y="{row_top}" width="380" height="10" rx="2" fill="#161A21"/>'
            f'<rect x="140" y="{row_top}" width="{bw(pct)}" height="10" rx="2" fill="#4F8FFF"/>'
            f'<text x="540" y="{text_y}" fill="#8B95A7" font-size="12">{pct}%</text>'
        )
        row_top += 22
    lang_rows_str = "\n".join(lang_rows)

    # Position sections below languages with consistent gap regardless of row count.
    n = len(langs)
    last_lang_text_y = 202 + (n - 1) * 22
    week_y = last_lang_text_y + 40
    peak_y = week_y + 22
    last_y = peak_y + 22
    footer_line_y = last_y + 24
    footer_text_y = footer_line_y + 18
    svg_height = footer_text_y + 18

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="680" height="{svg_height}" viewBox="0 0 680 {svg_height}" font-family="ui-monospace, 'SF Mono', Menlo, Consolas, monospace">
<rect width="680" height="{svg_height}" rx="12" fill="#0E1116"/>
<rect x="0" y="0" width="680" height="32" rx="12" fill="#161A21"/>
<rect x="0" y="20" width="680" height="12" fill="#161A21"/>
<circle cx="22" cy="16" r="5" fill="#FF5F57"/>
<circle cx="40" cy="16" r="5" fill="#FBA94A"/>
<circle cx="58" cy="16" r="5" fill="#4F8FFF"/>
<text x="340" y="20" text-anchor="middle" fill="#8B95A7" font-size="11" letter-spacing="2">~/GIT-DIGEST</text>
<text x="22" y="62" fill="#FBA94A" font-size="13" font-weight="600">$</text>
<text x="38" y="62" fill="#E8ECF1" font-size="13">git digest --all-time</text>

<text x="22" y="98" fill="#4F8FFF" font-size="11" letter-spacing="2" font-weight="700">ALL TIME</text>
<line x1="22" y1="106" x2="658" y2="106" stroke="#252A33" stroke-width="1"/>
{headline}

<text x="22" y="172" fill="#4F8FFF" font-size="11" letter-spacing="2" font-weight="700">LANGUAGES</text>
<line x1="22" y1="180" x2="658" y2="180" stroke="#252A33" stroke-width="1"/>

{lang_rows_str}

<text x="22" y="{week_y}" fill="#4F8FFF" font-size="11" letter-spacing="2" font-weight="700">THIS WEEK</text>
<text x="120" y="{week_y}" fill="#E8ECF1" font-size="12">{stats["weekly_commits"]} commits · {stats["weekly_repos"]} repos · last 7 days</text>

<text x="22" y="{peak_y}" fill="#4F8FFF" font-size="11" letter-spacing="2" font-weight="700">PEAK</text>
<text x="120" y="{peak_y}" fill="#E8ECF1" font-size="12">{stats["peak"]}</text>

<text x="22" y="{last_y}" fill="#4F8FFF" font-size="11" letter-spacing="2" font-weight="700">LAST</text>
<text x="120" y="{last_y}" fill="#E8ECF1" font-size="12">{stats["last"]}</text>

<line x1="22" y1="{footer_line_y}" x2="658" y2="{footer_line_y}" stroke="#252A33" stroke-width="1"/>
<text x="22" y="{footer_text_y}" fill="#8B95A7" font-size="11">generated by github action · refreshed {stats["generated_at"]}</text>
</svg>'''


def main():
    since = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    user_id, repos = fetch_user_and_repos(USER)

    lifetime_commits = 0
    lifetime_repos = 0
    lang_bytes = Counter()

    weekly_commits = 0
    weekly_repos = set()
    weekly_times = []
    last_dt = None
    last_label = None

    for repo in repos:
        branch_ref = repo.get("defaultBranchRef")
        if not branch_ref:
            continue
        branch = branch_ref["name"]

        l_count, r_count, r_nodes, lang_edges = fetch_commit_stats(
            USER, repo["name"], branch, since_iso, user_id
        )

        # Lifetime language bytes from every owned repo
        for edge in lang_edges:
            lang_bytes[edge["node"]["name"]] += edge["size"]

        if l_count > 0:
            lifetime_commits += l_count
            lifetime_repos += 1

        if r_count > 0:
            weekly_commits += r_count
            weekly_repos.add(repo["name"])

        for c in r_nodes:
            try:
                dt = datetime.fromisoformat(c["committedDate"].replace("Z", "+00:00"))
            except Exception:
                continue
            weekly_times.append(dt)
            if last_dt is None or dt > last_dt:
                last_dt = dt
                last_label = "private project" if repo["isPrivate"] else repo["name"]

    lifetime_prs = fetch_merged_prs(USER, since_iso=None)

    # Sanity guard: if we have repos but zero lifetime commits, something is
    # structurally wrong (schema drift, auth issue, etc.). Aborting preserves
    # the last good digest on the branch instead of overwriting with bad data.
    if lifetime_commits == 0 and len(repos) > 0:
        sys.stderr.write(
            f"Suspicious data: 0 lifetime commits across {len(repos)} repos. "
            "Aborting deploy to preserve last good digest.\n"
        )
        sys.exit(1)

    last_str = (
        f"{humanize_ago(last_dt)} · {last_label}" if last_dt else "no recent activity"
    )

    stats = {
        "lifetime_commits": lifetime_commits,
        "lifetime_repos": lifetime_repos,
        "lifetime_prs": lifetime_prs,
        "languages": top_languages(lang_bytes),
        "weekly_commits": weekly_commits,
        "weekly_repos": len(weekly_repos),
        "peak": find_peak_window(weekly_times),
        "last": last_str,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }

    sys.stdout.write(render(stats))


if __name__ == "__main__":
    main()
