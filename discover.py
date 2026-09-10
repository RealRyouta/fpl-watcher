import json, re, urllib.request, urllib.error
API = "https://fantasy.premierleague.com/api"
def get(path):
    req = urllib.request.Request(f"{API}/{path}", headers={"User-Agent": "Mozilla/5.0 (fpl-watcher)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)
out = {}
boot = get("bootstrap-static/")
out["top_level_keys"] = {k: type(v).__name__ for k, v in boot.items()}
els = boot["elements"]
out["element_keys"] = {k: type(v).__name__ for k, v in els[0].items()}
pat = re.compile(r"price|progress|predict|proj|likel|cost|change|transfer|threshold|rise|fall|drop", re.I)
out["interesting_keys"] = [k for k in els[0] if pat.search(k)]
top = sorted(els, key=lambda e: abs(e.get("transfers_in_event", 0) - e.get("transfers_out_event", 0)), reverse=True)[:4]
out["sample_elements_high_transfers"] = top
# non-element top-level structures that look relevant
out["other_relevant_top_level"] = {k: (v if not isinstance(v, list) else v[:3]) for k, v in boot.items()
                                   if k != "elements" and pat.search(k)}
probes = {}
for p in ["price-changes/", "price-change-predictor/", "price-change/", "prices/", "player-price-changes/",
          "price-predictor/", "element-price-changes/", "price-change-progress/"]:
    try:
        d = get(p); probes[p] = {"status": 200, "preview": json.dumps(d)[:1500]}
    except urllib.error.HTTPError as e:
        probes[p] = {"status": e.code}
    except Exception as e:
        probes[p] = {"error": str(e)[:100]}
out["endpoint_probes"] = probes
try:
    s = get(f"element-summary/{top[0]['id']}/")
    out["element_summary_keys"] = list(s.keys())
    out["element_summary_extra"] = {k: v for k, v in s.items() if k not in ("fixtures", "history", "history_past")}
except Exception as e:
    out["element_summary_error"] = str(e)
json.dump(out, open("discovery.json", "w"), indent=1, default=str)
print("done")
