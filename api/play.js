const CHANNELS_URL =
  "https://raw.githubusercontent.com/kadirsener1/aktif/refs/heads/main/channels.json";

const CHANNELS_CACHE_TTL_MS = 5 * 60 * 1000; // channels.json önbelleği: 5 dakika
const WORKING_URL_TTL_MS = 5 * 60 * 1000; // Çalışan kanal linki önbelleği: 5 dakika
const MIN_REQUEST_INTERVAL_MS = 1500; // Aynı IP+kanal için minimum istek aralığı: 1.5 saniye

let channelsCache = {
  data: null,
  expiresAt: 0,
};

// { [channel]: { url: string, expiresAt: number } }
const workingUrlCache = new Map();

// { [`${ip}:${channel}`]: number }
const lastHitMap = new Map();

function getClientIp(req) {
  const fwd = req.headers["x-forwarded-for"];
  if (typeof fwd === "string" && fwd.length > 0) {
    return fwd.split(",")[0].trim();
  }
  return req.socket?.remoteAddress || "unknown";
}

function cleanupMaps(now) {
  for (const [key, entry] of workingUrlCache.entries()) {
    if (now >= entry.expiresAt) {
      workingUrlCache.delete(key);
    }
  }
  for (const [key, ts] of lastHitMap.entries()) {
    if (now - ts > 60 * 1000) {
      lastHitMap.delete(key);
    }
  }
}

async function getChannels() {
  const now = Date.now();
  if (channelsCache.data && now < channelsCache.expiresAt) {
    return channelsCache.data;
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 3500);

  try {
    const res = await fetch(CHANNELS_URL, {
      headers: {
        "User-Agent": "Mozilla/5.0",
      },
      signal: controller.signal,
    });
    clearTimeout(timeout);

    if (!res.ok) {
      throw new Error(`channels.json HTTP ${res.status}`);
    }

    const parsed = await res.json();
    if (parsed && typeof parsed === "object") {
      channelsCache = {
        data: parsed,
        expiresAt: now + CHANNELS_CACHE_TTL_MS,
      };
      return parsed;
    }

    return channelsCache.data || {};
  } catch (err) {
    clearTimeout(timeout);
    if (channelsCache.data) {
      return channelsCache.data;
    }
    throw err;
  }
}

async function isStreamWorking(url, timeoutMs = 2000) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const res = await fetch(url, {
      method: "GET",
      headers: {
        "User-Agent": "VLC/3.0.18 LibVLC/3.0.18",
        "Range": "bytes=0-1",
      },
      redirect: "follow",
      signal: controller.signal,
    });
    clearTimeout(timeout);
    return res.ok || res.status === 206;
  } catch {
    clearTimeout(timeout);
    return false;
  }
}

export default async function handler(req, res) {
  try {
    const now = Date.now();
    cleanupMaps(now);

    let channel = req.query?.channel;
    if (!channel && req.url) {
      const pathOnly = req.url.split("?")[0];
      const match = pathOnly.match(/^\/play\/([^/?#]+)/);
      if (match) {
        channel = decodeURIComponent(match[1]);
      }
    }

    if (!channel) {
      return res.status(400).send("Kanal parametresi eksik.");
    }

    const ip = getClientIp(req);
    const rateKey = `${ip}:${channel}`;
    const prevHit = lastHitMap.get(rateKey) || 0;
    if (now - prevHit < MIN_REQUEST_INTERVAL_MS) {
      return res.status(429).send("Cok fazla istek gonderildi.");
    }
    lastHitMap.set(rateKey, now);

    let channels;
    try {
      channels = await getChannels();
    } catch (err) {
      return res.status(502).send(`channels.json okunamadi: ${err.message}`);
    }

    if (!Object.prototype.hasOwnProperty.call(channels, channel)) {
      return res.status(404).send("Kanal bulunamadi.");
    }

    const rawUrls = channels[channel];
    if (!Array.isArray(rawUrls) || rawUrls.length === 0) {
      return res.status(503).send("Kanal icin link tanimli degil.");
    }

    const validUrls = rawUrls
      .filter((u) => typeof u === "string" && u.trim())
      .map((u) => u.trim());

    if (validUrls.length === 0) {
      return res.status(503).send("Gecerli link bulunamadi.");
    }

    // 1. Önce önbellekteki çalışan linki kontrol et (ve listede hala varsa)
    const cachedWorking = workingUrlCache.get(channel);
    if (
      cachedWorking &&
      now < cachedWorking.expiresAt &&
      validUrls.includes(cachedWorking.url)
    ) {
      if (await isStreamWorking(cachedWorking.url)) {
        res.setHeader("Cache-Control", "no-store, no-cache, must-revalidate");
        return res.redirect(302, cachedWorking.url);
      }
      workingUrlCache.delete(channel);
    }

    // 2. Sırayla diğer linkleri dene ve ilk çalışanı önbelleğe al
    for (const url of validUrls) {
      if (await isStreamWorking(url)) {
        workingUrlCache.set(channel, {
          url,
          expiresAt: now + WORKING_URL_TTL_MS,
        });
        res.setHeader("Cache-Control", "no-store, no-cache, must-revalidate");
        return res.redirect(302, url);
      }
    }

    return res.status(503).send("Calisan yayin linki bulunamadi.");
  } catch (err) {
    return res.status(500).send(`Sunucu hatasi: ${err.message}`);
  }
}
