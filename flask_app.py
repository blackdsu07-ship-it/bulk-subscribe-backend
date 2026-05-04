# flask_app.py - Hybrid: fast requests first, Playwright fallback
import re
import logging
import os
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

FIELD_HINTS = {
    "email":      ["email", "e-mail", "mail"],
    "first_name": ["first", "fname", "firstname", "given", "forename"],
    "last_name":  ["last",  "lname", "lastname",  "surname", "family"],
    "phone":      ["phone", "mobile", "tel", "cell", "contact"],
    "name":       ["fullname", "full_name", "full-name", "yourname"],
}

SUCCESS_PHRASES = [
    "thank you", "thanks", "subscribed", "confirmed", "check your email",
    "almost there", "you're in", "welcome", "success", "signup complete",
    "signed up", "you have been", "added to", "on the list",
]
ALREADY_PHRASES = ["already subscribed", "already on our list", "already registered"]
ERROR_PHRASES   = ["invalid email", "please enter a valid", "something went wrong"]


def _normalize_url(url):
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _short_label(url):
    return urlparse(url).netloc or url


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
        soup, final_url = _fetch_page(url)
        form, fields    = _find_best_form(soup, final_url)
        if not form:
            return jsonify({"ok": False, "message": "No suitable form found on this page."})
        readable = {k: {"name": v.get("name",""), "type": v.get("type","text")} for k, v in fields.items()}
        return jsonify({"ok": True, "fields": readable, "message": f"Found {len(fields)} field(s)."})
    except Exception as e:
        log.error(f"detect error: {e}")
        return jsonify({"ok": False, "message": str(e)}), 500


# ── /bulk-subscribe ───────────────────────────────────────────
@app.route("/bulk-subscribe", methods=["POST"])
def bulk_subscribe():
    body        = request.get_json(force=True)
    sites       = [_normalize_url(s) for s in body.get("sites", [])]
    subscribers = body.get("subscribers", [])

    if not sites:       return jsonify({"error": "No sites provided"}), 400
    if not subscribers: return jsonify({"error": "No subscribers provided"}), 400

    results = []

    for url in sites:
        label = _short_label(url)
        log.info(f"Processing: {url}")

        # Try fast method first
        try:
            soup, final_url = _fetch_page(url)
            form, fields    = _find_best_form(soup, final_url)
            use_playwright  = not form or "email" not in fields
        except Exception as e:
            log.warning(f"Fast fetch failed for {url}: {e}")
            use_playwright = True
            form = None
            fields = {}
            final_url = url

        for sub in subscribers:
            email = sub.get("email", "").strip()
            if not email or not EMAIL_RE.match(email):
                continue

            if use_playwright:
                status, reason = _try_playwright(url, sub)
            else:
                try:
                    status, reason = _submit_form(form, fields, final_url, sub)
                    # If fast method looks wrong, retry with Playwright
                    if status == "skipped":
                        status, reason = _try_playwright(url, sub)
                except Exception as e:
                    log.error(f"Submit error: {e}")
                    status, reason = _try_playwright(url, sub)

            log.info(f"[{status.upper()}] {email} -> {label}: {reason}")
            results.append({"email": email, "site": label, "status": status, "reason": reason})

    ok_count = sum(1 for r in results if r["status"] == "success")
    return jsonify({"message": f"Done — {ok_count}/{len(results)} succeeded.", "results": results})


# ════════════════ FAST PATH (requests + BS4) ═════════════════

def _fetch_page(url):
    r = SESSION.get(url, timeout=20, allow_redirects=True)
    r.raise_for_status()
    return BeautifulSoup(r.text, "lxml"), r.url


def _score_form(form):
    score = 0
    text  = form.get_text(" ").lower()
    html  = str(form).lower()
    for inp in form.find_all("input"):
        if inp.get("type") == "email" or "email" in inp.get("name","").lower():
            score += 10; break
    for kw in ["subscribe","newsletter","sign up","signup","join","email"]:
        if kw in text or kw in html: score += 2
    if form.find(["button","input"], {"type": ["submit","button"]}): score += 3
    for kw in ["login","password","search","comment","checkout"]:
        if kw in text or kw in html: score -= 8
    return score


def _find_best_form(soup, base_url):
    forms = soup.find_all("form")
    if not forms: return None, {}
    best = max(forms, key=_score_form)
    if _score_form(best) < 5: return None, {}
    return best, _map_fields(best)


def _map_fields(form):
    inputs = [i for i in form.find_all("input")
              if i.get("type","text") not in ("submit","button","checkbox","radio","file","image","reset")]
    found = {}
    for inp in inputs:
        itype = inp.get("type","text").lower()
        attrs = " ".join(filter(None,[
            inp.get("name",""), inp.get("id",""), inp.get("placeholder",""),
            inp.get("aria-label",""), inp.get("autocomplete",""),
            " ".join(inp.get("class",[]) if isinstance(inp.get("class"), list) else [inp.get("class","")]),
        ])).lower()
        if "email" not in found and (itype=="email" or any(h in attrs for h in FIELD_HINTS["email"])):
            found["email"] = inp; continue
        if "first_name" not in found and any(h in attrs for h in FIELD_HINTS["first_name"]):
            found["first_name"] = inp; continue
        if "last_name" not in found and any(h in attrs for h in FIELD_HINTS["last_name"]):
            found["last_name"] = inp; continue
        if "phone" not in found and (itype=="tel" or any(h in attrs for h in FIELD_HINTS["phone"])):
            found["phone"] = inp; continue
        if "name" not in found and any(h in attrs for h in FIELD_HINTS["name"]):
            found["name"] = inp
    return found


def _submit_form(form, fields, base, subscriber):
    if "email" not in fields:
        return "skipped", "email field not detected"
    payload = {}
    for hidden in form.find_all("input", {"type":"hidden"}):
        if hidden.get("name"): payload[hidden["name"]] = hidden.get("value","")
    field_map = {
        "email":      subscriber.get("email",""),
        "first_name": subscriber.get("first_name",""),
        "last_name":  subscriber.get("last_name",""),
        "phone":      subscriber.get("phone",""),
        "name":       f"{subscriber.get('first_name','')} {subscriber.get('last_name','')}".strip(),
    }
    for key, el in fields.items():
        name = el.get("name")
        if name and field_map.get(key): payload[name] = field_map[key]
    submit = form.find("input",{"type":"submit"}) or form.find("button",{"type":"submit"})
    if submit and submit.get("name"): payload[submit["name"]] = submit.get("value","Submit")

    action = urljoin(base, form.get("action", base))
    method = form.get("method","post").strip().lower()
    headers = {
        "Referer": base,
        "Origin": f"{urlparse(base).scheme}://{urlparse(base).netloc}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    if method == "get":
        r = SESSION.get(action, params=payload, headers=headers, timeout=20, allow_redirects=True)
    else:
        r = SESSION.post(action, data=payload, headers=headers, timeout=20, allow_redirects=True)
    return _interpret_response(r)


def _interpret_response(r):
    if r.status_code >= 500: return "error", f"server error {r.status_code}"
    text = r.text.lower()
    for p in ALREADY_PHRASES:
        if p in text: return "success", "already subscribed"
    for p in SUCCESS_PHRASES:
        if p in text: return "success", p
    for p in ERROR_PHRASES:
        if p in text: return "rejected", p
    if r.status_code in (200,201,302): return "success", f"HTTP {r.status_code}"
    return "error", f"unexpected HTTP {r.status_code}"


# ════════════════ PLAYWRIGHT FALLBACK ════════════════════════

def _try_playwright(url, subscriber):
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        return "error", "Playwright not installed"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=[
                "--no-sandbox", "--disable-setuid-sandbox",
                "--disable-dev-shm-usage", "--disable-gpu", "--single-process",
            ])
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
            )
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)

            # Find email field
            email_sel = None
            for sel in ['input[type="email"]','input[name*="email" i]',
                        'input[id*="email" i]','input[placeholder*="email" i]']:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    email_sel = sel; break

            if not email_sel:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight/2)")
                page.wait_for_timeout(1000)
                for sel in ['input[type="email"]','input[name*="email" i]',
                            'input[placeholder*="email" i]']:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        email_sel = sel; break

            if not email_sel:
                browser.close()
                return "skipped", "email field not found"

            page.fill(email_sel, subscriber.get("email",""))

            # Fill other fields
            for sel in ['input[name*="first" i]','input[id*="first" i]']:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    page.fill(sel, subscriber.get("first_name","")); break
            for sel in ['input[name*="last" i]','input[id*="last" i]']:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    page.fill(sel, subscriber.get("last_name","")); break
            for sel in ['input[type="tel"]','input[name*="phone" i]']:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    page.fill(sel, subscriber.get("phone","")); break

            # Submit
            btn = None
            for sel in ['button[type="submit"]','input[type="submit"]',
                        'button:has-text("Subscribe")','button:has-text("Sign Up")',
                        'button:has-text("Join")','button:has-text("Submit")']:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        btn = el; break
                except: continue

            if not btn:
                browser.close()
                return "skipped", "submit button not found"

            url_before = page.url
            btn.click()
            page.wait_for_timeout(3000)

            body_text = page.inner_text("body").lower()
            browser.close()

            for phrase in ALREADY_PHRASES:
                if phrase in body_text: return "success", "already subscribed"
            for phrase in SUCCESS_PHRASES:
                if phrase in body_text: return "success", phrase
            for phrase in ERROR_PHRASES:
                if phrase in body_text: return "rejected", phrase
            if page.url != url_before: return "success", "redirected after submit"
            return "success", "submitted"

    except Exception as e:
        log.error(f"Playwright error: {e}")
        return "error", str(e)[:120]


if __name__ == "__main__":
    app.run(debug=True, port=5000)
