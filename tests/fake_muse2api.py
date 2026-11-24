"""Fake muse2api for offline tests of smoke.py and meter/muse_meter.py.

Mirrors the upstream behaviour the kit depends on (checked against v1.6.12):
POST /v1/images/tasks -> 202 + poll, POST /v1/videos -> 200 + poll, a loopback reference
URL -> 400 (upstream's SSRF guard), usage always 0, quota cached on GET /admin/accounts.
Usage: python3 fake_muse2api.py PORT   (key: m2a_test)
"""
import json, sys, time, threading, base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
KEY = "m2a_test"
TASKS = {}
QUOTA = {"pct": 3}
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
LOCK = threading.Lock()
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, obj, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def authed(self):
        if self.headers.get("Authorization") != "Bearer " + KEY:
            self.send(401, {"error": {"message": "API key 无效"}}); return False
        return True
    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")
    def do_GET(self):
        p = self.path
        if p == "/healthz": return self.send(200, {"status": "ok"})
        if p.startswith("/v1/media/"): return self.send(200, PNG, "image/png")
        if not self.authed(): return
        if p == "/v1/models": return self.send(200, {"data": [{"id": "muse-spark"}, {"id": "muse-image"}, {"id": "muse-video"}]})
        if p.startswith("/v1/images/tasks/") or p.startswith("/v1/videos/"):
            tid = p.rsplit("/", 1)[1]; t = TASKS.get(tid)
            if not t: return self.send(404, {"error": {"message": "not found"}})
            t["polls"] += 1
            if t["polls"] < 2: return self.send(200, {"id": tid, "status": "processing"})
            t["status"] = "completed"
            if t["kind"] == "image":
                return self.send(200, {"id": tid, "status": "completed", "url": "/v1/media/img.png",
                                       "data": [{"url": "/v1/media/img.png", "kind": "png", "bytes": len(PNG)}]})
            return self.send(200, {"id": tid, "status": "completed", "url": "/v1/media/v.mp4", "result": {"url": "/v1/media/v.mp4", "bytes": 1234}})
        if p == "/admin/accounts":
            return self.send(200, {"accounts": [{"id": "acc1", "label": "spare", "enabled": True, "cookie_count": 5,
                    "quota": {"found": True, "plan": "Free plan", "weekly_used_pct": QUOTA["pct"], "weekly_reset": "Dec 1", "checked_at": 1795500000}}]})
        if p.startswith("/admin/tasks"):
            return self.send(200, {"tasks": [{"id": k, "kind": v["kind"], "status": v["status"], "created_at": int(time.time()),
                     "updated_at": int(time.time()), "elapsed": 12.5, "prompt": "secret prompt text",
                     "result": {"bytes": 99, "kind": "mp4"} if v["status"] == "completed" else None} for k, v in TASKS.items()]})
        self.send(404, {"detail": "nope"})
    def do_POST(self):
        p = self.path
        if not self.authed(): return
        b = self.body()
        if p == "/v1/chat/completions":
            return self.send(200, {"choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})
        if p == "/v1/images/tasks":
            tid = "task_img%d" % len(TASKS); TASKS[tid] = {"kind": "image", "status": "queued", "polls": 0}
            return self.send(202, {"id": tid, "status": "queued"})
        if p == "/v1/videos":
            ref = b.get("image_url") or ""
            if ref.startswith("http://127.") or ref.startswith("http://localhost"):
                return self.send(400, {"error": {"message": "images[0]: 参考图地址被拒绝：仅允许公网地址"}})
            if ref and not ref.startswith("data:image/png;base64,"):
                return self.send(400, {"error": {"message": "bad ref " + ref[:30]}})
            tid = "task_vid%d" % len(TASKS); TASKS[tid] = {"kind": "video", "status": "queued", "polls": 0, "ref": bool(ref)}
            return self.send(200, {"id": tid, "status": "queued"})
        if p.startswith("/admin/accounts/") and p.endswith("/quota"):
            QUOTA["pct"] += 1
            return self.send(200, {"found": True, "plan": "Free plan", "weekly_used_pct": QUOTA["pct"], "weekly_reset": "Dec 1", "checked_at": int(time.time())})
        self.send(404, {"detail": "nope"})
if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
