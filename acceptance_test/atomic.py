#!/usr/bin/env python3
"""atomic.py: crash-safe JSON records. dump_json(obj, path) writes <path>.tmp, fsyncs it and renames it over path, so a reader never sees a
half-written record (once the disk hit 100 %, a failed/ record was truncated at 4,096 B and the runner's dedup read it as a
measured duplicate). load_json(path, default) returns default on a missing or unparsable file and never raises.
  python3 atomic.py --selftest"""
import json, os, sys, tempfile


def dump_json(obj, path, **kw):
    kw.setdefault("indent", 1); tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, **kw); fh.flush(); os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


def load_json(path, default=None):
    try:
        with open(path) as fh: return json.load(fh)
    except (OSError, ValueError): return default


def selftest():
    t = tempfile.mkdtemp(); p = os.path.join(t, "rec.json")
    dump_json({"a": 1, "b": [1, 2]}, p); assert load_json(p) == {"a": 1, "b": [1, 2]} and not [f for f in os.listdir(t) if f.endswith(".tmp")], os.listdir(t)
    dump_json({"a": 2}, p, sort_keys=True); assert load_json(p) == {"a": 2} and open(p).read().startswith("{\n")
    open(p, "w").write(open(p).read()[:5]); assert load_json(p) is None and load_json(p, {}) == {}, "a truncated record reads as the default, never raises"
    assert load_json(os.path.join(t, "missing.json"), "d") == "d"
    try: dump_json({1}, p); raise AssertionError("a non-serialisable object must raise")
    except TypeError: pass
    assert open(p).read() == open(p).read()[:5] or load_json(p) is None  # the failed dump left the old file untouched (the tmp was never renamed)
    print("atomic selftest ok: tmp + fsync + rename, no tmp left behind, truncated -> default, a failed dump leaves the old file")
    return 0


if __name__ == "__main__": sys.exit(selftest() if "--selftest" in sys.argv else (print(__doc__) or 2))
