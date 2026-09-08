import json
import urllib.request

req = urllib.request.Request(
    "https://api.github.com/repos/tiaohu52-netizen/lhgp/actions/runs?branch=main&per_page=3",
    headers={"Accept": "application/vnd.github+json"},
)
data = json.loads(urllib.request.urlopen(req, timeout=30).read())
print("total runs:", data["total_count"])
for r in data["workflow_runs"][:3]:
    print(
        f"  {r['name']:10s} | {r['head_sha'][:7]} | {r['event']:9s} | "
        f"{r['status']:8s} | {r['conclusion'] or 'pending':10s} | {r['created_at']}"
    )
    # also fetch jobs to see matrix results
