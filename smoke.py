#!/usr/bin/env python3
"""Smoke test for a muse2api instance: one call per capability, compact table, JSON report.

Stdlib only. Run on the VPS (default) or anywhere that can reach the API:

    sudo python3 smoke.py                                   # key from /etc/muse2api/muse2api.env
    python3 smoke.py --base https://api.example.com --key-env MUSE_FRONT_KEY --only chat

Steps: models -> chat -> image (async task) -> text-to-video -> image-to-video.
Every generation step spends a little of the account's free weekly quota (the full
set moved one Free account from 0% to 2% in our test), so use --only to run less.

Behaviour that tripped up our first script, now handled:
  * POST /v1/images/tasks answers 202 Accepted with a task id. That is success; poll it.
  * Image-to-video sends the generated image as a data: URL. A loopback or private URL
    (e.g. http://127.0.0.1:18610/v1/media/...) is refused on purpose by muse2api's
    reference-image SSRF guard unless MUSE2API_REF_IMAGE_PRIVATE_FETCH=1.
  * usage is always 0 in muse2api responses; the report records it so you can see that.

No key, cookie or account detail is printed or written to the report.
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

STEPS = ("models", "chat", "image", "t2v", "i2v")


def read_key(path: str, name: str) -> str:
    if os.environ.get(name):
        return os.environ[name].strip()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    except OSError as exc:
        sys.exit(f"cannot read {path}: {exc.strerror} (run with sudo, or set ${name})")
    sys.exit(f"{name} not found in {path} or the environment")


class Api:
    def __init__(self, base: str, key: str):
        self.base = base.rstrip("/")
        self.key = key

    def call(self, method: str, path: str, body: dict | None = None, timeout: int = 360):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": "Bearer " + self.key, "Content-Type": "application/json"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw, status = r.read(), r.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(), e.code
        except (urllib.error.URLError, TimeoutError) as e:
            return 0, {"error": f"{type(e).__name__}: {getattr(e, 'reason', e)}"}, round(time.time() - t0, 1)
        try:
            js = json.loads(raw.decode() or "{}")
        except ValueError:
            js = {"_raw": raw.decode(errors="replace")[:500]}
        return status, js, round(time.time() - t0, 1)

    def poll(self, path: str, limit: int, every: float = 5.0) -> tuple[dict, float]:
        t0 = time.time()
        last: dict = {}
        while time.time() - t0 < limit:
            st, js, _ = self.call("GET", path, timeout=30)
            if st == 200:
                last = js
                if js.get("status") in ("completed", "succeeded", "failed"):
                    break
            time.sleep(every)
        return last, round(time.time() - t0, 1)

    def fetch_media(self, url: str) -> tuple[bytes, str]:
        """Download a generated file. Absolute public URLs are mapped back onto --base."""
        path = urllib.parse.urlsplit(url).path
        with urllib.request.urlopen(self.base + path, timeout=60) as r:
            mime = r.headers.get_content_type()
            return r.read(), mime if mime != "application/octet-stream" else (
                mimetypes.guess_type(path)[0] or "image/png")


def err_text(js: dict) -> str:
    e = js.get("error")
    if isinstance(e, dict):
        e = e.get("message")
    return str(e or js.get("detail") or js.get("_raw") or "")[:160]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://127.0.0.1:18610", help="API base without /v1 (default: %(default)s)")
    ap.add_argument("--key-file", default="/etc/muse2api/muse2api.env", help="env file holding the key (default: %(default)s)")
    ap.add_argument("--key-env", default="MUSE2API_KEY", help="variable name of the key, in the env or the file (default: %(default)s)")
    ap.add_argument("--only", default=",".join(STEPS), help=f"comma list from {','.join(STEPS)} (default: all)")
    ap.add_argument("--out", default=".", help="directory for smoke-<time>.json (default: current dir)")
    ap.add_argument("--image-timeout", type=int, default=420)
    ap.add_argument("--video-timeout", type=int, default=900)
    args = ap.parse_args()

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    bad = only - set(STEPS)
    if bad:
        ap.error(f"unknown step(s): {', '.join(sorted(bad))}")
    api = Api(args.base, read_key(args.key_file, args.key_env))
    report: dict = {"started": int(time.time()), "base": args.base, "steps": {}}
    rows: list[tuple] = []

    def record(name: str, ok: bool, http: int, seconds: float, detail: str, **extra) -> None:
        report["steps"][name] = {"ok": ok, "http": http, "seconds": seconds, "detail": detail, **extra}
        rows.append((name, "ok" if ok else "FAIL", http, seconds, detail))
        print(f"  {name:<7} {'ok' if ok else 'FAIL':<4} http={http:<3} {seconds:>6}s  {detail}", flush=True)

    print(f"smoke test against {args.base}")

    if "models" in only:
        st, js, dt = api.call("GET", "/v1/models", timeout=30)
        ids = [m.get("id") for m in js.get("data", [])] if st == 200 else []
        record("models", st == 200 and bool(ids), st, dt, ",".join(ids) or err_text(js))

    if "chat" in only:
        st, js, dt = api.call("POST", "/v1/chat/completions", {
            "model": "muse-spark", "messages": [{"role": "user", "content": "Reply with the single word: ok"}]})
        text = ((js.get("choices") or [{}])[0].get("message") or {}).get("content") if st == 200 else None
        record("chat", st == 200 and bool(text), st, dt,
               repr((text or "")[:40]) + f" usage={js.get('usage')}" if st == 200 else err_text(js),
               usage=js.get("usage"))

    image_url = None
    if "image" in only or "i2v" in only:
        st, js, dt = api.call("POST", "/v1/images/tasks", {
            "model": "muse-image", "prompt": "a red apple on a white table, studio product photo",
            "aspect_ratio": "1:1"}, timeout=60)
        if st in (200, 202) and js.get("id"):          # 202 Accepted is the normal answer
            res, wait = api.poll(f"/v1/images/tasks/{js['id']}", args.image_timeout)
            item = (res.get("data") or [{}])[0]
            image_url = item.get("url") or res.get("url")
            ok = res.get("status") == "completed" and bool(image_url)
            record("image", ok, st, wait,
                   f"{item.get('kind')} {item.get('bytes')} bytes" if ok else f"status={res.get('status')} {err_text(res)}",
                   submit_http=st, task_status=res.get("status"), url=image_url)
        else:
            record("image", False, st, dt, err_text(js))

    def video(name: str, body: dict) -> None:
        st, js, dt = api.call("POST", "/v1/videos", body, timeout=120)
        if st in (200, 202) and js.get("id"):
            res, wait = api.poll(f"/v1/videos/{js['id']}", args.video_timeout, every=8)
            url = res.get("url") or (res.get("result") or {}).get("url")
            ok = res.get("status") in ("completed", "succeeded") and bool(url)
            size = (res.get("result") or {}).get("bytes")
            record(name, ok, st, wait, f"{size} bytes" if ok else f"status={res.get('status')} {err_text(res)}",
                   submit_http=st, task_status=res.get("status"), url=url)
        else:
            record(name, False, st, dt, err_text(js))

    if "t2v" in only:
        video("t2v", {"model": "muse-video", "prompt": "a paper boat drifting on a calm lake at sunset",
                      "aspect_ratio": "16:9"})

    if "i2v" in only:
        if image_url:
            data, mime = api.fetch_media(image_url)
            data_url = f"data:{mime};base64,{base64.b64encode(data).decode()}"
            video("i2v", {"model": "muse-video", "prompt": "the apple slowly turns around, soft light",
                          "image_url": data_url, "aspect_ratio": "16:9"})
        else:
            record("i2v", False, 0, 0, "skipped: no image from the image step")

    report["finished"] = int(time.time())
    report["ok"] = all(s["ok"] for s in report["steps"].values())
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, time.strftime("smoke-%Y%m%dT%H%M%SZ.json", time.gmtime(report["started"])))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"{'PASS' if report['ok'] else 'FAIL'}: {sum(r[1] == 'ok' for r in rows)}/{len(rows)} steps ok; report {path}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
