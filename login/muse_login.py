#!/usr/bin/env python3
"""Log a Muse account in from this server and import the session into muse2api.

Upstream muse2api only *imports* cookies (browser extension / userscript). That means the
login happens in your desktop browser, from your home IP, and the session is then used from
the VPS's IP. This script does the login on the VPS itself, through headless Chrome, so the
session is created from the same exit IP that will use it:

  muse.ai -> email -> Continue -> (one-time code | password) -> /api/session == 200
  -> read cookies (hatch_sess, ...) -> POST /admin/accounts (or update the account with the
  same label) -> POST /admin/accounts/{id}/test

Where the one-time code comes from (--otp):
  prompt  (default) you read the code in your mailbox and type it here
  imap    poll an IMAP mailbox:   MUSE_IMAP_HOST, MUSE_IMAP_USER, MUSE_IMAP_PASSWORD,
          optional MUSE_IMAP_PORT (993), MUSE_IMAP_FOLDER (INBOX), MUSE_IMAP_FROM (filter)
  http    poll a JSON endpoint:   MUSE_OTP_URL, optional MUSE_OTP_HEADER ("Name: value")
          accepted shapes: {"code": "123456"} | {"otp": ...} | a list of messages, or
          {"emails"|"messages"|"data"|"items": [...]} with to / created_at|date / otp|code /
          subject / body|text|html fields. Messages for other recipients are ignored.

Settings can come from flags, the environment, or --account-env FILE (KEY=VALUE lines):
  MUSE_EMAIL, MUSE_PASSWORD (only if Muse asks for one), MUSE_LABEL, MUSE_OTP_REGEX.

Safety: it stops (exit 3, screenshot in --shots) on a captcha, checkpoint / identity check,
terms / consent, sign-up, birthday, waitlist or region page. It never clicks "agree" and
never creates an account. It prints no cookie value, code or key.

Needs: pip install playwright (uses the system Chrome; no `playwright install` needed).
Exit codes: 0 ok, 2 usage/config, 3 stopped for a human, 4 import or account test failed.
"""
from __future__ import annotations

import argparse
import asyncio
import email
import email.utils
import getpass
import imaplib
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

T0 = time.time()
BLOCKERS = re.compile(
    r"captcha|i'?m not a robot|verify you are human|security check|suspicious|"
    r"confirm (it'?s|that it'?s) you|confirm your identity|checkpoint|upload a (photo|selfie)|"
    r"birthday|date of birth|create (an|your|new) account|sign up for|"
    r"i agree|agree to (the )?terms|accept (the )?terms|review (the )?terms|"
    r"waitlist|join the wait|not (yet )?available in your|isn'?t available", re.I)


def log(msg: str) -> None:
    print(f"[{time.time() - T0:6.1f}s] {msg}", flush=True)


def load_env(path: str | None) -> dict:
    out: dict = {}
    if not path:
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                out[k.strip()] = v.strip().strip("'\"")
    return out


def mask(addr: str) -> str:
    user, _, dom = addr.partition("@")
    return (user[:2] + "***@" + dom) if dom else "***"


# ----------------------------------------------------------------- one-time code sources
class OtpSource:
    def __init__(self, cfg: dict, email_addr: str, regex: str):
        self.cfg, self.email, self.rx = cfg, email_addr.lower(), re.compile(regex)

    def code_in(self, *texts: str) -> str | None:
        m = self.rx.search("\n".join(t or "" for t in texts))
        return m.group(1) if m and m.groups() else (m.group(0) if m else None)

    async def wait(self, since: float, timeout: float) -> str | None:
        raise NotImplementedError


class PromptOtp(OtpSource):
    async def wait(self, since: float, timeout: float) -> str | None:
        if not sys.stdin.isatty():
            log("STOP: --otp prompt needs a terminal (use ssh -t, or --otp imap / --otp http)")
            return None
        print(f"\nMuse sent a code to {mask(self.email)}. Type it here (Enter to give up): ", end="", flush=True)
        line = await asyncio.get_running_loop().run_in_executor(None, sys.stdin.readline)
        return self.code_in(line) or (line.strip() or None)


class ImapOtp(OtpSource):
    def _fetch(self, since: float) -> str | None:
        host = self.cfg.get("MUSE_IMAP_HOST")
        user = self.cfg.get("MUSE_IMAP_USER") or self.email
        pw = self.cfg.get("MUSE_IMAP_PASSWORD")
        if not host or not pw:
            raise SystemExit("--otp imap needs MUSE_IMAP_HOST and MUSE_IMAP_PASSWORD")
        sender = (self.cfg.get("MUSE_IMAP_FROM") or "").lower()
        box = imaplib.IMAP4_SSL(host, int(self.cfg.get("MUSE_IMAP_PORT") or 993))
        try:
            box.login(user, pw)
            box.select(self.cfg.get("MUSE_IMAP_FOLDER") or "INBOX", readonly=True)
            day = time.strftime("%d-%b-%Y", time.gmtime(since - 86400))
            _, ids = box.search(None, "SINCE", day)
            best: tuple[float, str] | None = None
            for num in reversed(ids[0].split()[-30:]):
                _, parts = box.fetch(num, "(INTERNALDATE RFC822)")
                meta, raw = parts[0][0], parts[0][1]
                when = imaplib.Internaldate2tuple(meta)
                ts = time.mktime(when) if when else 0
                if ts < since - 30:
                    continue
                msg = email.message_from_bytes(raw)
                if sender and sender not in (msg.get("From") or "").lower():
                    continue
                to = ",".join(a for _, a in email.utils.getaddresses(msg.get_all("To", []))).lower()
                if to and self.email not in to:
                    continue
                body = []
                for part in msg.walk():
                    if part.get_content_type() in ("text/plain", "text/html"):
                        body.append((part.get_payload(decode=True) or b"").decode(errors="replace"))
                code = self.code_in(msg.get("Subject") or "", *body)
                if code and (best is None or ts > best[0]):
                    best = (ts, code)
            return best[1] if best else None
        finally:
            try:
                box.logout()
            except Exception:  # noqa: BLE001
                pass

    async def wait(self, since: float, timeout: float) -> str | None:
        end = time.time() + timeout
        while time.time() < end:
            code = await asyncio.get_running_loop().run_in_executor(None, self._fetch, since)
            if code:
                return code
            await asyncio.sleep(5)
        return None


class HttpOtp(OtpSource):
    def _fetch(self, since: float) -> str | None:
        url = self.cfg.get("MUSE_OTP_URL")
        if not url:
            raise SystemExit("--otp http needs MUSE_OTP_URL")
        headers = {"Accept": "application/json"}
        if self.cfg.get("MUSE_OTP_HEADER"):
            k, _, v = self.cfg["MUSE_OTP_HEADER"].partition(":")
            headers[k.strip()] = v.strip()
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as r:
            data = json.loads(r.read().decode() or "null")
        if isinstance(data, dict):
            direct = data.get("code") or data.get("otp")
            if isinstance(direct, (str, int)) and str(direct).strip():
                return str(direct).strip()
            for key in ("emails", "messages", "data", "items"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
        if not isinstance(data, list):
            return None
        best: tuple[float, str] | None = None
        for m in data:
            if not isinstance(m, dict):
                continue
            to = str(m.get("to") or m.get("recipient") or "").lower()
            if to and self.email not in to:
                continue
            stamp = str(m.get("created_at") or m.get("date") or m.get("received_at") or "")
            try:
                ts = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
            except ValueError:
                try:
                    ts = email.utils.parsedate_to_datetime(stamp).timestamp()
                except (TypeError, ValueError):
                    continue
            if ts < since - 30:
                continue
            code = str(m.get("otp") or m.get("code") or "") or self.code_in(
                str(m.get("subject") or ""), str(m.get("body") or m.get("text") or m.get("html") or ""))
            if code and (best is None or ts > best[0]):
                best = (ts, code)
        return best[1] if best else None

    async def wait(self, since: float, timeout: float) -> str | None:
        end = time.time() + timeout
        while time.time() < end:
            code = await asyncio.get_running_loop().run_in_executor(None, self._fetch, since)
            if code:
                return code
            await asyncio.sleep(5)
        return None


SOURCES = {"prompt": PromptOtp, "imap": ImapOtp, "http": HttpOtp}


# ----------------------------------------------------------------- muse2api admin calls
def api(base: str, key: str, method: str, path: str, body: dict | None = None, timeout: int = 60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method, headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {"body": e.read().decode(errors="replace")[:300]}


# ----------------------------------------------------------------- browser helpers
async def shot(pg, shots: str, name: str) -> None:
    os.makedirs(shots, mode=0o700, exist_ok=True)
    path = os.path.join(shots, f"{int(T0)}-{name}.png")
    try:
        await pg.screenshot(path=path, full_page=False)
        log(f"screenshot: {path}")
    except Exception:  # noqa: BLE001
        pass


async def visible_inputs(pg) -> list[dict]:
    return await pg.evaluate("""Array.from(document.querySelectorAll('input')).filter(e=>e.offsetParent!==null)
      .map(e=>({type:e.type, ph:e.placeholder||'', ac:e.autocomplete||'', im:e.inputMode||'',
                ml:e.maxLength, name:e.name||'', aria:e.getAttribute('aria-label')||''}))""")


async def page_text(pg) -> str:
    try:
        return (await pg.evaluate("document.body.innerText"))[:4000]
    except Exception:  # noqa: BLE001
        return ""


async def session_ok(pg) -> bool:
    try:
        st = await pg.evaluate("fetch('/api/session',{credentials:'include'}).then(r=>r.status).catch(()=>0)")
        return st == 200
    except Exception:  # noqa: BLE001
        return False


async def click_submit(pg) -> None:
    for label in ("Continue", "Verify", "Next", "Log in", "Submit", "Confirm"):
        btn = pg.get_by_role("button", name=re.compile(rf"^\s*{label}\s*$", re.I))
        if await btn.count() and await btn.first.is_enabled():
            await btn.first.click()
            return
    await pg.keyboard.press("Enter")


def find_chrome(explicit: str | None) -> str:
    if explicit:
        return explicit
    for c in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser"):
        p = shutil.which(c)
        if p:
            return p
    raise SystemExit("no Chrome/Chromium found; pass --chrome PATH")


async def login(args, cfg: dict, otp: OtpSource) -> tuple[int, dict, dict]:
    from playwright.async_api import async_playwright  # imported late so --help works without it

    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=find_chrome(args.chrome), headless=not args.headful,
                                          args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        ctx_opts = {"viewport": {"width": 1280, "height": 900}, "locale": args.locale}
        if args.timezone:
            ctx_opts["timezone_id"] = args.timezone
        ctx = await browser.new_context(**ctx_opts)
        pg = await ctx.new_page()
        await pg.goto(args.site, wait_until="domcontentloaded", timeout=60000)
        await pg.wait_for_timeout(4000)
        if not await session_ok(pg):
            box = pg.get_by_placeholder(re.compile("mobile number or email|email", re.I)).first
            try:
                await box.wait_for(timeout=30000)
            except Exception:  # noqa: BLE001
                await shot(pg, args.shots, "no-email-box")
                txt = re.sub(r"\s+", " ", await page_text(pg))[:400]
                log(f"STOP: no email field on {pg.url}; page says: {txt}")
                return 3, {}, {}
            await box.fill(cfg["MUSE_EMAIL"])
            sent_at = time.time()
            await click_submit(pg)
            log(f"email submitted for {mask(cfg['MUSE_EMAIL'])}")

            code_done = pw_done = False
            for _ in range(int(args.timeout / 5)):
                await pg.wait_for_timeout(5000)
                if await session_ok(pg):
                    break
                txt = await page_text(pg)
                ins = await visible_inputs(pg)
                if any(i["type"] == "password" for i in ins) and not pw_done:
                    pw = cfg.get("MUSE_PASSWORD")
                    if not pw and sys.stdin.isatty():
                        pw = getpass.getpass("Muse asks for the account password: ")
                    if not pw:
                        await shot(pg, args.shots, "password")
                        log("STOP: a password is required; set MUSE_PASSWORD or run in a terminal")
                        return 3, {}, {}
                    await pg.locator("input[type=password]").first.fill(pw)
                    await click_submit(pg)
                    pw_done = True
                    log("password step submitted")
                    continue
                codey = [i for i in ins if i["ac"] == "one-time-code" or i["im"] == "numeric"
                         or re.search("code", i["ph"] + i["aria"] + i["name"], re.I)
                         or (i["type"] in ("text", "tel", "number") and 0 < (i["ml"] or 0) <= 8)]
                if codey and not code_done:
                    log(f"code step: waiting for the code ({args.otp})")
                    code = await otp.wait(sent_at, args.otp_timeout)
                    if not code:
                        await shot(pg, args.shots, "no-code")
                        log("STOP: no one-time code")
                        return 3, {}, {}
                    fields = pg.locator("input[autocomplete='one-time-code'], input[inputmode='numeric'], "
                                        "input[type='tel'], input[type='text'], input[type='number']")
                    if await fields.count() >= 4:      # one box per digit
                        await fields.first.click()
                        await pg.keyboard.type(code, delay=120)
                    else:
                        await fields.first.fill(code)
                    await pg.wait_for_timeout(800)
                    await click_submit(pg)
                    code_done = True
                    log("code step submitted")
                    continue
                if BLOCKERS.search(txt):
                    await shot(pg, args.shots, "needs-human")
                    log("STOP: this page needs a human (captcha, identity check, terms, sign-up or region)."
                        " Nothing was clicked. Finish it in a normal browser, then re-run.")
                    log("page says: " + re.sub(r"\s+", " ", txt)[:400])
                    return 3, {}, {}
            else:
                await shot(pg, args.shots, "timeout")
                log(f"STOP: no session after {args.timeout}s; url={pg.url}")
                log("page says: " + re.sub(r"\s+", " ", await page_text(pg))[:400])
                return 3, {}, {}
        log("session is 200")
        await pg.wait_for_timeout(3000)
        jar = await ctx.cookies([args.site, args.site.replace("://", "://www.", 1)])
        await browser.close()
    cookies = {c["name"]: c["value"] for c in jar}
    expires = {c["name"]: int(c["expires"]) for c in jar if (c.get("expires") or -1) > 0}
    log(f"cookies read: {sorted(cookies)}")
    if "hatch_sess" not in cookies:
        log("STOP: logged in but no hatch_sess cookie")
        return 3, {}, {}
    return 0, cookies, expires


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--email", help="account email (or MUSE_EMAIL)")
    ap.add_argument("--label", help="account label in muse2api; an existing account with this label is updated "
                                    "(or MUSE_LABEL; default: muse-<email user>)")
    ap.add_argument("--otp", choices=sorted(SOURCES), default="prompt", help="where the one-time code comes from")
    ap.add_argument("--otp-timeout", type=int, default=300, help="seconds to wait for the code (default: %(default)s)")
    ap.add_argument("--account-env", help="optional KEY=VALUE file with MUSE_EMAIL / MUSE_PASSWORD / IMAP or HTTP settings")
    ap.add_argument("--api", default="http://127.0.0.1:18610", help="muse2api base (default: %(default)s)")
    ap.add_argument("--key-file", default="/etc/muse2api/muse2api.env", help="file with MUSE2API_KEY (default: %(default)s)")
    ap.add_argument("--no-import", action="store_true", help="log in and save cookies to --cookies-out only")
    ap.add_argument("--cookies-out", help="also write cookies + expiries as JSON (mode 0600) to this path")
    ap.add_argument("--shots", default="./muse-login-shots", help="screenshot dir for stops (default: %(default)s)")
    ap.add_argument("--chrome", help="Chrome/Chromium binary (default: auto-detect)")
    ap.add_argument("--site", default="https://muse.ai/", help=argparse.SUPPRESS)
    ap.add_argument("--locale", default="en-US", help="browser locale; the stop-page detection reads English")
    ap.add_argument("--timezone", help="IANA zone matching the server's exit IP, e.g. America/Los_Angeles")
    ap.add_argument("--timeout", type=int, default=300, help="seconds for the whole verification flow")
    ap.add_argument("--headful", action="store_true", help="show the browser (needs a display)")
    args = ap.parse_args()

    cfg = {k: v for k, v in os.environ.items() if k.startswith("MUSE")}
    cfg.update(load_env(args.account_env))
    if args.email:
        cfg["MUSE_EMAIL"] = args.email
    if not cfg.get("MUSE_EMAIL"):
        ap.error("--email (or MUSE_EMAIL) is required")
    label = args.label or cfg.get("MUSE_LABEL") or "muse-" + cfg["MUSE_EMAIL"].split("@")[0][:20]
    key = ""
    if not args.no_import:
        key = os.environ.get("MUSE2API_KEY") or load_env(args.key_file).get("MUSE2API_KEY", "")
        if not key:
            ap.error(f"no MUSE2API_KEY in {args.key_file} or the environment")
        st, _ = api(args.api, key, "GET", "/v1/models", timeout=10)
        if st != 200:
            print(f"muse2api at {args.api} answered {st} to /v1/models; fix that first", file=sys.stderr)
            return 2
    otp = SOURCES[args.otp](cfg, cfg["MUSE_EMAIL"], cfg.get("MUSE_OTP_REGEX") or r"\b(\d{6})\b")

    rc, cookies, expires = asyncio.run(login(args, cfg, otp))
    if rc:
        return rc
    if args.cookies_out:
        fd = os.open(args.cookies_out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"label": label, "cookies": cookies, "expires": expires}, f)
        log(f"cookies written to {args.cookies_out} (0600)")
    if args.no_import:
        return 0

    st, listing = api(args.api, key, "GET", "/admin/accounts")
    existing = next((a for a in (listing.get("accounts") or []) if a.get("label") == label), None)
    if existing:
        st, res = api(args.api, key, "POST", f"/admin/accounts/{existing['id']}/cookies",
                      {"cookies": cookies, "expires": expires})
        aid = existing["id"] if st == 200 else None
        log(f"updated account {label} ({existing['id']}): http {st}")
    else:
        st, res = api(args.api, key, "POST", "/admin/accounts",
                      {"label": label, "cookies": cookies, "expires": expires})
        added = res.get("added") or []
        aid = added[0]["id"] if st == 200 and added else None
        log(f"imported account {label}: http {st} missing={res.get('essential_missing')}")
    if not aid:
        log(f"import failed: {str(res)[:300]}")
        return 4
    st, test = api(args.api, key, "POST", f"/admin/accounts/{aid}/test", {}, timeout=300)
    safe = {k: v for k, v in test.items() if k != "cookies"} if isinstance(test, dict) else test
    log("account test: " + json.dumps(safe, ensure_ascii=False)[:600])
    return 0 if st == 200 and isinstance(test, dict) and test.get("ok") else 4


if __name__ == "__main__":
    sys.exit(main())
