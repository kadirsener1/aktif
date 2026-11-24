from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import urllib.request
import json
import time
import traceback

CHANNELS_URL = (
    "https://raw.githubusercontent.com/kadirsener1/aktif/refs/heads/main/channels.json"
)
CACHE_TTL = 60

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
        raw = resp.read().decode("utf-8", errors="replace")
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
    def _send_text(self, code: int, text: str, extra_headers: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(text.encode("utf-8"))

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            channel = qs.get("channel", [None])[0]

            if not channel and parsed.path.startswith("/play/"):
                channel = parsed.path.split("/play/", 1)[1].strip("/")

            try:
                channels = get_channels()
            except Exception as exc:
                self._send_text(502, f"channels.json okunamadi: {exc}")
                return

            if not channel or channel not in channels:
                self._send_text(404, "Kanal bulunamadi.")
                return

            urls = channels[channel]
            if not isinstance(urls, list) or not urls:
                self._send_text(503, "Kanal icin link tanimli degil.")
                return

            for url in urls:
                if isinstance(url, str) and url.strip() and is_stream_working(url.strip()):
                    self.send_response(302)
                    self.send_header("Location", url.strip())
                    self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                    self.end_headers()
                    return

            self._send_text(503, "Calisan yayin linki bulunamadi.")
        except Exception:
            self._send_text(500, traceback.format_exc())
