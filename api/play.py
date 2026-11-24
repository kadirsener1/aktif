from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import urllib.request
import json
import time

CHANNELS_URL = "https://raw.githubusercontent.com/kadirsener1/aktif/refs/heads/main/channels.json"
CACHE_TTL = 60  # Saniye cinsinden JSON önbellek süresi

_cache = {
    "data": None,
    "expires_at": 0.0,
}


def get_channels() -> dict:
    now = time.time()
    if _cache["data"] is not None and now < _cache["expires_at"]:
        return _cache["data"]

    req = urllib.request.Request(
        CHANNELS_URL,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=4.0) as resp:
        raw = resp.read().decode("utf-8")
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            _cache["data"] = parsed
            _cache["expires_at"] = now + CACHE_TTL
            return parsed

    return _cache["data"] or {}


def is_stream_working(url: str, timeout: float = 2.5) -> bool:
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "User-Agent": "VLC/3.0.18 LibVLC/3.0.18",
            "Range": "bytes=0-1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status in (200, 206)
    except Exception:
        return False


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        channel = qs.get("channel", [None])[0]

        if not channel and parsed.path.startswith("/play/"):
            channel = parsed.path.split("/play/", 1)[1].strip("/")

        try:
            channels = get_channels()
        except Exception:
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("channels.json okunamadi.".encode("utf-8"))
            return

        if not channel or channel not in channels:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Kanal bulunamadi.".encode("utf-8"))
            return

        urls = channels[channel]
        if not isinstance(urls, list) or not urls:
            self.send_response(503)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Kanal icin link tanimli degil.".encode("utf-8"))
            return

        for url in urls:
            if is_stream_working(url):
                self.send_response(302)
                self.send_header("Location", url)
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                self.end_headers()
                return

        self.send_response(503)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Calisan yayin linki bulunamadi.".encode("utf-8"))
