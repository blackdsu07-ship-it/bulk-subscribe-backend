# flask_app.py
# ================================================================
# PythonAnywhere Flask backend — Universal Bulk Form Subscriber
# Works with any HTML newsletter/signup form via HTTP (no browser)
#
# SETUP in PythonAnywhere Bash console:
#   pip3.10 install flask flask-cors requests beautifulsoup4 lxml --user
#
# WEB TAB → open your WSGI file, replace contents with:
#   import sys
#   sys.path.insert(0, '/home/YOURUSERNAME/mysite')
#   from flask_app import app as application
#
# Then click Reload in the Web tab.
# ================================================================

import re
import logging
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Shared session with a real browser User-Agent to avoid bot blocks
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
})

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

# Keywords used to identify the right input fields
FIELD_HINTS = {
    "email":      ["email", "e-mail", "mail"],
    "first_name": ["first", "fname", "firstname", "given", "forename"],
    "last_name":  ["last",  "lname", "lastname",  "surname", "family"],
    "phone":      ["phone", "mobile", "tel", "cell", "contact"],
    "name":       ["name"],   # catch-all full-name field
}


# ── /ping ─────────────────────────────────────────────────────
@app.route("/ping")
def ping():
    return jsonify({"status": "ok"})


# ── /detect ───────────────────────────────────────────────────
# Preview what fields were found on a page before committing.
@app.route("/detect", methods=["POST"])
def detect():
    body = request.get_json(force=True)
    url  = body.get("url", "").strip()
    if not url:
        return jsonify({"ok": False, "message": "No URL provided"}), 400
    try:
        soup, final_url = _fetch_page(url)
        form, fields    = _find_form(soup, final_url)
        if not form:
            return jsonify({"ok": False, "message": "No suitable form found on this page."})
        readable = {k: {"name": v["name"], "type": v.get("type","text")}
                    for k, v in fields.items()}
        return jsonify({
            "ok":     True,
            "action": form.get("action", final_url),
            "method": form.get("method", "post").upper(),
            "fields": readable,
            "message": f"Found form with {len(fields)} matching field(s).",
        })
    except Exception as e:
        log.error(f"detect error for {url}: {e}")
        return jsonify({"ok": False, "message": str(e)}), 500


# ── /bulk-subscribe ───────────────────────────────────────────
@app.route("/bulk-subscribe", methods=["POST"])
def bulk_subscribe():
    """
    Body:
    {
      "sites": ["https://site1.com/subscribe", "https://site2.com"],
      "subscribers": [
        {"email": "a@b.com", "first_name": "John", "last_name": "Doe", "phone": "6379756588"}
      ]
    }
    """
    body        = request.get_json(force=True)
    sites       = body.get("sites", [])
    subscribers = body.get("subscribers", [])

    if not sites:
        return jsonify({"error": "No sites provided"}), 400
    if not subscribers:
        return jsonify({"error": "No subscribers provided"}), 400

    # Pre-fetch and parse each site's form once, then reuse for all emails
    site_forms = {}
    for url in sites:
        try:
            soup, final_url   = _fetch_page(url)
            form, fields      = _find_form(soup, final_url)
            site_forms[url]   = {"form": form, "fields": fields, "base": final_url, "error": None}
            if not form:
                site_forms[url]["error"] = "No suitable form found"
                log.warning(f"No form at {url}")
        except Exception as e:
            site_forms[url] = {"form": None, "fields": {}, "base": url, "error": str(e)}
            log.error(f"Fetch error {url}: {e}")

    results = []
    for url, ctx in site_forms.items():
        label = _short_label(url)
        if ctx["error"] or not ctx["form"]:
            # Mark all emails as skipped for this site
            for sub in subscribers:
                results.append({
                    "email":  sub["email"],
                    "site":   label,
                    "status": "skipped",
                    "reason": ctx["error"] or "no form",
                })
            continue

        for sub in subscribers:
            email = sub.get("email", "").strip()
            if not email or not EMAIL_RE.match(email):
                continue
            try:
                status, reason = _submit_form(ctx, sub)
                results.append({"email": email, "site": label, "status": status, "reason": reason})
                log.info(f"[{status.upper()}] {email} -> {label}")
            except Exception as e:
                log.error(f"Submit error {email} -> {label}: {e}")
                results.append({"email": email, "site": label, "status": "error", "reason": str(e)})

    ok_count = sum(1 for r in results if r["status"] == "success")
    return jsonify({
        "message": f"Done — {ok_count}/{len(results)} succeeded.",
        "results": results,
    })


# ════════════════════════════════════════════════════════════════
#  HELPERS
# ════════════════════════════════════════════════════════════════

def _normalize_url(url):
    """Ensure URL has a scheme."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _fetch_page(url):
    """Fetch a page, follow redirects, return (BeautifulSoup, final_url)."""
    url = _normalize_url(url)
    r = SESSION.get(url, timeout=15, allow_redirects=True)

    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    return soup, r.url


def _field_matches(el, *hint_lists):
    """Check if an <input> element matches any of the provided hint word lists."""
    attrs = " ".join(filter(None, [
        el.get("name", ""),
        el.get("id", ""),
        el.get("placeholder", ""),
        el.get("aria-label", ""),
        el.get("autocomplete", ""),
        el.get("class", "") if isinstance(el.get("class"), str) else " ".join(el.get("class", [])),
    ])).lower()

    for hints in hint_lists:
        if any(h in attrs for h in hints):
            return True
    return False


def _score_form(form):
    """Score a form: higher = more likely to be a signup form."""
    score = 0
    text  = form.get_text(" ").lower()
    html  = str(form).lower()

    # Has an email input → strong signal
    for inp in form.find_all("input"):
        if inp.get("type") == "email" or "email" in inp.get("name", "").lower():
            score += 10
            break

    # Keywords in form HTML/text
    for kw in ["subscribe", "newsletter", "sign up", "signup", "join", "email"]:
        if kw in text or kw in html:
            score += 2

    # Has a visible submit button
    if form.find(["button", "input"], {"type": ["submit", "button"]}):
        score += 3

    # Penalise login/search/contact forms
    for kw in ["login", "password", "search", "comment", "checkout"]:
        if kw in text or kw in html:
            score -= 8

    return score


def _find_form(soup, base_url):
    """
    Find the best signup form on the page.
    Returns (form_element, detected_fields_dict).
    """
    forms = soup.find_all("form")
    if not forms:
        return None, {}

    best_form  = max(forms, key=_score_form)
    best_score = _score_form(best_form)

    if best_score < 5:
        return None, {}

    fields = _detect_fields(best_form)
    return best_form, fields


def _detect_fields(form):
    """
    Map our logical field names to actual <input> elements inside the form.
    Returns dict: { "email": el, "first_name": el, ... }
    """
    inputs  = form.find_all("input", {"type": lambda t: t not in ("hidden", "submit", "button", "checkbox", "radio", "file", None) or t is None})
    # include inputs with no type (defaults to text)
    inputs  = [i for i in form.find_all("input") if i.get("type","text") not in ("submit","button","checkbox","radio","file","image","reset")]

    found = {}

    for inp in inputs:
        itype = inp.get("type", "text").lower()

        # Email field
        if "email" not in found and (itype == "email" or _field_matches(inp, FIELD_HINTS["email"])):
            found["email"] = inp
            continue

        # First name
        if "first_name" not in found and _field_matches(inp, FIELD_HINTS["first_name"]):
            found["first_name"] = inp
            continue

        # Last name
        if "last_name" not in found and _field_matches(inp, FIELD_HINTS["last_name"]):
            found["last_name"] = inp
            continue

        # Phone
        if "phone" not in found and (itype == "tel" or _field_matches(inp, FIELD_HINTS["phone"])):
            found["phone"] = inp
            continue

        # Generic "name" field (full name, used when first/last not separate)
        if "name" not in found and _field_matches(inp, FIELD_HINTS["name"]):
            found["name"] = inp

    return found


def _build_payload(form, fields, subscriber):
    """
    Build the POST payload:
    1. Start with all hidden inputs (CSRF tokens, list IDs, etc.)
    2. Fill in detected fields with subscriber data
    3. Include the submit button value if present
    """
    payload = {}

    # Hidden fields first (essential for CSRF, Mailchimp u/id, etc.)
    for hidden in form.find_all("input", {"type": "hidden"}):
        name = hidden.get("name")
        if name:
            payload[name] = hidden.get("value", "")

    # Our detected fields
    field_map = {
        "email":      subscriber.get("email", ""),
        "first_name": subscriber.get("first_name", ""),
        "last_name":  subscriber.get("last_name", ""),
        "phone":      subscriber.get("phone", ""),
        "name":       f"{subscriber.get('first_name','')} {subscriber.get('last_name','')}".strip(),
    }
    for key, el in fields.items():
        name = el.get("name")
        if name and field_map.get(key):
            payload[name] = field_map[key]

    # Submit button (some servers check for it)
    submit = form.find("input", {"type": "submit"}) or form.find("button", {"type": "submit"})
    if submit and submit.get("name"):
        payload[submit["name"]] = submit.get("value", "Submit")

    return payload


def _submit_form(ctx, subscriber):
    """Submit the form for one subscriber. Returns (status, reason)."""
    form    = ctx["form"]
    fields  = ctx["fields"]
    base    = ctx["base"]

    if "email" not in fields:
        return "skipped", "email field not detected"

    action  = form.get("action", base)
    action  = urljoin(base, action)
    method  = form.get("method", "post").strip().lower()
    payload = _build_payload(form, fields, subscriber)

    headers = {
        "Referer":      base,
        "Origin":       f"{urlparse(base).scheme}://{urlparse(base).netloc}",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    if method == "get":
        r = SESSION.get(action,  params=payload, headers=headers, timeout=15, allow_redirects=True)
    else:
        r = SESSION.post(action, data=payload,   headers=headers, timeout=15, allow_redirects=True)

    # Interpret the response
    return _interpret_response(r, subscriber["email"])


def _interpret_response(r, email):
    """Heuristically decide if the submission succeeded."""
    text = r.text.lower()

    # Hard error codes
    if r.status_code >= 500:
        return "error", f"server error {r.status_code}"

    # Success signals in body text
    success_phrases = [
        "thank you", "thanks", "subscribed", "confirmed",
        "check your email", "almost there", "you're in",
        "welcome", "success", "signup complete", "signed up",
    ]
    for phrase in success_phrases:
        if phrase in text:
            return "success", phrase

    # Already subscribed → still a success from our perspective
    already_phrases = ["already subscribed", "already on our list", "already registered"]
    for phrase in already_phrases:
        if phrase in text:
            return "success", "already subscribed"

    # Error signals
    error_phrases = ["invalid email", "error", "failed", "please try again", "something went wrong"]
    for phrase in error_phrases:
        if phrase in text:
            return "rejected", phrase

    # 200 with redirect to a different page is usually success
    if r.status_code == 200 and r.url != r.request.url:
        return "success", "redirected after submit"

    if r.status_code in (200, 201, 302):
        return "success", f"HTTP {r.status_code}"

    return "error", f"unexpected HTTP {r.status_code}"


def _short_label(url):
    parsed = urlparse(url)
    return parsed.netloc or url


# ── local dev ──────────────────────────────────────────────────
if __name__ == "__main__":
    print("Running locally at http://localhost:5000")
    app.run(debug=True, port=5000)
