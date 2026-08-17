#!/usr/bin/env python3
"""
Snapchat-style app server (Python stdlib only).

Run:
    python3 server.py            # port 8001
    python3 server.py 9000       # custom port

Open http://localhost:<port> from any browser on the same network.
Two people on the same network can use it together.
"""
import base64
import json
import os
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HOST = "0.0.0.0"
PORT = 8001
HERE = os.path.dirname(os.path.abspath(__file__))
MEDIA = os.path.join(HERE, "media")
os.makedirs(MEDIA, exist_ok=True)

lock = threading.RLock()
users = {}          # name -> {"friends": set of names}
snaps = []          # {"id","to","from","file","caption","ttl","ts","destroyed"}
chat = {}           # (a,b) sorted -> {"rev":int,"msgs":[{id,from,to,text,ttl,ts,seen}]}
last_seen = {}      # (a,b) -> last read rev (unused extra)
locs = {}           # name -> {"lat":float,"lng":float,"ts":int}


def _key(a, b):
    return tuple(sorted([a, b]))


def now():
    return int(time.time() * 1000)


def user_exists(name):
    return name in users


def ensure_user(name):
    if name and name not in users:
        users[name] = {"friends": set()}


def save_media(data_url):
    m = re.match(r"data:([\w/+-]+);base64,(.*)", data_url or "", re.S)
    if not m:
        return None
    mime, b64 = m.group(1), m.group(2)
    ext = "jpg"
    if "png" in mime:
        ext = "png"
    elif "webp" in mime:
        ext = "webp"
    elif "gif" in mime:
        ext = "gif"
    name = uuid.uuid4().hex + "." + ext
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return None
    with open(os.path.join(MEDIA, name), "wb") as f:
        f.write(raw)
    return name


def expire():
    t = now()
    removed = 0
    # timed snaps
    for s in snaps:
        if s["ttl"] > 0 and t - s["ts"] >= s["ttl"] * 1000:
            s["destroyed"] = True
    # timed chat messages
    for conv in chat.values():
        keep = []
        for m in conv["msgs"]:
            if m["ttl"] > 0 and t - m["ts"] >= m["ttl"] * 1000:
                removed += 1
            else:
                keep.append(m)
        conv["msgs"] = keep
    return removed


def save_media_remove(name):
    try:
        os.remove(os.path.join(MEDIA, name))
    except OSError:
        pass


def find_html():
    cand = os.path.join(HERE, "app.html")
    return cand if os.path.isfile(cand) else None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            self.send_json({"error": "not found"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            return {}
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:
            return {}

    # ---------------- routes ----------------

    def api_login(self, body):
        name = str(body.get("name", "")).strip()
        if not name:
            return self.send_json({"error": "Name required"}, 400)
        with lock:
            ensure_user(name)
            return self.send_json({"ok": True, "friends": sorted(users[name]["friends"])})

    def api_friends(self, body):
        name = str(body.get("user", "")).strip()
        with lock:
            ensure_user(name)
            return self.send_json({"friends": sorted(users[name]["friends"])})

    def api_friends_add(self, body):
        user = str(body.get("user", "")).strip()
        friend = str(body.get("name", "")).strip()
        if not user or not friend:
            return self.send_json({"error": "both names required"}, 400)
        with lock:
            ensure_user(user)
            ensure_user(friend)
            users[user]["friends"].add(friend)
            users[friend]["friends"].add(user)
            return self.send_json({"friends": sorted(users[user]["friends"])})

    def api_snap(self, body):
        to = str(body.get("to", "")).strip()
        frm = str(body.get("from", "")).strip()
        img = body.get("img", "")
        caption = str(body.get("caption", ""))[:200]
        try:
            ttl = int(body.get("timer", -1))
        except (TypeError, ValueError):
            ttl = -1
        if not to or not frm or not img:
            return self.send_json({"error": "to/from/img required"}, 400)
        with lock:
            ensure_user(frm)
            ensure_user(to)
            fn = save_media(img)
            if not fn:
                return self.send_json({"error": "bad image"}, 400)
            snap = {
                "id": uuid.uuid4().hex,
                "to": to,
                "from": frm,
                "file": fn,
                "caption": caption,
                "ttl": ttl,
                "ts": now(),
                "destroyed": False,
            }
            snaps.append(snap)
            return self.send_json({"ok": True, "id": snap["id"]})

    def api_stories(self, body):
        user = str(body.get("user", "")).strip()
        with lock:
            expire()
            out = []
            for s in snaps:
                if s["to"] == user and not s["destroyed"]:
                    if s["ttl"] < 0 or now() - s["ts"] < s["ttl"] * 1000:
                        out.append({
                            "id": s["id"], "from": s["from"],
                            "caption": s["caption"], "ttl": s["ttl"],
                            "img": "/media/" + s["file"],
                        })
            out.sort(key=lambda s: s["ts"] if "ts" in s else 0)
            return self.send_json({"stories": out})

    def api_view(self, body):
        sid = str(body.get("id", "")).strip()
        with lock:
            for s in snaps:
                if s["id"] == sid and not s["destroyed"]:
                    destroy = s["ttl"] < 0
                    data = {
                        "id": s["id"], "from": s["from"], "caption": s["caption"],
                        "img": "/media/" + s["file"],
                        "destroy": destroy,
                    }
                    if destroy:
                        s["destroyed"] = True
                    return self.send_json(data)
            return self.send_json({"error": "gone"}, 404)

    def api_chat_send(self, body):
        frm = str(body.get("from", "")).strip()
        to = str(body.get("to", "")).strip()
        text = str(body.get("text", ""))[:2000]
        try:
            ttl = int(body.get("ttl", 0))
        except (TypeError, ValueError):
            ttl = 0
        if not frm or not to or not text:
            return self.send_json({"error": "from/to/text required"}, 400)
        loc = None
        try:
            lat = float(body.get("lat"))
            lng = float(body.get("lng"))
            if -90 <= lat <= 90 and -180 <= lng <= 180:
                loc = {"lat": lat, "lng": lng}
        except (TypeError, ValueError):
            pass
        with lock:
            ensure_user(frm)
            ensure_user(to)
            k = _key(frm, to)
            conv = chat.setdefault(k, {"rev": 0, "msgs": []})
            conv["rev"] += 1
            conv["msgs"].append({
                "id": uuid.uuid4().hex, "from": frm, "to": to,
                "text": text, "ttl": ttl, "ts": now(), "seen": False,
                "loc": loc,
            })
            return self.send_json({"ok": True, "rev": conv["rev"]})

    def api_chat_history(self, body):
        a = str(body.get("user", "")).strip()
        b = str(body.get("other", "")).strip()
        with lock:
            expire()
            conv = chat.get(_key(a, b))
            if not conv:
                return self.send_json({"messages": [], "rev": 0})
            return self.send_json({"messages": conv["msgs"], "rev": conv["rev"]})

    def api_chat_read(self, body):
        user = str(body.get("user", "")).strip()
        other = str(body.get("other", "")).strip()
        with lock:
            conv = chat.get(_key(user, other))
            if conv:
                conv["msgs"] = [m for m in conv["msgs"]
                                if not (m["from"] == other and m["ttl"] == -1)]
                return self.send_json({"ok": True, "rev": conv["rev"]})
            return self.send_json({"ok": True, "rev": 0})

    def api_location_update(self, body):
        name = str(body.get("user", "")).strip()
        try:
            lat = float(body.get("lat"))
            lng = float(body.get("lng"))
        except (TypeError, ValueError):
            return self.send_json({"error": "lat/lng required"}, 400)
        if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
            return self.send_json({"error": "invalid coords"}, 400)
        with lock:
            ensure_user(name)
            locs[name] = {"lat": lat, "lng": lng, "ts": now()}
            return self.send_json({"ok": True})

    def api_locations(self, body):
        user = str(body.get("user", "")).strip()
        with lock:
            ensure_user(user)
            names = sorted(users[user]["friends"]) + [user]
            out = {}
            for n in names:
                if n in locs:
                    out[n] = locs[n]
            return self.send_json({"locations": out})

    # ---------------- dispatch ----------------

    def do_GET(self):
        parts = urlparse(self.path)
        path = parts.path
        if path in ("/", "/index.html", "/app.html"):
            html = find_html()
            if not html:
                return self.send_json({"error": "app.html missing"}, 500)
            return self.send_file(html, "text/html; charset=utf-8")
        if path.startswith("/media/"):
            name = path.split("/")[-1]
            if "/" in name or "\\" in name or not name:
                return self.send_json({"error": "bad"}, 400)
            return self.send_file(os.path.join(MEDIA, name),
                                  "image/jpeg" if name.endswith(".jpg")
                                  else "image/png" if name.endswith(".png")
                                  else "application/octet-stream")
        if path == "/favicon.svg":
            return self.send_file(os.path.join(HERE, "favicon.svg"),
                                  "image/svg+xml")
        return self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        body = self.read_body()
        path = urlparse(self.path).path
        routes = {
            "/api/login": self.api_login,
            "/api/friends": self.api_friends,
            "/api/friends/add": self.api_friends_add,
            "/api/snap": self.api_snap,
            "/api/stories": self.api_stories,
            "/api/view": self.api_view,
            "/api/chat/send": self.api_chat_send,
            "/api/chat/history": self.api_chat_history,
            "/api/chat/read": self.api_chat_read,
            "/api/location": self.api_location_update,
            "/api/locations": self.api_locations,
        }
        fn = routes.get(path)
        if fn:
            return fn(body)
        return self.send_json({"error": "not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def main():
    global PORT, HOST
    args = sys.argv[1:]
    if len(args) >= 1:
        try:
            PORT = int(args[0])
        except ValueError:
            print("Invalid port, using 8001.")
    if len(args) >= 2:
        HOST = args[1]
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print("SnapChat server running")
    print("  On this computer:  http://localhost:{0}".format(PORT))
    print("  From phone/tablet: http://<this-computer-ip>:{0}".format(PORT))
    print("  Stop with Ctrl+C")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nShutdown.")


if __name__ == "__main__":
    main()
