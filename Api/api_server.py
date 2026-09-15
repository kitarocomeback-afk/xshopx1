"""
CardCheckout API — Server Entry Point
======================================
Drop-in replacement for newss API.
bot.py compatible — Response/Price/Gate format မှန်ကန်.
"""

import os
import time
import asyncio
import concurrent.futures
import functools
import logging
import threading
from typing import Optional, Tuple

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from engine import (
    run_checkout_for_card,
    normalize_proxy,
    parse_card_entry,
    CheckStatus,
    GATE_NAME,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("api")

# ── Config ────────────────────────────────────────────────────────────
POOL_SIZE         = int(os.environ.get("POOL_SIZE", "500"))
POOL_PER_HOST     = int(os.environ.get("POOL_PER_HOST", "25"))
SITE_CONCURRENCY  = int(os.environ.get("SITE_CONCURRENCY", "15"))
CACHE_TTL         = int(os.environ.get("CACHE_TTL", "300"))
MAX_PRICE         = float(os.environ.get("MAX_PRICE", "20"))
CHECKER_THREADS   = int(os.environ.get("CHECKER_THREADS", "200"))
CHECKER_RETRIES   = int(os.environ.get("CHECKER_RETRIES", "1"))

_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=CHECKER_THREADS,
    thread_name_prefix="chk",
)

# ── Counters ──────────────────────────────────────────────────────────
_stats_lock = threading.Lock()
_stats = {
    "total_requests": 0,
    "requests_shopify": 0,
    "requests_health": 0,
    "requests_stats": 0,
    "requests_root": 0,
    "requests_docs": 0,
    "response_ORDER_PLACED": 0,
    "response_3DS_REQUIRED": 0,
    "response_CARD_DECLINED": 0,
    "response_EXPIRED_CARD": 0,
    "response_INVALID_CARD": 0,
    "response_INVALID_CVC": 0,
    "response_INSUFFICIENT_FUNDS": 0,
    "response_CART_FAILED": 0,
    "response_NO_PRODUCT": 0,
    "response_PRICE_OVER_MAX": 0,
    "response_TOKENIZATION_FAILED": 0,
    "response_NO_SESSION_TOKEN": 0,
    "response_SUBMIT_FAILED": 0,
    "response_CAPTCHA_REQUIRED": 0,
    "response_TIMEOUT": 0,
    "response_ERROR": 0,
    "cards_loaded": 0,
}


def _bump(key, n=1):
    with _stats_lock:
        _stats[key] = _stats.get(key, 0) + n


def _bump_response(code: str):
    if not code:
        code = "ERROR"
    key = f"response_{code}"
    with _stats_lock:
        _stats[key] = _stats.get(key, 0) + 1


# ── Cache ─────────────────────────────────────────────────────────────
_cache_lock = threading.Lock()
_cache = {}


def _cache_get(key: str) -> Optional[dict]:
    with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        ts, val = entry
        if time.time() - ts > CACHE_TTL:
            del _cache[key]
            return None
        return val


def _cache_set(key: str, val: dict):
    with _cache_lock:
        _cache[key] = (time.time(), val)
        if len(_cache) > 5000:
            now = time.time()
            dead = [k for k, (ts, _) in _cache.items() if now - ts > CACHE_TTL]
            for k in dead:
                _cache.pop(k, None)


# ── Site concurrency ─────────────────────────────────────────────────
_site_sems_lock = threading.Lock()
_site_sems = {}


def _get_site_sem(site: str) -> asyncio.Semaphore:
    key = site.lower()
    with _site_sems_lock:
        sem = _site_sems.get(key)
        if sem is None:
            sem = asyncio.Semaphore(SITE_CONCURRENCY)
            _site_sems[key] = sem
        return sem


# ── FastAPI app ───────────────────────────────────────────────────────
app = FastAPI(
    title="CardCheckout API",
    version="2.0.0",
    description="Shopify card-check API — bot.py compatible.",
    docs_url=None,
    redoc_url=None,
)


_DOCS_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>CardCheckout API</title>
<style>
body{background:#03030a;color:#f1f5f9;font-family:Inter,sans-serif;padding:40px;line-height:1.6}
h1{font-size:32px;font-weight:800;background:linear-gradient(135deg,#a78bfa,#38bdf8,#34d399);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
code{background:#1e1e2e;padding:2px 8px;border-radius:5px;font-family:monospace;color:#a78bfa}
pre{background:#0a0a15;border:1px solid #1e1e2e;border-radius:10px;padding:18px;overflow-x:auto;color:#e2e8f0}
.card{background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.07);border-radius:14px;padding:22px;margin:18px 0}
</style></head><body>
<h1>CardCheckout API v2.0</h1>
<p>Shopify checkout engine. Bot.py compatible.</p>

<div class="card"><h2><code>GET /shopify</code></h2>
<pre>curl "http://localhost:8000/shopify?site=https://store.myshopify.com&cc=4111111111111111|12|2026|123&proxy=http://user:pass@host:port"</pre>
</div>

<div class="card"><h2><code>GET /health</code></h2>
<pre>curl http://localhost:8000/health</pre>
</div>

<div class="card"><h2><code>GET /stats</code></h2>
<pre>curl http://localhost:8000/stats</pre>
</div>

<div class="card"><h2>Response Format</h2>
<pre>{
  "Response": "ORDER_PLACED | CARD_DECLINED | 3DS_REQUIRED | ...",
  "CC": "4111111111111111|12|2026|123",
  "Price": "1.99 USD",
  "Gate": "Shopify Payments",
  "Site": "https://store.myshopify.com",
  "Charged": "True | False",
  "Approved": "True | False",
  "Time": "4.23s"
}</pre>
</div>

</body></html>"""


# ── Request/Response models ───────────────────────────────────────────
class CheckResponse(BaseModel):
    """bot.py compatible response format."""
    Response: str = "ERROR"
    CC:       str = ""
    Price:    str = ""
    Gate:     str = "Shopify Payments"
    Site:     str = ""
    Charged:  str = "False"
    Approved: str = "False"
    Time:     str = "0.00s"


# ── Validators ────────────────────────────────────────────────────────
def _validate_card(raw: str) -> Tuple[Optional[str], Optional[str]]:
    if not raw or not raw.strip():
        return None, "CARD_REQUIRED"
    try:
        parse_card_entry(raw)
    except Exception as e:
        return None, f"INVALID_CARD: {e}"
    return raw.strip(), None


def _validate_url(raw: str) -> Tuple[Optional[str], Optional[str]]:
    if not raw or not raw.strip():
        return None, "URL_REQUIRED"
    url = raw.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url, None


def _validate_proxy(raw: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if raw is None:
        return None, None
    raw = raw.strip()
    if not raw:
        return None, None
    try:
        return normalize_proxy(raw), None
    except Exception as e:
        return None, f"PROXY_INVALID: {e}"


# ── Runner ────────────────────────────────────────────────────────────
def _run_check_sync(site: str, cc: str, proxy: Optional[str]) -> CheckResponse:
    t0 = time.perf_counter()
    try:
        fn = functools.partial(run_checkout_for_card, site, cc, proxy or "", True)
        res = fn()
    except Exception as exc:
        logger.warning("engine exception: %s", exc)
        return CheckResponse(
            Response="ERROR",
            CC=cc,
            Site=site,
            Charged="False",
            Approved="False",
            Time=f"{time.perf_counter() - t0:.2f}s",
        )

    status_name = res.status.name
    status_code = res.status_code or status_name

    # Map CheckStatus → Response
    if res.status == CheckStatus.CHARGED:
        response_str = "ORDER_PLACED"
        charged = "True"
        approved = "False"
    elif res.status == CheckStatus.APPROVED:
        response_str = status_code if status_code else "3DS_REQUIRED"
        charged = "False"
        approved = "True"
    elif res.status == CheckStatus.DECLINED:
        response_str = status_code if status_code else "CARD_DECLINED"
        charged = "False"
        approved = "False"
    else:  # ERROR
        response_str = status_code if status_code else "ERROR"
        charged = "False"
        approved = "False"

    # ⭐ Price format — " USD" မပါအောင်
    price_str = ""
    if res.amount:
        amount_clean = str(res.amount).replace(" USD", "").replace("$", "").strip()
        price_str = f"{amount_clean} USD" if amount_clean else ""

    elapsed = time.perf_counter() - t0
    return CheckResponse(
        Response=response_str,
        CC=res.card or cc,
        Price=price_str,
        Gate=GATE_NAME,
        Site=site,
        Charged=charged,
        Approved=approved,
        Time=f"{elapsed:.2f}s",
    )


async def _run_check(site: str, cc: str, proxy: Optional[str]) -> CheckResponse:
    loop = asyncio.get_event_loop()
    sem = _get_site_sem(site)
    async with sem:
        return await loop.run_in_executor(
            _pool,
            _run_check_sync,
            site, cc, proxy,
        )


# ── Routes ────────────────────────────────────────────────────────────
@app.get("/docs", include_in_schema=False)
async def custom_docs():
    _bump("requests_docs")
    return HTMLResponse(_DOCS_HTML)


@app.get("/health", tags=["meta"])
async def health():
    _bump("requests_health")
    _bump("total_requests")
    return {
        "status": "ok",
        "cards_loaded": _stats.get("cards_loaded", 0),
        "pool_size": POOL_SIZE,
        "pool_per_host": POOL_PER_HOST,
        "site_concurrency": SITE_CONCURRENCY,
        "cache_ttl": CACHE_TTL,
        "max_price": int(MAX_PRICE),
    }


@app.get("/", include_in_schema=False)
async def root():
    _bump("requests_root")
    _bump("total_requests")
    return {
        "status": "ok",
        "service": "CardCheckout API",
        "endpoints": ["/shopify", "/health", "/stats", "/docs"],
    }


@app.get("/stats", tags=["meta"])
async def stats():
    _bump("requests_stats")
    _bump("total_requests")
    with _stats_lock:
        return dict(_stats)


@app.get("/shopify", response_model=CheckResponse, tags=["check"])
async def shopify(
    site: str = Query(..., description="Shopify store URL"),
    cc: str = Query(..., description="Card: number|mm|yyyy|cvv"),
    proxy: Optional[str] = Query(None, description="Proxy: ip:port:user:pass or http://user:pass@host:port"),
):
    """
    Main check endpoint — bot.py compatible.
    """
    _bump("requests_shopify")
    _bump("total_requests")

    # validate
    card_val, card_err = _validate_card(cc)
    if card_err:
        _bump_response("INVALID_CARD")
        return CheckResponse(Response=card_err, CC=cc, Site=site)

    url_val, url_err = _validate_url(site)
    if url_err:
        _bump_response("ERROR")
        return CheckResponse(Response=url_err, CC=cc, Site=site)

    proxy_val, proxy_err = _validate_proxy(proxy)
    if proxy_err:
        _bump_response("ERROR")
        return CheckResponse(Response=proxy_err, CC=cc, Site=url_val)

    # cache key
    cache_key = f"{url_val}|{card_val}|{proxy_val or ''}"
    cached = _cache_get(cache_key)
    if cached:
        return CheckResponse(**cached)

    # run
    res = await _run_check(url_val, card_val, proxy_val)

    # cache (only successful responses)
    if res.Response and res.Response != "ERROR":
        _cache_set(cache_key, res.dict())

    # bump stats
    _bump_response(res.Response.split(":")[0].strip())

    return res


# ── Standalone runner ─────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    logger.info(
        "CardCheckout API — port=%d threads=%d site_conc=%d cache_ttl=%d max_price=$%.2f",
        port, CHECKER_THREADS, SITE_CONCURRENCY, CACHE_TTL, MAX_PRICE
    )
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")