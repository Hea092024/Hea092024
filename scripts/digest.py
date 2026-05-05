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
            defaultBranchRef { name }
            languages(first: 10) {
              edges {
                size
                node { name }
              }
            }
          }
        }
      }
    }
    """
    data = gql(query, {"user": user})
    u = data["user"]
    return u["id"], u["repositories"]["nodes"]


def fetch_commit_stats(owner, repo, branch, since_iso, author_id):
    """Returns (lifetime_count, recent_count, recent_commit_nodes)."""
    query = """
    query($owner: String!, $repo: String!, $branch: String!,
          $since: GitTimestamp!, $authorId: ID!) {
      repository(owner: $owner, name: $repo) {
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
    try:
        data = gql(query, {
            "owner": owner, "repo": repo, "branch": branch,
            "since": since_iso, "authorId": author_id,
        })
        ref = (data.get("repository") or {}).get("ref")
        if not ref or not ref.get("target"):
            return 0, 0, []
        target = ref["target"]
        lifetime_count = (target.get("lifetime") or {}).get("totalCount", 0)
        recent = target.get("recent") or {}
        return (
            lifetime_count,
            recent.get("totalCount", 0),
            recent.get("nodes", []),
        )
    except Exception as e:
        sys.stderr.write(f"warn: could not fetch {owner}/{repo}: {e}\n")
        return 0, 0, []


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


def top_three(counter, limit=2):
    """Top N + 'Other', padded to 3 entries."""
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
    if other > 0:
        top.append(("Other", round(100 * other / total)))
    while len(top) < 3:
        top.append(("—", 0))
    return top[:3]


def render_headline(stats):
    """Dynamically position the all-time headline numbers + labels."""
    char_w_big = 14   # monospace font-size 22, weight 700
    char_w_lbl = 8    # monospace font-size 13
    items = [
        (stats["lifetime_commits"], "commits"),
        (stats["lifetime_repos"], "repos"),
        (stats["lifetime_prs"], "PRs merged"),
    ]
    parts = []
    cursor = 22
    for num, label in items:
        num_str = str(num)
        num_w = len(num_str) * char_w_big
        label_x = cursor + num_w + 8
        label_w = len(label) * char_w_lbl
        parts.append(
            f'<text x="{cursor}" y="132" fill="#FBA94A" font-size="22" font-weight="700">{num}</text>'
            f'<text x="{label_x}" y="132" fill="#E8ECF1" font-size="13">{label}</text>'
        )
        cursor = label_x + label_w + 30
    return "\n".join(parts)


def render(stats):
    BAR_MAX = 380
    langs = stats["languages"]

    def bw(p):
        return max(0, min(BAR_MAX, int(round(BAR_MAX * p / 100))))

    headline = render_headline(stats)

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="680" height="390" viewBox="0 0 680 390" font-family="ui-monospace, 'SF Mono', Menlo, Consolas, monospace">
<rect width="680" height="390" rx="12" fill="#0E1116"/>
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

<text x="22" y="286" fill="#4F8FFF" font-size="11" letter-spacing="2" font-weight="700">THIS WEEK</text>
<text x="120" y="286" fill="#E8ECF1" font-size="12">{stats["weekly_commits"]} commits · {stats["weekly_repos"]} repos · last 7 days</text>

<text x="22" y="308" fill="#4F8FFF" font-size="11" letter-spacing="2" font-weight="700">PEAK</text>
<text x="120" y="308" fill="#E8ECF1" font-size="12">{stats["peak"]}</text>

<text x="22" y="330" fill="#4F8FFF" font-size="11" letter-spacing="2" font-weight="700">LAST</text>
<text x="120" y="330" fill="#E8ECF1" font-size="12">{stats["last"]}</text>

<line x1="22" y1="354" x2="658" y2="354" stroke="#252A33" stroke-width="1"/>
<text x="22" y="372" fill="#8B95A7" font-size="11">generated by github action · refreshed {stats["generated_at"]}</text>
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
        # Lifetime language bytes from every owned repo, regardless of recency.
        for edge in repo.get("languages", {}).get("edges", []):
            lang_bytes[edge["node"]["name"]] += edge["size"]

        branch_ref = repo.get("defaultBranchRef")
        if not branch_ref:
            continue
        branch = branch_ref["name"]

        l_count, r_count, r_nodes = fetch_commit_stats(
            USER, repo["name"], branch, since_iso, user_id
        )

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

    last_str = (
        f"{humanize_ago(last_dt)} · {last_label}" if last_dt else "no recent activity"
    )

    stats = {
        "lifetime_commits": lifetime_commits,
        "lifetime_repos": lifetime_repos,
        "lifetime_prs": lifetime_prs,
        "languages": top_three(lang_bytes),
        "weekly_commits": weekly_commits,
        "weekly_repos": len(weekly_repos),
        "peak": find_peak_window(weekly_times),
        "last": last_str,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }

    sys.stdout.write(render(stats))


if __name__ == "__main__":
    main()
