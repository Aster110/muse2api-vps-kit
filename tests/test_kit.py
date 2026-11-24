"""Offline tests for smoke.py, meter/muse_meter.py and the login OTP parsers.

Run: python3 tests/test_kit.py     (stdlib only; no Muse account, no network)
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "meter"))
sys.path.insert(0, str(ROOT / "login"))
import fake_muse2api  # noqa: E402
import muse_meter  # noqa: E402
import muse_login  # noqa: E402


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class KitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.base = serve(fake_muse2api.H)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.keyfile = os.path.join(cls.tmp.name, "muse2api.env")
        Path(cls.keyfile).write_text("# test\nMUSE2API_KEY=m2a_test\n")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def run_py(self, *args):
        env = {k: v for k, v in os.environ.items() if k != "MUSE2API_KEY"}
        return subprocess.run([sys.executable, *args], capture_output=True, text=True, env=env, timeout=120)

    def test_smoke_accepts_202_and_sends_data_url(self):
        out = os.path.join(self.tmp.name, "smoke")
        r = self.run_py(str(ROOT / "smoke.py"), "--base", self.base, "--key-file", self.keyfile, "--out", out)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        report = json.loads(Path(out, os.listdir(out)[0]).read_text())
        self.assertEqual(report["steps"]["image"]["submit_http"], 202)
        self.assertTrue(report["steps"]["i2v"]["ok"])      # fake rejects loopback URLs, accepts data URLs
        self.assertEqual(report["steps"]["chat"]["usage"]["total_tokens"], 0)
        self.assertNotIn("m2a_test", r.stdout + json.dumps(report))

    def test_smoke_wrong_key_fails(self):
        bad = os.path.join(self.tmp.name, "bad.env")
        Path(bad).write_text("MUSE2API_KEY=m2a_wrong\n")
        r = self.run_py(str(ROOT / "smoke.py"), "--base", self.base, "--key-file", bad, "--only", "models",
                        "--out", self.tmp.name)
        self.assertEqual(r.returncode, 1)
        self.assertIn("http=401", r.stdout)

    def test_meter_collect_report(self):
        db = os.path.join(self.tmp.name, "meter.sqlite")
        now_us = int(time.time()) * 1_000_000
        msgs = ['"POST /v1/chat/completions HTTP/1.1" 200 OK', '"POST /v1/chat/completions HTTP/1.1" 502 Bad',
                '"POST /v1/images/tasks HTTP/1.1" 202 Accepted', '"GET /v1/images/tasks/t1 HTTP/1.1" 200 OK',
                '"GET /admin?key=m2a_secret HTTP/1.1" 200 OK', 'not an access line']
        journal = os.path.join(self.tmp.name, "journal.json")
        with open(journal, "w") as f:
            for i, m in enumerate(msgs):
                f.write(json.dumps({"__CURSOR": f"c{i}", "__REALTIME_TIMESTAMP": str(now_us - (60 - i) * 1_000_000),
                                    "MESSAGE": "INFO:     127.0.0.1:1 - " + m}) + "\n")
        common = ["collect", "--db", db, "--base", self.base, "--key-file", self.keyfile, "--unit", "",
                  "--journal-file", journal]
        for extra in ([], ["--quota-every", "3600"]):   # second run: no double counting, one live read
            r = self.run_py(str(ROOT / "meter" / "muse_meter.py"), *common, *extra)
            self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_py(str(ROOT / "meter" / "muse_meter.py"), "report", "--db", db, "--days", "1", "--json")
        rep = json.loads(r.stdout)
        self.assertEqual(rep["requests"]["chat"], {"2xx": 1, "4xx": 0, "5xx": 1, "other": 0})
        self.assertEqual(rep["requests"]["image_submit"]["2xx"], 1)
        self.assertEqual(rep["requests"]["admin"]["2xx"], 1)
        self.assertEqual(len(rep["quota"]), 1)
        self.assertGreater(rep["quota"][0]["used_pct_in_window"], 0)
        self.assertIsNotNone(rep["estimate"])
        self.assertNotIn(b"m2a_secret", Path(db).read_bytes())   # query strings are never stored
        self.assertNotIn(b"secret prompt", Path(db).read_bytes())  # task prompts are never stored

    def test_meter_quota_floor(self):
        r = self.run_py(str(ROOT / "meter" / "muse_meter.py"), "collect", "--db",
                        os.path.join(self.tmp.name, "floor.sqlite"), "--base", self.base,
                        "--key-file", self.keyfile, "--unit", "", "--quota-every", "60")
        self.assertIn("raised to 3600", r.stderr)

    def test_route_of(self):
        self.assertEqual(muse_meter.route_of("GET", "/v1/videos/task_1"), "video_poll")
        self.assertEqual(muse_meter.route_of("POST", "/v1/videos"), "video_submit")
        self.assertEqual(muse_meter.route_of("GET", "/v1/media/a.mp4"), "media")
        self.assertEqual(muse_meter.route_of("GET", "/admin?key=x"), "admin")

    def test_http_otp_shapes(self):
        since = time.time()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since + 5))
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since - 600))
        payloads = {
            "/direct": {"code": "123456"},
            "/list": {"emails": [
                {"to": "other@example.com", "created_at": stamp, "subject": "Your code 111111"},
                {"to": "you@example.com", "created_at": old, "subject": "Your code 222222"},
                {"to": "you@example.com", "created_at": stamp, "subject": "Muse", "body": "Your code is 654321."}]},
            "/none": {"emails": []},
        }

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = json.dumps(payloads[self.path]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

        srv, base = serve(H)
        try:
            for path, want in (("/direct", "123456"), ("/list", "654321"), ("/none", None)):
                otp = muse_login.HttpOtp({"MUSE_OTP_URL": base + path}, "you@example.com", r"\b(\d{6})\b")
                self.assertEqual(otp._fetch(since), want, path)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_prompt_otp_needs_tty(self):
        otp = muse_login.PromptOtp({}, "you@example.com", r"\b(\d{6})\b")
        if not sys.stdin.isatty():
            self.assertIsNone(asyncio.run(otp.wait(time.time(), 1)))

    def test_blocker_regex(self):
        for text in ("Please verify you are human", "Join the waitlist", "I agree to the Terms",
                     "Confirm it's you", "Enter your date of birth"):
            self.assertRegex(text, muse_login.BLOCKERS)
        self.assertNotRegex("Enter the code we sent to your email", muse_login.BLOCKERS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
