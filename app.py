"""
Clipforge Local — paste a video link, get vertical short clips.

Website version: runs on a server (Render etc.) via the Dockerfile.

Only clip videos you own or have permission to use.
"""
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from array import array
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

HOST, PORT = "0.0.0.0", int(os.environ.get("PORT", "8765"))
MAX_SECONDS = int(os.environ.get("MAX_SECONDS", "5400"))  # longest video accepted (90 min)
KEEP_SECONDS = 3600  # files are deleted after 1 hour
BASE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(BASE, "work")
CLIPS = os.path.join(BASE, "clips")
os.makedirs(WORK, exist_ok=True)
os.makedirs(CLIPS, exist_ok=True)

JOBS = {}  # id -> dict(status, progress, title, error, energy, file)
LOCK = threading.Lock()
ID_RE = re.compile(r"^[a-f0-9]{12}$")
FORMATS = {"9:16": (720, 1280), "1:1": (1080, 1080), "16:9": (1280, 720)}


def set_job(jid, **kw):
    with LOCK:
        JOBS[jid].update(kw)


def compute_energy(path):
    """Loudness per second of audio (mono, 8 kHz)."""
    p = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
        stdout=subprocess.PIPE,
    )
    energy, sr = [], 8000
    while True:
        raw = p.stdout.read(sr * 2)
        if not raw:
            break
        a = array("h")
        a.frombytes(raw[: len(raw) // 2 * 2])
        s = a[::4]
        if s:
            energy.append((sum(x * x for x in s) / len(s)) ** 0.5 / 32768)
    p.wait()
    return energy


def fetch_worker(jid, url):
    folder = os.path.join(WORK, jid)
    os.makedirs(folder, exist_ok=True)
    out = os.path.join(folder, "source.%(ext)s")
    cmd = [
        sys.executable, "-m", "yt_dlp", "--no-playlist", "--newline", "--progress", "--no-simulate",
        "--print", "before_dl:TITLE:%(title)s",
        "-f", "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*[height<=1080]+ba/b",
        "--merge-output-format", "mp4", "--match-filter", f"duration<={MAX_SECONDS}",
        "--max-filesize", "1500M", "-o", out, url,
    ]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace")
        last_lines = []
        for line in p.stdout:
            line = line.strip()
            if not line:
                continue
            last_lines = (last_lines + [line])[-6:]
            if line.startswith("TITLE:"):
                set_job(jid, title=line[6:])
            m = re.search(r"\[download\]\s+([\d.]+)%", line)
            if m:
                set_job(jid, progress=float(m.group(1)))
        p.wait()
        src = next((os.path.join(folder, f) for f in os.listdir(folder) if f.startswith("source.")
                    and not f.endswith((".part", ".ytdl"))), None)
        if p.returncode == 0 and not src and any("does not pass filter" in l for l in last_lines):
            raise RuntimeError(f"Video is too long (limit {MAX_SECONDS // 60} minutes)")
        if p.returncode != 0 or not src:
            err = next((l for l in reversed(last_lines) if "ERROR" in l), last_lines[-1] if last_lines else "")
            raise RuntimeError(err or "Download failed")
        set_job(jid, status="analyzing", progress=100, file=src)
        energy = compute_energy(src)
        set_job(jid, status="ready", energy=energy)
    except Exception as e:  # noqa: BLE001
        set_job(jid, status="error", error=str(e)[:400])


def safe_name(s):
    s = re.sub(r"[^\w-]+", "_", s or "clip").strip("_")
    return (s or "clip")[:40]


def export_clip(jid, start, end, fmt, pos):
    job = JOBS.get(jid)
    if not job or job.get("status") != "ready":
        raise ValueError("Video not ready")
    if fmt not in FORMATS:
        raise ValueError("Bad format")
    start, end, pos = float(start), float(end), min(1.0, max(0.0, float(pos)))
    if end - start < 1 or start < 0:
        raise ValueError("Clip must be at least 1 second")
    W, H = FORMATS[fmt]
    rw, rh = (9, 16) if fmt == "9:16" else (1, 1) if fmt == "1:1" else (16, 9)
    vf = (f"crop=w='min(iw,ih*{rw}/{rh})':h='min(ih,iw*{rh}/{rw})':"
          f"x='(iw-ow)*{pos:.3f}':y='(ih-oh)/2',scale={W}:{H},setsar=1")
    name = f"{safe_name(job.get('title'))}_{int(start)}-{int(end)}_{fmt.replace(':', 'x')}.mp4"
    dst = os.path.join(CLIPS, name)
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}", "-to", f"{end:.2f}", "-i", job["file"],
           "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", dst]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-400:] or "ffmpeg failed")
    return name


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def send_file(self, path, download_name=None):
        if not os.path.isfile(path):
            self.send_error(404)
            return
        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range")
        m = re.match(r"bytes=(\d*)-(\d*)", rng or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self.send_file(os.path.join(BASE, "index.html"))
        m = re.match(r"^/api/status/([a-f0-9]{12})$", path)
        if m:
            job = JOBS.get(m.group(1))
            if not job:
                return self.send_json({"error": "Unknown job"}, 404)
            out = {k: job.get(k) for k in ("status", "progress", "title", "error")}
            if job.get("status") == "ready":
                out["energy"] = job["energy"]
            return self.send_json(out)
        m = re.match(r"^/video/([a-f0-9]{12})$", path)
        if m and JOBS.get(m.group(1), {}).get("file"):
            return self.send_file(JOBS[m.group(1)]["file"])
        m = re.match(r"^/clips/([\w.-]+\.mp4)$", path)
        if m:
            return self.send_file(os.path.join(CLIPS, m.group(1)), download_name=m.group(1))
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = self.read_json()
        except Exception:  # noqa: BLE001
            return self.send_json({"error": "Bad request"}, 400)
        if path == "/api/fetch":
            url = str(data.get("url", "")).strip()
            if not re.match(r"^https?://", url):
                return self.send_json({"error": "Paste a full link starting with https://"}, 400)
            jid = uuid.uuid4().hex[:12]
            with LOCK:
                JOBS[jid] = {"status": "downloading", "progress": 0, "title": "", "error": None}
            threading.Thread(target=fetch_worker, args=(jid, url), daemon=True).start()
            return self.send_json({"id": jid})
        if path == "/api/export":
            jid = str(data.get("id", ""))
            if not ID_RE.match(jid):
                return self.send_json({"error": "Bad id"}, 400)
            try:
                name = export_clip(jid, data.get("start"), data.get("end"), data.get("fmt", "9:16"),
                                   data.get("pos", 0.5))
                return self.send_json({"url": "/clips/" + name, "name": name})
            except Exception as e:  # noqa: BLE001
                return self.send_json({"error": str(e)}, 400)
        self.send_error(404)


def cleanup_loop():
    while True:
        cutoff = time.time() - KEEP_SECONDS
        for root in (WORK, CLIPS):
            for name in os.listdir(root):
                path = os.path.join(root, name)
                try:
                    if os.path.getmtime(path) < cutoff:
                        shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
                except OSError:
                    pass
        with LOCK:
            for jid in [j for j, v in JOBS.items() if v.get("file") and not os.path.exists(v["file"])]:
                JOBS.pop(jid, None)
        time.sleep(300)


def main():
    threading.Thread(target=cleanup_loop, daemon=True).start()
    print(f"Clipforge web listening on port {PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
