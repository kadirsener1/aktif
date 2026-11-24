import json
import time
from typing import Optional
import requests
from fastapi import FastAPI, Response
from fastapi.responses import RedirectResponse, PlainTextResponse

CHANNELS_URL = (
    "https://raw.githubusercontent.com/kadirsener1/aktif/refs/heads/main/channels.json"
)
CACHE_TTL = 60

app = FastAPI()

_cache = {
    "data": None,
    "expires_at": 0.0,
}


def get_channels() -> dict:
    now = time.time()
    if _cache["data"] is not None and now < _cache["expires_at"]:
        return _cache["data"]

    resp = requests.get(
        CHANNELS_URL,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Cache-Control": "no-cache",
        },
        timeout=4.0,
    )
    resp.raise_for_status()
    parsed = resp.json()
    if isinstance(parsed, dict):
        _cache["data"] = parsed
        _cache["expires_at"] = now + CACHE_TTL
        return parsed

    return _cache["data"] or {}


def is_stream_working(url: str, timeout: float = 2.5) -> bool:
    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": "VLC/3.0.18 LibVLC/3.0.18",
                "Range": "bytes=0-1",
            },
            stream=True,
            allow_redirects=True,
            timeout=timeout,
            verify=False,
        )
        ok = resp.status_code in (200, 206)
        resp.close()
        return ok
    except Exception:
        return False


@app.get("/play/{channel}")
def play_channel(channel: str, response: Response):
    try:
        channels = get_channels()
    except Exception as exc:
        return PlainTextResponse(f"channels.json okunamadi: {exc}", status_code=502)

    if not channel or channel not in channels:
        return PlainTextResponse("Kanal bulunamadi.", status_code=404)

    urls = channels[channel]
    if not isinstance(urls, list) or not urls:
        return PlainTextResponse("Kanal icin link tanimli degil.", status_code=503)

    for url in urls:
        if isinstance(url, str) and url.strip() and is_stream_working(url.strip()):
            res = RedirectResponse(url=url.strip(), status_code=302)
            res.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            return res

    return PlainTextResponse("Calisan yayin linki bulunamadi.", status_code=503)
