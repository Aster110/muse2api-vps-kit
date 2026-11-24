#!/usr/bin/env python3
"""muse-meter: what did this muse2api instance actually spend? Stdlib only, SQLite.

muse2api returns usage = 0 on every /v1 response, so a gateway in front of it cannot bill
or budget from the response. This meter records the things that do move, without patching
muse2api:

  quota     the account's weekly quota % from GET /admin/accounts (cached by muse2api, free)
            and, at most every --quota-every seconds, a live read through
            POST /admin/accounts/{id}/quota (~8 s, holds muse2api's generation lock, so it is
            skipped while a task is queued or running)
  tasks     image/video tasks from GET /admin/tasks, mirrored into SQLite (no prompt text)
  requests  hourly request counts per route and status from muse2api's access log in
            journald (works with or without the Caddy front)

  sudo python3 muse_meter.py collect                       # one sample; the systemd timer runs this
  sudo python3 muse_meter.py report --days 7               # tables; --json for machines

`report` divides the work done by the weekly-quota points it consumed, which is the number
you need to price or cap usage ("about N chats per 1% of the weekly quota").
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request

SCHEMA = """
CREATE TABLE IF NOT EXISTS quota_samples(
  ts INTEGER NOT NULL, account_id TEXT NOT NULL, label TEXT, source TEXT NOT NULL,
  checked_at INTEGER NOT NULL, plan TEXT, weekly_used_pct REAL, weekly_reset TEXT,
  extra_used_pct REAL, extra_left TEXT,
  UNIQUE(account_id, checked_at));
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, kind TEXT, status TEXT, created_at INTEGER, updated_at INTEGER,
  account TEXT, elapsed REAL, queue_elapsed REAL, total_elapsed REAL, bytes INTEGER,
  media_kind TEXT, error TEXT, prompt_chars INTEGER);
CREATE TABLE IF NOT EXISTS requests(
  hour INTEGER NOT NULL, route TEXT NOT NULL, status INTEGER NOT NULL, n INTEGER NOT NULL,
  PRIMARY KEY(hour, route, status));
CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY, v TEXT);
"""
ACCESS = re.compile(r'"(?P<m>[A-Z]+) (?P<p>\S+) HTTP/[\d.]+" (?P<s>\d{3})')
WORK_ROUTES = ("chat", "responses", "image_submit", "image_sync", "image_edit", "video_submit")


def route_of(method: str, path: str) -> str:
    p = path.split("?", 1)[0]
    if p.startswith("/v1/media/"):
        return "media"
    if p.startswith("/admin"):
        return "admin"
    if method == "GET" and p.startswith("/v1/images/tasks/"):
        return "image_poll"
    if method == "GET" and p.startswith("/v1/videos/"):
        return "video_poll"
    return {
        ("POST", "/v1/chat/completions"): "chat", ("POST", "/v1/responses"): "responses",
        ("POST", "/v1/images/tasks"): "image_submit", ("POST", "/v1/images/generations"): "image_sync",
        ("POST", "/v1/images/edits"): "image_edit", ("POST", "/v1/videos"): "video_submit",
        ("POST", "/v1/videos/generations"): "video_submit", ("GET", "/v1/models"): "models",
        ("GET", "/healthz"): "health",
    }.get((method, p), "other")


def db_open(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.executescript(SCHEMA)
    return con


def get_state(con, k: str, default: str = "") -> str:
    row = con.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def set_state(con, k: str, v) -> None:
    con.execute("INSERT INTO state(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


def read_key(path: str) -> str:
    if os.environ.get("MUSE2API_KEY"):
        return os.environ["MUSE2API_KEY"].strip()
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("MUSE2API_KEY="):
                return line.split("=", 1)[1].strip().strip("'\"")
    raise SystemExit(f"MUSE2API_KEY not found in {path}")


def call(base: str, key: str, method: str, path: str, timeout: int = 30):
    req = urllib.request.Request(base.rstrip("/") + path, method=method,
                                 data=b"{}" if method == "POST" else None,
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode(errors="replace")[:200]}


def pct(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def save_quota(con, now: int, acc: dict, q: dict, source: str) -> int:
    if not isinstance(q, dict) or not q.get("found") or not q.get("checked_at"):
        return 0
    cur = con.execute(
        "INSERT OR IGNORE INTO quota_samples VALUES(?,?,?,?,?,?,?,?,?,?)",
        (now, acc["id"], acc.get("label"), source, int(q["checked_at"]), q.get("plan"),
         pct(q.get("weekly_used_pct")), q.get("weekly_reset"), pct(q.get("extra_used_pct")), q.get("extra_left")))
    return cur.rowcount


def save_tasks(con, tasks: list[dict]) -> int:
    n = 0
    for t in tasks:
        res = t.get("result") if isinstance(t.get("result"), dict) else {}
        con.execute(
            "INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "status=excluded.status, updated_at=excluded.updated_at, account=excluded.account, "
            "elapsed=excluded.elapsed, queue_elapsed=excluded.queue_elapsed, total_elapsed=excluded.total_elapsed, "
            "bytes=excluded.bytes, media_kind=excluded.media_kind, error=excluded.error",
            (t.get("id"), t.get("kind"), t.get("status"), t.get("created_at"), t.get("updated_at"),
             t.get("account"), t.get("elapsed"), t.get("queue_elapsed"), t.get("total_elapsed"),
             res.get("bytes") or res.get("size"), res.get("kind"), (t.get("error") or "")[:200] or None,
             len(t.get("prompt") or "")))
        n += 1
    return n


def journal_lines(unit: str, cursor: str, journal_file: str | None):
    if journal_file:
        with open(journal_file, encoding="utf-8") as f:
            yield from f
        return
    if not shutil.which("journalctl"):
        return
    cmd = ["journalctl", "-u", unit, "-o", "json", "--no-pager"]
    cmd += ["--after-cursor", cursor] if cursor else ["--since", "-7d"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    yield from out.stdout.splitlines()


def save_requests(con, unit: str, journal_file: str | None) -> int:
    cursor = get_state(con, "journal_cursor")
    last_us = int(get_state(con, "journal_last_us", "0") or 0)
    counts: dict[tuple, int] = {}
    seen = 0
    for line in journal_lines(unit, "" if journal_file else cursor, journal_file):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        us = int(e.get("__REALTIME_TIMESTAMP") or 0)
        if journal_file and us <= last_us:
            continue
        msg = e.get("MESSAGE")
        if isinstance(msg, list):  # journald encodes non-UTF-8 messages as byte arrays
            msg = bytes(msg).decode(errors="replace")
        m = ACCESS.search(msg or "")
        if m:
            hour = us // 1_000_000 // 3600 * 3600
            k = (hour, route_of(m["m"], m["p"]), int(m["s"]))
            counts[k] = counts.get(k, 0) + 1
            seen += 1
        last_us = max(last_us, us)
        cursor = e.get("__CURSOR") or cursor
    for (hour, route, status), n in counts.items():
        con.execute("INSERT INTO requests VALUES(?,?,?,?) ON CONFLICT(hour,route,status) DO UPDATE SET n=n+excluded.n",
                    (hour, route, status, n))
    set_state(con, "journal_cursor", cursor)
    set_state(con, "journal_last_us", last_us)
    return seen


def collect(args) -> int:
    con = db_open(args.db)
    now = int(time.time())
    key = read_key(args.key_file)
    summary = []

    st, js = call(args.base, key, "GET", "/admin/accounts")
    if st != 200:
        print(f"GET /admin/accounts -> {st}; is muse2api up and the key right?", file=sys.stderr)
        return 1
    accounts = js.get("accounts") or []
    cached = sum(save_quota(con, now, a, a.get("quota"), "cache") for a in accounts)
    summary.append(f"accounts={len(accounts)} quota_cached_new={cached}")

    st, js = call(args.base, key, "GET", f"/admin/tasks?limit={args.task_limit}")
    tasks = (js.get("tasks") or []) if st == 200 else []
    summary.append(f"tasks_seen={save_tasks(con, tasks)}")

    every = args.quota_every
    if every and every < 3600 and not args.force:
        every = 3600
        print("--quota-every raised to 3600: a live read holds the generation lock", file=sys.stderr)
    last_live = int(get_state(con, "last_live_quota", "0") or 0)
    if args.force or (every and now - last_live >= every):
        busy = [t for t in tasks if t.get("status") in ("queued", "processing")]
        if busy and not args.force:
            summary.append(f"live_quota=skipped({len(busy)} task(s) busy)")
        else:
            live = 0
            for a in accounts:
                if not a.get("enabled", True) or not a.get("cookie_count"):
                    continue
                st, q = call(args.base, key, "POST", f"/admin/accounts/{a['id']}/quota", timeout=180)
                if st == 200:
                    live += save_quota(con, now, a, q, "live")
                else:
                    summary.append(f"live_quota[{a.get('label')}]=http{st}")
            set_state(con, "last_live_quota", now)
            summary.append(f"live_quota_new={live}")

    if args.unit or args.journal_file:
        summary.append(f"access_lines={save_requests(con, args.unit, args.journal_file)}")
    set_state(con, "last_collect", now)
    con.commit()
    print("collect: " + " ".join(summary))
    return 0


def quantile(vals: list[float], q: float):
    if not vals:
        return None
    vals = sorted(vals)
    return round(vals[min(len(vals) - 1, int(q * len(vals)))], 1)


def report(args) -> int:
    con = db_open(args.db)
    since = int(time.time()) - int(args.days * 86400)
    out: dict = {"since": since, "days": args.days, "quota": [], "requests": {}, "tasks": [], "estimate": None}

    for (aid,) in con.execute("SELECT DISTINCT account_id FROM quota_samples WHERE checked_at>=?", (since,)):
        cols = "checked_at, label, plan, weekly_used_pct, weekly_reset, source"
        rows = con.execute(f"SELECT {cols} FROM quota_samples WHERE account_id=? AND checked_at>=? "
                           "ORDER BY checked_at", (aid, since)).fetchall()
        before = con.execute(f"SELECT {cols} FROM quota_samples WHERE account_id=? AND checked_at<? "
                             "ORDER BY checked_at DESC LIMIT 1", (aid, since)).fetchone()
        if before:  # the last reading before the window is the baseline
            rows.insert(0, before)
        first, last = rows[0], rows[-1]
        # a weekly reset makes the % drop; count only increases
        used = sum(max(0.0, (b[3] or 0) - (a[3] or 0)) for a, b in zip(rows, rows[1:]))
        out["quota"].append({"account": aid, "label": last[1], "plan": last[2], "weekly_used_pct": last[3],
                             "weekly_reset": last[4], "last_checked": last[0], "source": last[5],
                             "samples": len(rows), "first_pct": first[3], "first_checked": first[0],
                             "used_pct_in_window": round(used, 2)})

    for route, status, n in con.execute(
            "SELECT route, status, SUM(n) FROM requests WHERE hour>=? GROUP BY route, status", (since // 3600 * 3600,)):
        r = out["requests"].setdefault(route, {"2xx": 0, "4xx": 0, "5xx": 0, "other": 0})
        r["2xx" if 200 <= status < 300 else "4xx" if 400 <= status < 500 else "5xx" if status >= 500 else "other"] += n

    for kind, status, n in con.execute(
            "SELECT kind, status, COUNT(*) FROM tasks WHERE created_at>=? GROUP BY kind, status", (since,)):
        el = [r[0] for r in con.execute("SELECT elapsed FROM tasks WHERE created_at>=? AND kind=? AND status=? "
                                        "AND elapsed IS NOT NULL", (since, kind, status))]
        out["tasks"].append({"kind": kind, "status": status, "n": n,
                             "p50_s": quantile(el, 0.5), "p90_s": quantile(el, 0.9)})

    # Pair quota movement with the requests made between the same two quota readings
    # (hour buckets, so this is approximate at the edges).
    used = sum(q["used_pct_in_window"] for q in out["quota"])
    if used and out["quota"]:
        lo = min(q["first_checked"] for q in out["quota"]) // 3600 * 3600
        hi = max(q["last_checked"] for q in out["quota"])
        marks = ",".join("?" * len(WORK_ROUTES))
        work = con.execute(f"SELECT COALESCE(SUM(n),0) FROM requests WHERE hour>=? AND hour<=? "
                           f"AND status BETWEEN 200 AND 299 AND route IN ({marks})", (lo, hi, *WORK_ROUTES)).fetchone()[0]
        if work:
            out["estimate"] = {"work_requests": work, "weekly_pct_used": round(used, 2),
                               "requests_per_pct": round(work / used, 1), "between": [lo, hi]}

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    print(f"muse-meter report, last {args.days:g} day(s), db {args.db}")
    print("\nquota (weekly %, from muse2api's own reading of the Muse settings panel)")
    if not out["quota"]:
        print("  no samples yet")
    for q in out["quota"]:
        print(f"  {q['label'] or q['account']:<18} {q['plan'] or '?':<11} now {q['weekly_used_pct']}%  "
              f"used in window {q['used_pct_in_window']}%  resets {q['weekly_reset']}  "
              f"checked {time.strftime('%m-%d %H:%M', time.localtime(q['last_checked']))} ({q['source']}, {q['samples']} samples)")
    print("\nrequests reaching muse2api (2xx / 4xx / 5xx)")
    if not out["requests"]:
        print("  none recorded")
    for route, r in sorted(out["requests"].items(), key=lambda kv: -sum(kv[1].values())):
        print(f"  {route:<13} {r['2xx']:>6} / {r['4xx']:>4} / {r['5xx']:>4}")
    print("\ntasks (image / video)")
    if not out["tasks"]:
        print("  none recorded")
    for t in out["tasks"]:
        print(f"  {t['kind']:<6} {t['status']:<10} {t['n']:>5}   p50 {t['p50_s']}s  p90 {t['p90_s']}s")
    e = out["estimate"]
    print("\nestimate: " + (f"{e['work_requests']} successful chat/image/video requests between two quota readings "
                             f"used {e['weekly_pct_used']}% of the weekly quota -> about {e['requests_per_pct']} per 1%"
                             if e else "needs two quota readings with movement and successful requests between them"))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect", help="take one sample")
    c.add_argument("--db", default="/var/lib/muse2api-meter/meter.sqlite")
    c.add_argument("--base", default="http://127.0.0.1:18610")
    c.add_argument("--key-file", default="/etc/muse2api/muse2api.env")
    c.add_argument("--unit", default="muse2api.service", help="systemd unit whose access log to count ('' to skip)")
    c.add_argument("--journal-file", help="read `journalctl -o json` output from this file instead of journald")
    c.add_argument("--quota-every", type=int, default=0,
                   help="live quota read at most every N seconds (0 = never, minimum 3600; default 0)")
    c.add_argument("--force", action="store_true", help="live quota read now, even if a task is running")
    c.add_argument("--task-limit", type=int, default=500)
    r = sub.add_parser("report", help="print what was spent")
    r.add_argument("--db", default="/var/lib/muse2api-meter/meter.sqlite")
    r.add_argument("--days", type=float, default=7)
    r.add_argument("--json", action="store_true")
    args = ap.parse_args()
    return collect(args) if args.cmd == "collect" else report(args)


if __name__ == "__main__":
    sys.exit(main())
