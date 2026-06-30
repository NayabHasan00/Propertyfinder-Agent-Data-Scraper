import asyncio
import json
import re
import os
import random
import logging
from typing import List, Dict, Any, Optional, AsyncGenerator
from urllib.parse import urlparse, urlunparse

from playwright.async_api import async_playwright, BrowserContext, Page, Route

from .base import BaseScraper
# Note: Strictly importing only the required models for Requirement 7
from models import Listing, LocationData, PropertyDetailsData, AgentData

logger = logging.getLogger(__name__)

STEALTH_INIT_SCRIPT = """
(() => {
  Object.defineProperty(navigator, 'plugins', {
    get: () => {
      const makePlugin = (name, filename, desc, mimeType, suffix) => {
        const mt = { type: mimeType, suffixes: suffix, description: desc, enabledPlugin: null };
        const plugin = { name, filename, description: desc, length: 1, 0: mt, item: i => (i === 0 ? mt : null), namedItem: n => (n === mimeType ? mt : null) };
        mt.enabledPlugin = plugin;
        return plugin;
      };
      const plugins = [
        makePlugin('PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format', 'application/pdf', 'pdf'),
        makePlugin('Chrome PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format', 'application/pdf', 'pdf')
      ];
      plugins.length = plugins.length;
      plugins.item = i => plugins[i] || null;
      plugins.namedItem = n => plugins.find(p => p.name === n) || null;
      plugins[Symbol.iterator] = Array.prototype[Symbol.iterator].bind(plugins);
      return plugins;
    }
  });
  Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en', 'ar'] });
  if (!window.chrome) {
    window.chrome = { app: { isInstalled: false }, runtime: {} };
  }
})();
"""

BLOCKED_TYPES = {"image", "media", "font", "stylesheet"}
BLOCKED_URL_FRAGMENTS = (
    "google-analytics", "googletagmanager", "hotjar", "clarity.ms",
    "facebook.net", "doubleclick", "segment.io", "sentry.io",
    "amplitude", "mixpanel", "optimizely", "newrelic",
)

# BOTH RETRIES SET TO 7 AS REQUESTED
MIN_LISTINGS_THRESHOLD = 5
MAX_PAGE_RETRIES = 7
MAX_DETAIL_RETRIES = 7  

def clean_text(text: Any) -> Optional[str]:
    if not text:
        return None
    text = re.sub(r'<[^>]+>', '', str(text))
    text = text.replace("&nbsp;", " ").replace("\n", " ").replace("\r", " ")
    text = re.sub(r'\s+', ' ', text).strip()
    return text or None

def _fuzzy_extract_property(root_obj: Any) -> Optional[dict]:
    queue = [root_obj]
    best, max_score = None, 0
    while queue:
        current = queue.pop(0)
        if isinstance(current, dict):
            # Direct Apollo hit fallback
            if current.get("__typename") in ["Property", "Listing"] and current.get("id"):
                return current
                
            score = sum([
                bool(isinstance(current.get("description"), str) and len(current.get("description", "")) > 10),
                bool(current.get("price") or current.get("rentPrice")),
                bool(current.get("images") or current.get("photos")),
                bool(current.get("amenities") or current.get("features")),
                bool(current.get("location_tree") or current.get("location")),
                bool(current.get("property_type") or current.get("propertyType")),
            ])
            if score > max_score and score >= 2:
                max_score, best = score, current
            for v in current.values():
                if isinstance(v, (dict, list)):
                    queue.append(v)
        elif isinstance(current, list):
            queue.extend(item for item in current if isinstance(item, (dict, list)))
    return best

def _parse_listing(property_data: dict, url: str) -> Optional[Listing]:
    if not property_data:
        return None

    price_obj = property_data.get("price") or property_data.get("min_price") or {}
    price_val = 0.0
    currency_val = "AED"
    try:
        if isinstance(price_obj, dict):
            price_val = float(price_obj.get("value", price_obj.get("amount", 0.0)) or 0.0)
            currency_val = clean_text(price_obj.get("currency", "AED")) or "AED"
        elif isinstance(price_obj, (int, float)):
            price_val = float(price_obj)
        elif isinstance(price_obj, str):
            price_val = float(re.sub(r'[^\d.]', '', price_obj) or 0.0)
    except Exception:
        pass

    final_title = clean_text(str(property_data.get("title", property_data.get("name", "Unknown Title"))))

    match = re.search(r'-(\d{6,15})', url)
    property_id = match.group(1) if match else "unknown"
    listing_id = str(property_data.get("id", property_data.get("reference", property_id)))

    if final_title == "Unknown Title" and listing_id == "unknown":
        return None

    loc_obj = property_data.get("location") or property_data.get("geography") or {}
    city_val, loc_val, address_val = None, None, None

    for loc_node in (property_data.get("location_tree") or []):
        if isinstance(loc_node, dict):
            if loc_node.get("type") == "CITY":
                city_val = clean_text(loc_node.get("name"))
            elif loc_node.get("type") in ["SUBCOMMUNITY", "COMMUNITY"] or str(loc_node.get("level")) in ["1", "2"]:
                loc_val = clean_text(loc_node.get("name"))

    if isinstance(loc_obj, dict):
        city_val = city_val or clean_text(loc_obj.get("city"))
        loc_val = clean_text(loc_obj.get("name", loc_val))
        address_val = clean_text(loc_obj.get("fullLocation", loc_val))
    elif isinstance(loc_obj, str):
        loc_val = address_val = clean_text(loc_obj)

    if address_val and loc_val and address_val.lower() == loc_val.lower():
        address_val = None
    if loc_val and city_val and loc_val.lower() == city_val.lower():
        loc_val = None

    prop_type = clean_text(
        property_data.get("property_type") or
        property_data.get("propertyType") or
        property_data.get("type")
    )
    size_obj = property_data.get("size") or property_data.get("area") or {}
    area_val = 0.0
    try:
        area_val = float(size_obj.get("value", 0.0) if isinstance(size_obj, dict) else size_obj or 0.0)
    except Exception:
        pass

    def _parse_int(raw) -> int:
        if raw is None: return 0
        if isinstance(raw, str) and "studio" in raw.lower(): return 0
        try: return int(str(raw).strip())
        except ValueError:
            digits = re.findall(r'\d+', str(raw))
            return int(digits[0]) if digits else 0

    beds_val = _parse_int(property_data.get("bedrooms") or property_data.get("rooms"))
    baths_val = _parse_int(property_data.get("bathrooms") or property_data.get("baths"))

    agent_obj = property_data.get("agent") or property_data.get("broker") or {}
    broker_obj = property_data.get("broker") or property_data.get("agency") or {}
    phone_val, email_val = None, None
    for opt in (property_data.get("contact_options") or []):
        if isinstance(opt, dict):
            t = opt.get("type", "").lower()
            v = opt.get("value")
            if t == "phone" and v: phone_val = clean_text(v)
            elif t == "email" and v: email_val = clean_text(v)
            
    if not phone_val:
        phone_val = clean_text(agent_obj.get("phone") or agent_obj.get("mobile") or broker_obj.get("phone"))
    if not email_val:
        email_val = clean_text(agent_obj.get("email") or broker_obj.get("email"))

    # ── Agent image ───────────────────────────────────────────────────────
    agent_image_val = None
    raw_picture = agent_obj.get("picture") or agent_obj.get("photo") or agent_obj.get("image")
    if isinstance(raw_picture, str):
        agent_image_val = raw_picture
    elif isinstance(raw_picture, dict):
        agent_image_val = raw_picture.get("url") or raw_picture.get("large") or raw_picture.get("medium")

    return Listing(
        listing_id=listing_id,
        price=price_val,
        currency=currency_val,
        location=LocationData(city=city_val, area=loc_val, address=address_val),
        property_details=PropertyDetailsData(
            property_type=prop_type,
            bedrooms=beds_val,
            bathrooms=baths_val,
            area_sqft=area_val,
        ),
        agent=AgentData(
            name=clean_text(agent_obj.get("name")),
            agency=clean_text(broker_obj.get("name")),
            phone=phone_val,
            email=email_val,
            image=agent_image_val,
        ),
        url=url,
    )


class PropertyFinderScraper(BaseScraper):
    # Concurrency controls to prevent immediate DataDome IP blocks
    CONCURRENCY = 6 if os.environ.get("APIFY_IS_AT_HOME") else 8

    def __init__(self, proxy: str = None):
        super().__init__(proxy=proxy, concurrency_limit=self.CONCURRENCY)

    def _get_launch_args(self) -> List[str]:
        return [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-site-isolation-trials",
            "--disable-web-security",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-translate",
            "--metrics-recording-only",
            "--no-first-run",
            "--safebrowsing-disable-auto-update",
        ]

    async def _create_context(self, browser):
        opts = self.get_context_options()
        opts.update({
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "locale": "en-US",
            "timezone_id": "Asia/Dubai",
            "extra_http_headers": {
                "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            },
        })
        context = await browser.new_context(**opts)
        await context.add_init_script(STEALTH_INIT_SCRIPT)

        async def global_route(route: Route):
            req = route.request
            if req.resource_type in BLOCKED_TYPES:
                await route.abort()
                return
            if any(f in req.url for f in BLOCKED_URL_FRAGMENTS):
                await route.abort()
                return
            try: await route.continue_()
            except Exception: pass

        await context.route("**/*", global_route)
        return context

    async def _intercept_next_data(self, page: Page, url: str) -> Optional[dict]:
        result: Dict[str, Any] = {}

        async def handle_response(response):
            if result.get("done"): return
            if response.url == url or response.url.rstrip("/") == url.rstrip("/"):
                if "text/html" in response.headers.get("content-type", ""):
                    try:
                        body = await response.text()
                        
                        m_next = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', body, re.DOTALL)
                        if m_next:
                            result["data"] = json.loads(m_next.group(1))
                            result["done"] = True
                            return
                            
                        m_apollo = re.search(r'window\.__APOLLO_STATE__\s*=\s*(\{.*?\});', body, re.DOTALL)
                        if m_apollo:
                            result["data"] = json.loads(m_apollo.group(1))
                            result["done"] = True
                            return
                            
                        m_init = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});', body, re.DOTALL)
                        if m_init:
                            result["data"] = json.loads(m_init.group(1))
                            result["done"] = True
                            
                    except Exception:
                        pass

        page.on("response", handle_response)
        try:
            await page.goto(url, wait_until="commit", timeout=25000)
            for _ in range(15):
                if result.get("done"): break
                await asyncio.sleep(0.2)
        except Exception:
            pass
        finally:
            page.remove_listener("response", handle_response)

        return result.get("data")

    def _extract_property_data(self, data: Any, url: str) -> dict:
        """Intelligently navigates NextJS / Apollo State directly by ID"""
        if not data or not isinstance(data, dict): return {}

        # 1. Apollo Cache Direct Match (Extremely reliable for newer PF updates)
        match = re.search(r'-(\d{6,15})', url)
        if match:
            prop_key = f"Property:{match.group(1)}"
            if prop_key in data:
                return data[prop_key]
                
        # 2. Standard Next.js hierarchy
        page_props = data.get("props", {}).get("pageProps", {})
        if "propertyResult" in page_props: return page_props["propertyResult"].get("property", {})
        if "property" in page_props: return page_props.get("property", {})
        
        # 3. BFS Fallback
        found = _fuzzy_extract_property(data)
        return found if found else (data if isinstance(data, dict) else {})

    async def _get_listing_urls(self, context, paginated_url: str, page_num: int) -> List[str]:
        listing_hrefs: List[str] = []
        captured_xhr: List[dict] = []

        HREF_PATTERN = re.compile(
            r'/en/(?:rent|buy|plp|property|commercial|new-projects)'
            r'/[a-zA-Z0-9\-\/]+-\d{6,15}\.html'
        )

        def _extract_hrefs_from_text(text: str):
            for m in HREF_PATTERN.findall(text):
                full = "https://www.propertyfinder.ae" + m
                clean = full.split("?")[0].split("#")[0]
                if clean not in listing_hrefs:
                    listing_hrefs.append(clean)

        for attempt in range(1, MAX_PAGE_RETRIES + 1):
            search_page = await context.new_page()

            async def handle_response(response):
                try:
                    ct = response.headers.get("content-type", "")
                    rt = response.request.resource_type
                    if "text/html" in ct:
                        body = await response.text()
                        _extract_hrefs_from_text(body)
                    elif rt in ("xhr", "fetch"):
                        body = await response.text()
                        _extract_hrefs_from_text(body)
                        try: captured_xhr.append(json.loads(body))
                        except Exception: pass
                except Exception:
                    pass

            search_page.on("response", handle_response)
            
            if attempt == 1:
                logger.info(f"[PF] Scanning search page {page_num}: {paginated_url}")
            else:
                logger.warning(f"    [PF] Retry {attempt}/{MAX_PAGE_RETRIES} — search page {page_num}")

            try:
                await search_page.goto(paginated_url, wait_until="commit", timeout=25000)

                for _ in range(20):
                    if len(listing_hrefs) >= MIN_LISTINGS_THRESHOLD: break
                    await asyncio.sleep(0.2)

                try:
                    title = await search_page.title()
                    if any(x in title for x in ["Just a moment", "Access Denied", "DataDome"]):
                        logger.warning(f"    [PF] Bot challenge detected on page {page_num} — waiting 10s")
                        await asyncio.sleep(10)
                except Exception:
                    pass

                try:
                    prev_count = len(listing_hrefs)
                    for scroll_step in range(6):
                        await search_page.evaluate(f"window.scrollBy(0, {random.randint(600, 900)})")
                        await asyncio.sleep(random.uniform(0.4, 0.8))
                        if len(listing_hrefs) >= 20 and len(listing_hrefs) == prev_count: break
                        prev_count = len(listing_hrefs)
                except Exception:
                    pass

                if len(listing_hrefs) < MIN_LISTINGS_THRESHOLD:
                    await asyncio.sleep(3.0)

                try:
                    js_links = await search_page.evaluate("""() => {
                        return Array.from(document.querySelectorAll('a[href]'))
                            .map(a => a.href)
                            .filter(h => /\\/en\\/(rent|buy|plp|property|commercial|new-projects)\\/.*-\\d{6,15}\\.html/.test(h));
                    }""")
                    for href in js_links:
                        clean = href.split("?")[0].split("#")[0]
                        if clean not in listing_hrefs: listing_hrefs.append(clean)
                except Exception:
                    pass

                if not listing_hrefs:
                    for entry in captured_xhr:
                        try: _extract_hrefs_from_text(json.dumps(entry))
                        except Exception: pass

            finally:
                search_page.remove_listener("response", handle_response)
                await search_page.close()

            if len(listing_hrefs) >= MIN_LISTINGS_THRESHOLD:
                logger.info(f"[PF] Found {len(listing_hrefs)} listings on page {page_num}")
                return listing_hrefs

            if attempt < MAX_PAGE_RETRIES:
                backoff = random.uniform(3.0, 6.0) * attempt
                logger.warning(f"    [PF] Only {len(listing_hrefs)} listings found — retrying in {backoff:.1f}s")
                await asyncio.sleep(backoff)
            else:
                logger.warning(f"[PF] Accepting {len(listing_hrefs)} listings on page {page_num} after {MAX_PAGE_RETRIES} attempts")
                return listing_hrefs

        return []

    async def scrape_detail_page(self, context, semaphore: asyncio.Semaphore, url: str) -> Optional[Listing]:
        async with semaphore:
            # Jitter start to prevent immediate simultaneous firewall block
            await asyncio.sleep(random.uniform(0.5, 3.0))

            match = re.search(r'-(\d{6,15})\.html', url)
            prop_id = match.group(1) if match else url.split('/')[-1].replace('.html', '')

            for attempt in range(1, MAX_DETAIL_RETRIES + 1):
                if attempt == 1:
                    logger.info(f"    [PF] Fetching listing {prop_id}")
                else:
                    logger.warning(f"    [PF] Retry {attempt}/{MAX_DETAIL_RETRIES} — listing {prop_id}")

                page = await context.new_page()
                try:
                    data = await self._intercept_next_data(page, url)

                    if not data:
                        try:
                            raw = await page.locator("#__NEXT_DATA__").text_content(timeout=3000)
                            if raw: data = json.loads(raw)
                        except Exception: pass

                    if not data:
                        for var in ["window.__APOLLO_STATE__", "window.__INITIAL_STATE__"]:
                            try:
                                data = await page.evaluate(f"() => {var}")
                                if data: break
                            except Exception: pass

                    property_data = self._extract_property_data(data, url)
                    listing = _parse_listing(property_data, url)

                    if listing:
                        # Click the "Call" button to reveal the agent's masked
                        # name/phone/email, then merge into the AgentData
                        # already attached to this Listing.
                        revealed = await self._reveal_agent_contact(page, prop_id)
                        if revealed:
                            listing.agent.name = revealed.get("name") or listing.agent.name
                            listing.agent.phone = revealed.get("phone") or listing.agent.phone
                            listing.agent.email = revealed.get("email") or listing.agent.email

                        logger.info(f"    [PF] Parsed listing {prop_id}")
                        return listing
                        
                except Exception as e:
                    logger.error(f"    [PF] Error parsing listing {prop_id}: {e}")
                finally:
                    try: await page.close()
                    except Exception: pass

                if attempt < MAX_DETAIL_RETRIES:
                    await asyncio.sleep(random.uniform(3.0, 7.0) * attempt)
            
            logger.warning(f"    [PF] Skipped listing {prop_id} — no usable data after {MAX_DETAIL_RETRIES} attempts")
            return None

    async def _reveal_agent_contact(self, page: Page, prop_id: str) -> Optional[dict]:
        """
        Clicks the listing's 'Call' button to reveal the agent's masked
        phone number, then scrapes whatever name/phone/email becomes
        visible in the DOM. Returns None (non-fatal) if the button can't
        be found or clicked -- the caller falls back to the agent data
        already pulled from the page's initial JSON state in that case.
        """
        # Single fast JS poll across every known "Call" button shape instead of
        # trying 8 selectors one-by-one with a separate multi-second wait each
        # (that serial approach could burn 30s+ before even reaching the click).
        FIND_BUTTON_JS = r"""() => {
            const testIds = ['call-button', 'phone-cta', 'agent-call-button'];
            for (const id of testIds) {
                const el = document.querySelector(`[data-testid="${id}"]`);
                if (el && el.offsetParent !== null) return true;
            }
            const tel = document.querySelector('a[href^="tel:"]');
            if (tel && tel.offsetParent !== null) return true;
            for (const el of document.querySelectorAll('button, a')) {
                if (el.offsetParent === null) continue;
                const label = (el.getAttribute('aria-label') || '').toLowerCase();
                const text = (el.textContent || '').trim().toLowerCase();
                if (label.includes('call') || text === 'call' || text.includes('call')) return true;
            }
            return false;
        }"""

        try:
            await page.wait_for_function(FIND_BUTTON_JS, timeout=5000, polling=200)
        except Exception:
            logger.warning(f"    [PF] No call button found for listing {prop_id}")
            return None

        CLICK_BUTTON_JS = r"""() => {
            const testIds = ['call-button', 'phone-cta', 'agent-call-button'];
            let el = null;
            for (const id of testIds) {
                const c = document.querySelector(`[data-testid="${id}"]`);
                if (c && c.offsetParent !== null) { el = c; break; }
            }
            if (!el) {
                const tel = document.querySelector('a[href^="tel:"]');
                if (tel && tel.offsetParent !== null) el = tel;
            }
            if (!el) {
                for (const c of document.querySelectorAll('button, a')) {
                    if (c.offsetParent === null) continue;
                    const label = (c.getAttribute('aria-label') || '').toLowerCase();
                    const text = (c.textContent || '').trim().toLowerCase();
                    if (label.includes('call') || text === 'call' || text.includes('call')) { el = c; break; }
                }
            }
            if (!el) return false;
            el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
            el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
            return true;
        }"""

        try:
            clicked = await page.evaluate(CLICK_BUTTON_JS)
        except Exception as e:
            logger.warning(f"    [PF] Could not click call button for listing {prop_id}: {e}")
            return None

        if not clicked:
            logger.warning(f"    [PF] Could not click call button for listing {prop_id}")
            return None

        # After clicking, the "You are calling" modal appears.
        # Strategy: wait for a modal-like container, then run a single JS
        # evaluate() that:
        #   (a) finds the tightest modal container via CSS selector strings
        #       (no element handles that can go stale), and
        #   (b) extracts phone + name from within that container.
        # We never pass element handles to evaluate() — that's what was
        # causing silent failures when the handle went stale mid-animation.

        # Single fast poll for any modal-like container instead of a fixed
        # 800ms sleep plus 5 sequential selector waits (up to ~26s worst case).
        MODAL_READY_JS = r"""() => {
            const sels = ['[data-testid="call-dialog"]', '[data-testid="phone-reveal-modal"]', '[role="dialog"]'];
            for (const s of sels) {
                const el = document.querySelector(s);
                if (el && el.offsetParent !== null) return true;
            }
            for (const el of document.querySelectorAll('[class*="modal" i], [class*="dialog" i]')) {
                if (el.offsetParent !== null) return true;
            }
            return false;
        }"""
        try:
            await page.wait_for_function(MODAL_READY_JS, timeout=4000, polling=150)
        except Exception:
            # No modal detected within the window — fall through anyway, the
            # extraction below also checks page-wide tel:/mailto: links as a
            # fallback, so this isn't fatal.
            pass

        # Single JS call — finds modal by CSS, extracts phone + name, no handles.
        NON_NAME_PATTERNS = re.compile(
            r'^(amenities|features|specifications|details|speaks|reference|'
            r'you are calling|overview|description|highlights|price|location|'
            r'property|bedroom|bathroom|area|contact|agent|broker|agency)$',
            re.IGNORECASE,
        )

        result = await page.evaluate(r"""() => {
            // ── 1. Find the tightest visible modal container ──────────────────
            const MODAL_SELECTORS = [
                '[data-testid="call-dialog"]',
                '[data-testid="phone-reveal-modal"]',
                '[role="dialog"]'
            ];
            // Also try class-based, but pick the *smallest* matching element
            // that is visible (avoids grabbing a full-page overlay backdrop).
            let modal = null;
            for (const sel of MODAL_SELECTORS) {
                const el = document.querySelector(sel);
                if (el && el.offsetParent !== null) { modal = el; break; }
            }
            if (!modal) {
                // fallback: find smallest visible element whose class contains
                // "modal" or "dialog" — use area heuristic
                const byClass = Array.from(
                    document.querySelectorAll('[class*="modal"],[class*="dialog"],[class*="Modal"],[class*="Dialog"]')
                ).filter(el => {
                    if (el.offsetParent === null) return false;
                    const r = el.getBoundingClientRect();
                    // Must be meaningfully sized but not the full viewport
                    return r.width > 100 && r.height > 100
                        && r.width < window.innerWidth * 0.95
                        && r.height < window.innerHeight * 0.95;
                });
                // pick smallest by area
                byClass.sort((a, b) => {
                    const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
                    return (ra.width * ra.height) - (rb.width * rb.height);
                });
                modal = byClass[0] || null;
            }
            const root = modal || document;

            // ── 2. Extract phone ──────────────────────────────────────────────
            // The phone appears as the innerText of a <button> or <a> element.
            // We check own text only (via childNodes) to avoid grabbing
            // concatenated child text from a wrapper div.
            const phoneRe = /^\+?\d[\d\s\-]{7,17}$/;
            let phone = null;

            // 2a. Prefer tel: href (most reliable when present)
            for (const a of root.querySelectorAll('a[href^="tel:"]')) {
                const num = (a.getAttribute('href') || '').replace('tel:', '').trim();
                if (num) { phone = num; break; }
            }

            // 2b. Button/link whose *direct* text (not children) is a phone
            if (!phone) {
                for (const el of root.querySelectorAll('button, a, span')) {
                    // Get only the direct text nodes, not nested element text
                    let directText = '';
                    for (const node of el.childNodes) {
                        if (node.nodeType === Node.TEXT_NODE) {
                            directText += node.textContent;
                        }
                    }
                    directText = directText.trim();
                    if (phoneRe.test(directText)) { phone = directText; break; }
                }
            }

            // 2c. Any element whose complete text content is a phone number
            if (!phone) {
                for (const el of root.querySelectorAll('button, a, span, p, div')) {
                    const t = (el.textContent || '').trim();
                    if (phoneRe.test(t) && t.length <= 20) { phone = t; break; }
                }
            }

            // ── 3. Extract agent name ─────────────────────────────────────────
            let name = null;
            const nameRe = /^[A-Z\u00C0-\u017E][a-zA-Z\u00C0-\u017E'`-]+(\s[A-Z\u00C0-\u017E][a-zA-Z\u00C0-\u017E'`-]+){0,4}$/;
            const skipRe = /^(amenities|features|specifications|details|speaks|reference|you are calling|overview|description|highlights|price|location|property|bedroom|bathroom|area|contact|agent|broker|agency|verified|superagent|note)$/i;

            // Priority 1: explicit data-testid or agent-name class
            const explicit = root.querySelector(
                '[data-testid="agent-name"],[data-testid*="agentName"],[class*="agent-name"],[class*="agentName"],[class*="agent__name"],[class*="name__agent"]'
            );
            if (explicit) {
                name = explicit.textContent.trim();
            }

            // Priority 2: first title-cased short text node in a <p>/<strong>/<span>
            if (!name) {
                for (const el of root.querySelectorAll('p, strong, span, h2, h3, h4')) {
                    const t = (el.textContent || '').trim();
                    if (nameRe.test(t) && !skipRe.test(t) && t.length >= 3 && t.length <= 60) {
                        name = t; break;
                    }
                }
            }

            return { phone, name };
        }""")

        phone_val = None
        name_val = None

        if isinstance(result, dict):
            raw_phone = result.get("phone")
            if raw_phone:
                phone_val = re.sub(r'[\s\-]', '', clean_text(raw_phone) or "")
                if not re.search(r'\d{7,}', phone_val):
                    phone_val = None
            raw_name = result.get("name")
            if raw_name:
                candidate_name = clean_text(raw_name)
                if candidate_name and not NON_NAME_PATTERNS.match(candidate_name):
                    name_val = candidate_name

        # Fallback: tel: href anywhere on the page (catches masked pre-modal links)
        if not phone_val:
            try:
                for loc in await page.locator('a[href^="tel:"]').all():
                    href = await loc.get_attribute("href")
                    if href:
                        candidate = re.sub(r'[\s\-]', '', href.replace("tel:", "").strip())
                        if re.search(r'\d{7,}', candidate):
                            phone_val = candidate
                            break
            except Exception:
                pass

        email_val = None

        # Email — modals rarely show email; check full page as fallback
        try:
            email_link = page.locator('a[href^="mailto:"]').first
            if await email_link.count() > 0:
                href = await email_link.get_attribute("href")
                if href:
                    email_val = clean_text(href.replace("mailto:", "").split("?")[0])
        except Exception:
            pass

        if not (phone_val or email_val or name_val):
            logger.warning(f"    [PF] Call button clicked but no contact info revealed for listing {prop_id}")
            return None

        revealed_parts = []
        if phone_val: revealed_parts.append(f"phone={phone_val}")
        if name_val: revealed_parts.append(f"name={name_val}")
        if email_val: revealed_parts.append(f"email={email_val}")
        logger.info(f"    [PF] Revealed agent contact for listing {prop_id}: {', '.join(revealed_parts)}")
        return {"name": name_val, "phone": phone_val, "email": email_val}

    async def scrape_stream(
        self,
        url: str,
        start_page: int,
        end_page: int,
        max_results: Optional[int] = None,
    ) -> AsyncGenerator[Listing, None]:
        """
        Stream listings one-by-one.

        Args:
            url:         Single property page URL  OR  a search/listing URL.
            start_page:  First pagination page to visit (search mode only).
            end_page:    Last pagination page to visit (search mode only).
            max_results: Cap on total yielded listings.  Pass ``None`` to
                         yield everything found (used for single-listing mode).
        """
        seen_ids: set = set()
        seen_urls: set = set()
        yielded_count: int = 0
        is_cloud = bool(os.environ.get("APIFY_IS_AT_HOME")) or bool(os.environ.get("GITHUB_ACTIONS"))

        # Detect URL type: single property page vs. search/listing page.
        # Single-listing URLs contain a numeric ID suffix before .html
        # e.g. /en/buy/apartment-for-sale/downtown-dubai/...-12345678.html
        is_single_listing = bool(re.search(
            r'/en/(?:rent|buy|plp|property|commercial|new-projects)'
            r'/[a-zA-Z0-9\-\/]+-\d{6,15}\.html',
            url,
        ))

        def _limit_reached() -> bool:
            """True when the caller-supplied cap has been hit."""
            return max_results is not None and yielded_count >= max_results

        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(
                    headless=is_cloud,
                    channel="chrome",
                    args=self._get_launch_args(),
                    ignore_default_args=["--enable-automation"],
                )
            except Exception:
                browser = await p.chromium.launch(
                    headless=is_cloud,
                    args=self._get_launch_args(),
                    ignore_default_args=["--enable-automation"],
                )

            context = await self._create_context(browser)
            semaphore = asyncio.Semaphore(self.CONCURRENCY)

            try:
                if is_single_listing:
                    # ── Single property page: ignore max_results entirely ──────
                    res = await self.scrape_detail_page(context, semaphore, url)
                    if res and res.listing_id not in seen_ids:
                        seen_ids.add(res.listing_id)
                        yield res
                else:
                    # ── Search / multi-listing page ───────────────────────────
                    for page_num in range(start_page, end_page + 1):
                        if _limit_reached():
                            logger.info(
                                f"[PF] maxResults cap ({max_results}) reached — "
                                "stopping pagination."
                            )
                            break

                        parsed = urlparse(url)
                        query = re.sub(r'page=\d+', '', parsed.query).strip('&')
                        new_query = f"{query}&page={page_num}" if query else f"page={page_num}"
                        paginated_url = urlunparse((
                            parsed.scheme, parsed.netloc, parsed.path,
                            parsed.params, new_query, parsed.fragment
                        ))

                        listing_hrefs = await self._get_listing_urls(context, paginated_url, page_num)

                        new_hrefs = [h for h in listing_hrefs if h not in seen_urls]
                        seen_urls.update(new_hrefs)

                        # Trim hrefs list to avoid launching unnecessary tasks.
                        if max_results is not None:
                            remaining = max_results - yielded_count
                            new_hrefs = new_hrefs[:remaining]

                        tasks = [
                            asyncio.create_task(
                                self.scrape_detail_page(context, semaphore, href)
                            )
                            for href in new_hrefs
                        ]

                        for completed_task in asyncio.as_completed(tasks):
                            try:
                                r = await completed_task
                                if isinstance(r, Listing) and r.listing_id not in seen_ids:
                                    seen_ids.add(r.listing_id)
                                    yielded_count += 1
                                    yield r
                                    if _limit_reached():
                                        logger.info(
                                            f"[PF] maxResults cap ({max_results}) reached "
                                            "mid-page — stopping."
                                        )
                                        break
                            except Exception as e:
                                logger.error(f"Task exception during extraction: {e}")

                        logger.info(
                            f"[PF] Page {page_num} complete. "
                            f"Total unique streamed so far: {len(seen_ids)}"
                        )

                        if _limit_reached():
                            break

                        if page_num < end_page:
                            await asyncio.sleep(random.uniform(2.0, 4.0))

            finally:
                await context.close()
                await browser.close()