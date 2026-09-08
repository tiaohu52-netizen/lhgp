"""Poll GitHub Actions for the latest main HEAD."""

import json
import sys
import urllib.request

sha = sys.argv[1] if len(sys.argv) > 1 else "4cc4f98e"
url = f"https://api.github.com/repos/tiaohu52-netizen/lhgp/actions/runs?head_sha={sha}&per_page=10"
req = urllib.request.Request(
    url,
    headers={"Accept": "application/vnd.github+json", "User-Agent": "mavis-check"},
)
with urllib.request.urlopen(req, timeout=15) as r:
    data = json.loads(r.read())
runs = data.get("workflow_runs", [])
if not runs:
    print("NO_RUNS")
else:
    for run in runs[:5]:
        print(run["name"], run["status"], run["conclusion"], run["html_url"])
