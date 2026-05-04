# flask_app.py
# ================================================================
# Railway Flask backend — Universal Bulk Form Subscriber
# Uses Playwright (headless browser) to handle JavaScript-rendered
# forms that plain requests/BeautifulSoup cannot see.
#
# SETUP on Railway:
#   requirements.txt should include:
#     flask, flask-cors, playwright, gunicorn
#
#   Add this to your railway.toml or Dockerfile build command:
#     playwright install chromium --with-deps
# ================================================================

import re
import logging
from urllib.parse import urljoin, urlparse

from flask import Flask, request, jsonify
from flask_cors import CORS
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

# Keywords to identify form fields
FIELD_HINTS = {
    "email":      ["email", "e-mail", "mail"],
    "first_name": ["first", "fname", "firstname", "given", "forename"],
    "last_name":  ["last",  "lname", "lastname",  "surname", "family"],
    "phone":      ["phone", "mobile", "tel", "cell", "contact"],
    "name":       ["fullname", "full_name", "full-name", "yourname", "your_name"],
}

SUCCESS_PHRASES = [
    "thank you", "thanks", "subscribed", "confirmed", "check your email",
    "almost there", "you're in", "welcome", "success", "signup complete",
    "signed up", "you have been", "added to", "on the list",
]
ALREADY_PHRASES = ["already subscribed", "already on our list", "already registered"]
ERROR_PHRASES   = ["invalid email", "please enter a valid", "something went wrong",
                   "error", "failed", "please try again"]


def _normalize_url(url):
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


# ── /ping ─────────────────────────────────────────────────────
@app.route("/ping")
def ping():
    return jsonify({"status": "ok"})


# ── /detect ───────────────────────────────────────────────────
@app.route("/detect", methods=["POST"])
def detect():
    body = request.get_json(force=True)
    url  = _normalize_url(body.get("url", "").strip())
    if not url:
        return jsonify({"ok": False, "message": "No URL provided"}), 400
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page    = _open_page(browser, url)
            fields  = _detect_fields_pw(page)
            browser.close()

        if not fields:
            return jsonify({"ok": False, "message": "No suitable form found on this page."})

        readable = {k: v for k, v in fields.items()}
        return jsonify({
            "ok":      True,
            "fields":  readable,
            "message": f"Found form with {len(fields)} matching field(s).",
        })
    except Exception as e:
        log.error(f"detect error for {url}: {e}")
        return jsonify({"ok": False, "message": str(e)}), 500


# ── /bulk-subscribe ───────────────────────────────────────────
@app.route("/bulk-subscribe", methods=["POST"])
def bulk_subscribe():
    body        = request.get_json(force=True)
    sites       = [_normalize_url(s) for s in body.get("sites", [])]
    subscribers = body.get("subscribers", [])

    if not sites:
        return jsonify({"error": "No sites provided"}), 400
    if not subscribers:
        return jsonify({"error": "No subscribers provided"}), 400

    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for url in sites:
            label = _short_label(url)
            log.info(f"Processing site: {url}")

            for sub in subscribers:
                email = sub.get("email", "").strip()
                if not email or not EMAIL_RE.match(email):
                    continue
                try:
                    status, reason = _submit_with_playwright(browser, url, sub)
                    log.info(f"[{status.upper()}] {email} -> {label}: {reason}")
                    results.append({"email": email, "site": label, "status": status, "reason": reason})
                except Exception as e:
                    log.error(f"Error {email} -> {label}: {e}")
                    results.append({"email": email, "site": label, "status": "error", "reason": str(e)[:120]})

        browser.close()

    ok_count = sum(1 for r in results if r["status"] == "success")
    return jsonify({
        "message": f"Done — {ok_count}/{len(results)} succeeded.",
        "results": results,
    })


# ════════════════════════════════════════════════════════════════
#  PLAYWRIGHT HELPERS
# ════════════════════════════════════════════════════════════════

def _open_page(browser, url):
    """Open a page with realistic browser settings."""
    ctx = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
        locale="en-US",
    )
    page = ctx.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    # Wait a bit for JS-rendered forms to appear
    page.wait_for_timeout(2500)
    return page


def _detect_fields_pw(page):
    """Find email/name/phone inputs on the page using Playwright."""
    fields = {}

    # Try to find email input
    for sel in ['input[type="email"]', 'input[name*="email"]', 'input[id*="email"]',
                'input[placeholder*="email" i]', 'input[placeholder*="Email" i]']:
        el = page.query_selector(sel)
        if el and el.is_visible():
            fields["email"] = sel
            break

    # First name
    for sel in ['input[name*="first" i]', 'input[id*="first" i]',
                'input[placeholder*="first" i]', 'input[autocomplete="given-name"]']:
        el = page.query_selector(sel)
        if el and el.is_visible():
            fields["first_name"] = sel
            break

    # Last name
    for sel in ['input[name*="last" i]', 'input[id*="last" i]',
                'input[placeholder*="last" i]', 'input[autocomplete="family-name"]']:
        el = page.query_selector(sel)
        if el and el.is_visible():
            fields["last_name"] = sel
            break

    # Phone
    for sel in ['input[type="tel"]', 'input[name*="phone" i]', 'input[id*="phone" i]',
                'input[placeholder*="phone" i]']:
        el = page.query_selector(sel)
        if el and el.is_visible():
            fields["phone"] = sel
            break

    # Full name fallback
    if "first_name" not in fields:
        for sel in ['input[name*="name" i]', 'input[id*="name" i]',
                    'input[placeholder*="name" i]', 'input[autocomplete="name"]']:
            el = page.query_selector(sel)
            if el and el.is_visible():
                fields["name"] = sel
                break

    return fields


def _find_submit_button(page):
    """Find the most likely submit button for a newsletter form."""
    candidates = [
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Subscribe")',
        'button:has-text("Sign Up")',
        'button:has-text("Join")',
        'button:has-text("Submit")',
        'button:has-text("Get")',
        '[role="button"]:has-text("Subscribe")',
    ]
    for sel in candidates:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return el
        except Exception:
            continue
    return None


def _submit_with_playwright(browser, url, subscriber):
    """
    Open the page in a real headless browser, fill in the form,
    submit it, and interpret the result.
    """
    page = _open_page(browser, url)

    try:
        fields = _detect_fields_pw(page)

        if "email" not in fields:
            # Try scrolling to reveal lazy-loaded forms
            page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
            page.wait_for_timeout(1500)
            fields = _detect_fields_pw(page)

        if "email" not in fields:
            return "skipped", "email field not found on page"

        # Fill email
        page.fill(fields["email"], subscriber.get("email", ""))

        # Fill name fields if found
        if "first_name" in fields:
            page.fill(fields["first_name"], subscriber.get("first_name", ""))
        if "last_name" in fields:
            page.fill(fields["last_name"], subscriber.get("last_name", ""))
        if "name" in fields:
            full = f"{subscriber.get('first_name','')} {subscriber.get('last_name','')}".strip()
            page.fill(fields["name"], full)
        if "phone" in fields:
            page.fill(fields["phone"], subscriber.get("phone", ""))

        # Click submit
        btn = _find_submit_button(page)
        if not btn:
            return "skipped", "submit button not found"

        # Wait for navigation or response after click
        url_before = page.url
        try:
            with page.expect_response(lambda r: r.status < 400, timeout=10000):
                btn.click()
        except PlaywrightTimeout:
            btn.click()  # click anyway even if no response event

        page.wait_for_timeout(3000)

        # Check result
        body_text = page.inner_text("body").lower()
        url_after = page.url

        for phrase in ALREADY_PHRASES:
            if phrase in body_text:
                return "success", "already subscribed"

        for phrase in SUCCESS_PHRASES:
            if phrase in body_text:
                return "success", phrase

        for phrase in ERROR_PHRASES:
            if phrase in body_text:
                return "rejected", phrase

        if url_after != url_before:
            return "success", "redirected after submit"

        return "success", "submitted (no explicit confirmation)"

    finally:
        page.context.close()


def _short_label(url):
    parsed = urlparse(url)
    return parsed.netloc or url


# ── local dev ──────────────────────────────────────────────────
if __name__ == "__main__":
    print("Running locally at http://localhost:5000")
    app.run(debug=True, port=5000)
