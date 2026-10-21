#!/usr/bin/env python3
"""Static schedule summary from `cargo furiosa-opt compile ... --dump-schedule X.json`.
Prints makespan, per-context busy cycles and top-N longest instructions with source locations.
usage: static_makespan.py schedule.json [-n 15] [--json]"""
import json, sys, collections, re
def summarize(path, n=15):
    d = json.load(open(path))
    ins = d.get("instructions", [])
    mk = max((i["lifetime"]["end"] for i in ins), default=0)
    busy = collections.Counter(); cnt = collections.Counter()
    for i in ins:
        dur = i["lifetime"]["end"] - i["lifetime"]["begin"]
        for c in (i.get("contexts") or ["?"]):
            busy[c] += dur; cnt[c] += 1
    top = sorted(ins, key=lambda i: i["lifetime"]["begin"] - i["lifetime"]["end"])[:n]
    return {"file": path, "makespan": mk, "n_instructions": len(ins), "n_tensors": len(d.get("tensors", [])),
            "contexts": {c: {"busy": busy[c], "pct": round(100 * busy[c] / mk, 1) if mk else 0, "count": cnt[c]} for c in busy},
            "top": [{"dur": i["lifetime"]["end"] - i["lifetime"]["begin"], "begin": i["lifetime"]["begin"], "tpe": i.get("tpe"),
                     "ctx": ",".join(i.get("contexts") or []), "util": (i.get("util") or {}).get("total_util"),
                     "src": (re.search(r"-->\s*(\S+:\d+)", i.get("description", "") or "") or [None, None])[1],
                     "desc": (i.get("description") or "")[:120]} for i in top]}
if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    n = int(sys.argv[sys.argv.index("-n") + 1]) if "-n" in sys.argv else 15
    s = summarize(args[0], n)
    if "--json" in sys.argv: print(json.dumps(s, indent=1)); sys.exit()
    print(f"{s['file']}: makespan={s['makespan']:,} instructions={s['n_instructions']} tensors={s['n_tensors']}")
    for c, v in sorted(s["contexts"].items(), key=lambda kv: -kv[1]["busy"]):
        print(f"  {c:16s} busy={v['busy']:>10,} ({v['pct']:5.1f}%) n={v['count']}")
    print("  top instructions:")
    for t in s["top"]:
        print(f"    {t['dur']:>9,} @{t['begin']:>9,} {t['ctx']:14s} util={t['util']} {t['src'] or ''}  {t['desc']}")
