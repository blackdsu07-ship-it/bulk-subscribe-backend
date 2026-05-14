import streamlit as st
import asyncio
import random
import pandas as pd
import os
from playwright.async_api import async_playwright

# =========================================
# CONFIG
# =========================================
MAX_CONCURRENT   = 4
PAGE_LOAD_WAIT   = 2500
SCROLL_STEPS     = 3
DETECT_ATTEMPTS  = 4
POPUP_WAIT       = 8
SUCCESS_WAIT     = 4
HUMAN_DELAY_MIN  = 200
HUMAN_DELAY_MAX  = 600

# =========================================
# HELPER: INSTALL PLAYWRIGHT (Streamlit Cloud workaround)
# =========================================
@st.cache_resource
def install_playwright():
    os.system("playwright install chromium")
    os.system("playwright install-deps chromium")

install_playwright()

# =========================================
# PARSE DOMAINS
# =========================================
def parse_domains(text):
    if not text:
        return []
    raw = text.replace(",", "\n").split("\n")
    domains = []
    for d in raw:
        d = d.strip()
        if not d:
            continue
        d = d.replace("https://", "").replace("http://", "").split("/")[0]
        if "." not in d:
            continue
        domains.append(d)
    return domains

# =========================================
# ASYNC PLAYWRIGHT FUNCTIONS
# =========================================
async def human_delay(a=HUMAN_DELAY_MIN, b=HUMAN_DELAY_MAX):
    await asyncio.sleep(random.uniform(a / 1000, b / 1000))

async def remove_overlays(page):
    try:
        await page.evaluate("""
            document.querySelectorAll(
                '.overlay,.modal-backdrop,.cookie-banner,' +
                '.cookie-consent,.gdpr,[id*="cookie" i],' +
                '[class*="cookie" i],[id*="gdpr" i],' +
                '[class*="sticky-bar" i],[class*="promo-bar" i],' +
                '[class*="bottom-bar" i],[class*="floating" i]'
            ).forEach(e => e.remove());
            document.body.style.overflow = 'auto';
        """)
    except:
        pass

async def scroll_to_bottom(page):
    try:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1.2)
        await page.evaluate("window.scrollBy(0, -300)")
        await asyncio.sleep(0.5)
    except:
        pass

async def fast_scroll(page):
    try:
        for _ in range(SCROLL_STEPS):
            await page.mouse.wheel(0, 2000)
            await asyncio.sleep(0.4)
        await page.mouse.move(10, 10)
        await asyncio.sleep(0.2)
        await page.mouse.move(400, 300)
    except:
        pass

async def trigger_popups(page):
    try:
        await page.evaluate("""
            const keywords = [
                'subscribe','sign up','signup','join','newsletter',
                'get updates','discount','deals','notify','free',
                'unlock','offer','exclusive','email'
            ];
            const tags = ['button','a','[role="button"]','input[type="submit"]'];
            tags.forEach(tag => {
                document.querySelectorAll(tag).forEach(el => {
                    const txt = (el.innerText || el.value || '').toLowerCase();
                    if (keywords.some(k => txt.includes(k))) {
                        try { el.click(); } catch(e) {}
                    }
                });
            });
        """)
        await asyncio.sleep(0.8)
    except:
        pass
    try:
        await page.mouse.wheel(0, 3000)
        await asyncio.sleep(0.5)
        await page.mouse.wheel(0, -3000)
    except:
        pass

async def wait_for_popup(page, timeout=POPUP_WAIT):
    combined = ", ".join([
        "input[type='email']", "input[name*='email' i]", "input[id*='email' i]",
        "input[placeholder*='email' i]", "[class*='popup' i] input",
        "[class*='modal' i] input", "[class*='klaviyo' i] input",
        "[id*='klaviyo' i] input", "[id*='newsletter' i] input",
        "[class*='newsletter' i] input", "[class*='subscribe' i] input",
        ".hs-email", "#email", "input[name='EMAIL']", "input[name='email_address']",
    ])
    try:
        await page.wait_for_selector(combined, timeout=timeout * 1000, state="visible")
        return True
    except:
        pass
    return False

async def find_email_input(page):
    selectors = [
        "input[type='email']", "input[name='EMAIL']", "input[name='email_address']",
        "input[name*='email' i]", "input[id='email']", "input[id*='email' i]",
        "input[placeholder*='email' i]", "input[autocomplete='email']",
        ".hs-email", "input[class*='email' i]",
        "form input[type='text']:not([name*='search' i]):not([placeholder*='search' i]):not([id*='search' i])",
    ]

    async def check(el):
        try:
            if not await el.is_visible(): return None
            box = await el.bounding_box()
            return el if box and box["width"] > 50 and box["height"] > 10 else None
        except:
            return None

    for sel in selectors:
        try:
            locs = page.locator(sel)
            for i in range(min(await locs.count(), 5)):
                el = await check(locs.nth(i))
                if el: return el
        except: pass

    await scroll_to_bottom(page)
    await asyncio.sleep(0.8)

    for sel in selectors:
        try:
            locs = page.locator(sel)
            for i in range(min(await locs.count(), 5)):
                el = await check(locs.nth(i))
                if el: return el
        except: pass

    try:
        for frame in page.frames:
            if frame == page.main_frame: continue
            for sel in selectors[:8]:
                try:
                    locs = frame.locator(sel)
                    for i in range(min(await locs.count(), 5)):
                        el = await check(locs.nth(i))
                        if el: return el
                except: pass
    except: pass

    try:
        shadow_handle = await page.evaluate_handle("""
            () => {
                for (const el of document.querySelectorAll('*')) {
                    if (el.shadowRoot) {
                        const inp = el.shadowRoot.querySelector(
                            'input[type=email],input[name*=email],input[placeholder*=email]'
                        );
                        if (inp && inp.offsetWidth > 50) return inp;
                    }
                }
                return null;
            }
        """)
        if shadow_handle:
            el = shadow_handle.as_element()
            if el: return el
    except: pass

    return None

async def click_submit(page, email_el=None):
    if email_el:
        try:
            done = await page.evaluate("""
                (el) => {
                    const form = el.closest('form');
                    if (!form) return false;
                    const btn = form.querySelector('button[type=submit],input[type=submit],button');
                    if (btn) { btn.click(); return true; }
                    return false;
                }
            """, email_el)
            if done: return True
        except: pass

    if email_el:
        try:
            done = await page.evaluate("""
                (el) => {
                    const form = el.closest('form');
                    if (form) { form.submit(); return true; }
                    return false;
                }
            """, email_el)
            if done: return True
        except: pass

    for sel in ["button[type='submit']", "input[type='submit']"]:
        try:
            btn = page.locator(sel).first
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click(force=True, timeout=3000)
                return True
        except: pass

    for k in ["sign up", "subscribe", "submit", "join", "continue", "go", "send"]:
        try:
            btn = page.locator(f"button:has-text('{k}'), [role='button']:has-text('{k}'), input[value*='{k}' i]").first
            if await btn.count() > 0 and await btn.is_visible():
                await btn.scroll_into_view_if_needed()
                await btn.click(force=True, timeout=3000)
                return True
        except: pass

    try:
        await page.keyboard.press("Enter")
        return True
    except:
        return False

async def check_success(page):
    success_texts = [
        "thank you", "thanks", "you're in", "you are in", "subscribed", "confirmed", 
        "check your email", "success", "welcome", "signed up", "added", "great",
        "almost there", "verify", "look out for", "got it", "received", "enrolled", 
        "registered", "inbox", "confirmation", "you've been", "stay tuned"
    ]
    url_signals = ["thank", "success", "confirm", "subscribed", "registered", "welcome", "done"]

    for _ in range(SUCCESS_WAIT):
        await asyncio.sleep(1)
        try:
            url = page.url.lower()
            if any(sig in url for sig in url_signals): return True
        except: pass

        try:
            combined = "|".join(success_texts)
            if await page.locator(f"text=/{combined}/i").count() > 0: return True
        except: pass

        try:
            title = (await page.title()).lower()
            if any(t in title for t in success_texts): return True
        except: pass

    return False

async def process_one(context, domain, email, semaphore, log_placeholder):
    async with semaphore:
        try:
            page = await context.new_page()
            try:
                await page.goto(f"https://{domain}", wait_until="domcontentloaded", timeout=45000)
            except:
                try:
                    await page.goto(f"http://{domain}", wait_until="domcontentloaded", timeout=45000)
                except:
                    await page.close()
                    return {"domain": domain, "status": "error: navigation failed"}

            await page.wait_for_timeout(PAGE_LOAD_WAIT)
            await remove_overlays(page)
            await scroll_to_bottom(page)
            await trigger_popups(page)
            await wait_for_popup(page, timeout=POPUP_WAIT)

            email_input = None
            for attempt in range(DETECT_ATTEMPTS):
                email_input = await find_email_input(page)
                if email_input: break
                await remove_overlays(page)
                await trigger_popups(page)
                await asyncio.sleep(1.5)

            if not email_input:
                await page.close()
                return {"domain": domain, "status": "no email field"}

            try:
                await email_input.scroll_into_view_if_needed()
                await asyncio.sleep(0.4)
            except: pass

            await human_delay()

            try:
                await email_input.click(force=True)
                await email_input.fill("")
                await email_input.fill(email)
            except:
                try:
                    await email_input.type(email, delay=50)
                except:
                    await page.close()
                    return {"domain": domain, "status": "error: could not fill email"}

            await human_delay()
            submitted = await click_submit(page, email_input)

            if submitted:
                await asyncio.sleep(1.5)
                success = await check_success(page)
                status = "subscribed ✓" if success else "submitted (unconfirmed)"
            else:
                status = "filled only — no submit"

            await page.close()
            return {"domain": domain, "status": status}

        except Exception as e:
            try: await page.close() 
            except: pass
            return {"domain": domain, "status": f"error: {str(e)[:100]}"}

async def process_domains(domains, email, log_placeholder):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled",
                "--disable-images", "--blink-settings=imagesEnabled=false", "--disable-extensions", "--disable-gpu",
            ]
        )

        context = await browser.new_context(
            viewport={"width": 1366, "height": 768},
            ignore_https_errors=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )

        await context.route("**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,mp4,mp3,avi}", lambda route: route.abort())
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); window.chrome = { runtime: {} };")

        semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        tasks = [process_one(context, domain, email, semaphore, log_placeholder) for domain in domains]
        results = await asyncio.gather(*tasks)
        await browser.close()

    return list(results)

# =========================================
# STREAMLIT UI
# =========================================
st.set_page_config(page_title="Bulk Subscriber", layout="wide")
st.title("Bulk Newsletter Subscriber")

email_input = st.text_input("Enter your email:")
domain_input = st.text_area("Domains:", placeholder="Paste domains here\nOne per line or comma separated", height=200)

if st.button("Run Fast", type="primary"):
    if not email_input or not domain_input:
        st.warning("Please provide both an email and at least one domain.")
    else:
        domains = parse_domains(domain_input)
        st.info(f"Loaded {len(domains)} domains. Processing up to {MAX_CONCURRENT} at a time...")
        
        log_placeholder = st.empty()
        with st.spinner("Processing domains... Please wait."):
            # Run the async loop
            results = asyncio.run(process_domains(domains, email_input, log_placeholder))
        
        df = pd.DataFrame(results)
        
        total = len(df)
        confirmed = len(df[df["status"] == "subscribed ✓"])
        unconf = len(df[df["status"] == "submitted (unconfirmed)"])
        failed = total - confirmed - unconf

        st.subheader("Results Summary")
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total", total)
        col2.metric("✓ Confirmed", confirmed)
        col3.metric("? Unconfirmed", unconf)
        col4.metric("✗ Failed", failed)
        
        st.dataframe(df, use_container_width=True)
