#!/usr/bin/env python3
"""
Generate a weekly git activity digest as an SVG.

Pulls aggregate stats from GitHub GraphQL API for the authenticated user's
own repositories (including private), and renders a Bloomberg-terminal styled
summary. The output SVG never exposes commit messages or private repo names.

Required env vars:
    STATS_TOKEN  - GitHub PAT with read-only 'repo' scope
    GITHUB_USER  - GitHub username (e.g. Hea092024)
"""

import os
import sys
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


def gql(query, variables=None):
    r = requests.post(
        GRAPHQL_URL,
        headers=HEADERS,
        json={"query": query, "variables": variables or {}},
        timeout=30,
    )
    r.raise_for_status()
    payload = r.json()
    if "errors" in payload:
        sys.stderr.write(f"GraphQL errors: {payload['errors']}\n")
        sys.exit(1)
    return payload["data"]


def fetch_user_and_repos(user):
    query = """
    query($user: String!) {
      user(login: $user) {
        id
        repositories(
          first: 100,
          ownerAffiliations: OWNER,
          isFork: false,
          orderBy: {field: UPDATED_AT, direction: DESC}
        ) {
          nodes {
            name
            isPrivate
            primaryLanguage { name }
            defaultBranchRef { name }
          }
        }
      }
    }
    """
    data = gql(query, {"user": user})
    u = data["user"]
    return u["id"], u["repositories"]["nodes"]


def fetch_recent_commits(owner, repo, branch, since_iso, author_id):
    query = """
    query($owner: String!, $repo: String!, $branch: String!,
          $since: GitTimestamp!, $authorId: ID!) {
      repository(owner: $owner, name: $repo) {
        ref(qualifiedName: $branch) {
          target {
            ... on Commit {
              history(since: $since, author: { id: $authorId }, first: 100) {
                nodes { committedDate }
              }
            }
          }
        }
      }
    }
    """
    try:
        data = gql(query, {
            "owner": owner, "repo": repo, "branch": branch,
            "since": since_iso, "authorId": author_id,
        })
        ref = (data.get("repository") or {}).get("ref")
        if not ref or not ref.get("target"):
            return []
        return ref["target"]["history"]["nodes"]
    except Exception as e:
        sys.stderr.write(f"warn: could not fetch {owner}/{repo}: {e}\n")
        return []


def fetch_merged_prs(user, since_iso):
    since_date = since_iso.split("T")[0]
    q = f"author:{user} is:pr is:merged merged:>={since_date} user:{user}"
    query = "query($q: String!) { search(query: $q, type: ISSUE) { issueCount } }"
    return gql(query, {"q": q})["search"]["issueCount"]


def humanize_ago(dt):
    delta = datetime.now(timezone.utc) - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        m = seconds // 60
        return f"{m} min{'s' if m != 1 else ''} ago"
    if seconds < 86400:
        h = seconds // 3600
        return f"{h} hour{'s' if h != 1 else ''} ago"
    d = seconds // 86400
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

    weekdays = sum(1 for dt in commit_times if dt.weekday() < 5)
    weekends = sum(1 for dt in commit_times if dt.weekday() >= 5)
    if weekdays >= weekends * 2:
        period = "mornings" if best_start < 12 else "afternoons" if best_start < 17 else "evenings"
        suffix = f"weekday {period}"
    elif weekends > weekdays * 2:
        suffix = "weekends"
    else:
        suffix = "across the week"
    return f"{best_start:02d}:00 – {end:02d}:00 · {suffix}"


def top_languages(lang_counter, limit=2):
    total = sum(lang_counter.values())
    if total == 0:
        return [("—", 0), ("—", 0), ("—", 0)]
    sorted_langs = sorted(lang_counter.items(), key=lambda x: x[1], reverse=True)
    top = []
    other_count = 0
    for i, (name, count) in enumerate(sorted_langs):
        if i < limit:
            top.append((name, round(100 * count / total)))
        else:
            other_count += count
    if other_count > 0:
        top.append(("Other", round(100 * other_count / total)))
    while len(top) < 3:
        top.append(("—", 0))
    # fix rounding so total <= 100
    return top[:3]


def render(stats):
    bar_max = 380
    langs = stats["languages"]

    def bw(p):
        return max(0, min(bar_max, int(round(bar_max * p / 100))))

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="680" height="350" viewBox="0 0 680 350" font-family="ui-monospace, 'SF Mono', Menlo, Consolas, monospace">
<rect width="680" height="350" rx="12" fill="#0E1116"/>
<rect x="0" y="0" width="680" height="32" rx="12" fill="#161A21"/>
<rect x="0" y="20" width="680" height="12" fill="#161A21"/>
<circle cx="22" cy="16" r="5" fill="#FF5F57"/>
<circle cx="40" cy="16" r="5" fill="#FBA94A"/>
<circle cx="58" cy="16" r="5" fill="#4F8FFF"/>
<text x="340" y="20" text-anchor="middle" fill="#8B95A7" font-size="11" letter-spacing="2">~/GIT-DIGEST</text>
<text x="22" y="62" fill="#FBA94A" font-size="13" font-weight="600">$</text>
<text x="38" y="62" fill="#E8ECF1" font-size="13">git digest --weekly</text>

<text x="22" y="98" fill="#4F8FFF" font-size="11" letter-spacing="2" font-weight="700">THIS WEEK</text>
<line x1="22" y1="106" x2="658" y2="106" stroke="#252A33" stroke-width="1"/>
<text x="22" y="132" fill="#FBA94A" font-size="22" font-weight="700">{stats["commits"]}</text>
<text x="80" y="132" fill="#E8ECF1" font-size="13">commits</text>
<text x="180" y="132" fill="#FBA94A" font-size="22" font-weight="700">{stats["repos"]}</text>
<text x="210" y="132" fill="#E8ECF1" font-size="13">repos</text>
<text x="278" y="132" fill="#FBA94A" font-size="22" font-weight="700">{stats["prs"]}</text>
<text x="308" y="132" fill="#E8ECF1" font-size="13">PRs merged</text>

<text x="22" y="172" fill="#4F8FFF" font-size="11" letter-spacing="2" font-weight="700">LANGUAGES</text>
<line x1="22" y1="180" x2="658" y2="180" stroke="#252A33" stroke-width="1"/>

<text x="22" y="202" fill="#E8ECF1" font-size="12">{langs[0][0]}</text>
<rect x="140" y="192" width="380" height="10" rx="2" fill="#161A21"/>
<rect x="140" y="192" width="{bw(langs[0][1])}" height="10" rx="2" fill="#4F8FFF"/>
<text x="540" y="202" fill="#8B95A7" font-size="12">{langs[0][1]}%</text>

<text x="22" y="224" fill="#E8ECF1" font-size="12">{langs[1][0]}</text>
<rect x="140" y="214" width="380" height="10" rx="2" fill="#161A21"/>
<rect x="140" y="214" width="{bw(langs[1][1])}" height="10" rx="2" fill="#4F8FFF"/>
<text x="540" y="224" fill="#8B95A7" font-size="12">{langs[1][1]}%</text>

<text x="22" y="246" fill="#E8ECF1" font-size="12">{langs[2][0]}</text>
<rect x="140" y="236" width="380" height="10" rx="2" fill="#161A21"/>
<rect x="140" y="236" width="{bw(langs[2][1])}" height="10" rx="2" fill="#4F8FFF"/>
<text x="540" y="246" fill="#8B95A7" font-size="12">{langs[2][1]}%</text>

<text x="22" y="282" fill="#4F8FFF" font-size="11" letter-spacing="2" font-weight="700">PEAK</text>
<text x="80" y="282" fill="#E8ECF1" font-size="12">{stats["peak"]}</text>
<text x="22" y="304" fill="#4F8FFF" font-size="11" letter-spacing="2" font-weight="700">LAST</text>
<text x="80" y="304" fill="#E8ECF1" font-size="12">{stats["last"]}</text>

<line x1="22" y1="324" x2="658" y2="324" stroke="#252A33" stroke-width="1"/>
<text x="22" y="342" fill="#8B95A7" font-size="11">generated by github action · refreshed {stats["generated_at"]}</text>
</svg>'''


def main():
    since = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    user_id, repos = fetch_user_and_repos(USER)

    total_commits = 0
    repos_with_commits = set()
    lang_commit_count = Counter()
    all_commit_times = []
    last_commit_dt = None
    last_commit_label = None

    for repo in repos:
        branch_ref = repo.get("defaultBranchRef")
        if not branch_ref:
            continue
        branch = branch_ref["name"]
        commits = fetch_recent_commits(USER, repo["name"], branch, since_iso, user_id)
        if not commits:
            continue

        total_commits += len(commits)
        repos_with_commits.add(repo["name"])

        primary_lang = repo.get("primaryLanguage")
        if primary_lang:
            lang_commit_count[primary_lang["name"]] += len(commits)

        for c in commits:
            try:
                dt = datetime.fromisoformat(c["committedDate"].replace("Z", "+00:00"))
            except Exception:
                continue
            all_commit_times.append(dt)
            if last_commit_dt is None or dt > last_commit_dt:
                last_commit_dt = dt
                last_commit_label = "private project" if repo["isPrivate"] else repo["name"]

    prs = fetch_merged_prs(USER, since_iso)

    if last_commit_dt:
        last_str = f"{humanize_ago(last_commit_dt)} · {last_commit_label}"
    else:
        last_str = "no recent activity"

    stats = {
        "commits": total_commits,
        "repos": len(repos_with_commits),
        "prs": prs,
        "languages": top_languages(lang_commit_count),
        "peak": find_peak_window(all_commit_times),
        "last": last_str,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }

    sys.stdout.write(render(stats))


if __name__ == "__main__":
    main()
