#!/usr/bin/env python3
"""
Fitbit Air India launch watcher.

Two-stage Telegram alerts:
  Stage 1 -> Fitbit Air looks live / announced for India (news or a listing) + link
  Stage 2 -> The listing is actually buyable (in stock / Add to Cart) + direct link

Backbone signal = Google News RSS (free, no API key, not blocked).
Amazon.in / Flipkart checks are best-effort bonuses (often blocked from CI IPs).
State is persisted in state.json so you only get each alert once.
"""

import os
import re
import json
import html
import time
from pathlib import Path

import requests
import feedparser

# ---------------- Config ----------------
STATE_FILE = Path("state.json")

# Reliable, keyless signal. Add/edit queries freely.
NEWS_QUERIES = [
    '"Fitbit Air" India',
    '"Fitbit Air" India price buy',
    '"Fitbit Air" Amazon India',
]

# Best-effort retailer search pages (bonus; may be blocked from GitHub runners)
AMAZON_SEARCH = "https://www.amazon.in/s?k=fitbit+air"
FLIPKART_SEARCH = "https://www.flipkart.com/search?q=fitbit+air"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept-Language": "en-IN,en;q=0.9",
}

# Strong phrases that mean it's ACTUALLY available now (not just announced).
# Kept strict on purpose: the India launch is already announced with an expected
# price, so weak words like "launch"/"price"/"expected" would fire too early.
STRONG_AVAIL = (
    "now available", "available now", "goes on sale", "now on sale",
    "on sale in india", "available to buy", "now selling", "you can buy",
    "now in india", "available in india", "buy on amazon", "buy on flipkart",
    "listed on amazon", "listed on flipkart", "up for grabs", "sale starts",
)

# Words on a product page that signal buyable vs not
BUY_HINTS = ("add to cart", "buy now", "add to bag")
UNAVAIL_HINTS = ("currently unavailable", "out of stock",
                 "temporarily out of stock")

TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")


# ---------------- State ----------------
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"stage": 0, "product_url": "", "notified": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------- Notify ----------------
# Uses ntfy.sh if NTFY_TOPIC is set (simplest, no account). Otherwise uses
# Telegram if both TELEGRAM_* are set. Set whichever ONE you prefer as a secret.
def notify(title, body, url="", priority="high"):
    # ---- ntfy.sh path (recommended for beginners) ----
    ntfy = os.environ.get("NTFY_TOPIC", "").strip()
    if ntfy:
        safe_title = title.encode("ascii", "ignore").decode().strip() or "Alert"
        data = (body + (("\n" + url) if url else "")).encode("utf-8")
        headers = {"Title": safe_title, "Priority": priority, "Tags": "rocket"}
        if url:
            headers["Click"] = url
        try:
            r = requests.post(f"https://ntfy.sh/{ntfy}", data=data,
                              headers=headers, timeout=20)
            print("[notify] ntfy status", r.status_code)
        except Exception as e:
            print("[notify] ntfy error", e)
        return

    # ---- Telegram path ----
    if TG_TOKEN and TG_CHAT:
        text = f"*{title}*\n{body}" + (f"\n{url}" if url else "")
        api = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        try:
            r = requests.post(api, data={
                "chat_id": TG_CHAT, "text": text,
                "parse_mode": "Markdown", "disable_web_page_preview": "false",
            }, timeout=20)
            print("[notify] telegram status", r.status_code)
        except Exception as e:
            print("[notify] telegram error", e)
        return

    print("[notify] (no notifier configured) ->", title, body, url)


def alert(title, body, url="", times=5, gap=4):
    """Insistent alert: a burst of max-priority pushes, ~gap seconds apart,
    so it keeps buzzing and is hard to miss/sleep through."""
    for i in range(times):
        notify(title, body, url, priority="urgent")
        if i < times - 1:
            time.sleep(gap)


# ---------------- Detectors ----------------
def fetch(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code == 200 and "captcha" not in r.text.lower():
            return r.text
        print("[fetch] blocked/non-200", url, r.status_code)
    except Exception as e:
        print("[fetch] error", url, e)
    return ""


def check_news():
    """Return [(title, link)] of items that look like India availability."""
    hits = []
    for q in NEWS_QUERIES:
        rss = ("https://news.google.com/rss/search?q="
               + requests.utils.quote(q) + "&hl=en-IN&gl=IN&ceid=IN:en")
        try:
            r = requests.get(rss, headers=HEADERS, timeout=25)
            feed = feedparser.parse(r.content)
        except Exception as e:
            print("[news] error", e)
            continue
        for entry in feed.entries[:15]:
            text = (entry.get("title", "") + " " + entry.get("summary", "")).lower()
            if "fitbit air" not in text:
                continue
            if "india" in text and any(h in text for h in STRONG_AVAIL):
                hits.append((entry.get("title", "").strip(),
                             entry.get("link", "").strip()))
    return hits


def find_amazon_product():
    page = fetch(AMAZON_SEARCH)
    if not page or "fitbit air" not in page.lower():
        return ""
    m = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', page)
    return f"https://www.amazon.in/dp/{m.group(1)}" if m else ""


def find_flipkart_product():
    page = fetch(FLIPKART_SEARCH)
    if not page or "fitbit air" not in page.lower():
        return ""
    m = re.search(r'href="(/[^"]*fitbit-air[^"]*/p/[^"]+)"', page, re.I)
    return "https://www.flipkart.com" + html.unescape(m.group(1)) if m else ""


def is_buyable(url):
    """True=buyable, False=unavailable, None=couldn't tell (blocked)."""
    if not url:
        return None
    page = fetch(url)
    if not page:
        return None
    low = page.lower()
    if any(u in low for u in UNAVAIL_HINTS):
        return False
    if any(b in low for b in BUY_HINTS):
        return True
    return False


# ---------------- Main ----------------
def main():
    # Manual test ping (run the workflow with the "test" box checked)
    if os.environ.get("TEST", "").lower() in ("true", "1"):
        notify("✅ Test", "Your Fitbit Air watcher is wired up correctly.")
        return

    state = load_state()
    stage = state.get("stage", 0)
    product_url = state.get("product_url", "")
    notified = set(state.get("notified", []))

    # Re-buzz: if a recent alert hasn't been "used up", re-send it on this run too
    # (so even if you slept through the first burst, it nags you again ~every 15 min).
    ra = state.get("realert")
    if ra and ra.get("left", 0) > 0:
        alert(ra["title"], ra["body"], ra.get("url", ""))
        ra["left"] -= 1
        state["realert"] = ra

    # Try to discover a real product URL if we don't have one yet
    if not product_url:
        product_url = find_amazon_product() or find_flipkart_product()
        if product_url:
            state["product_url"] = product_url

    # Stage 1: first India availability signal
    if stage < 1:
        news = check_news()
        new_news = [(t, l) for (t, l) in news if l not in notified]
        link = product_url or (new_news[0][1] if new_news else "")
        if link:
            title = "🚀 Fitbit Air — India"
            body = ("Looks like the Fitbit Air just went live / was announced "
                    "for India. Earliest link I found:")
            alert(title, body, link)
            state["realert"] = {"title": title, "body": body,
                                "url": link, "left": 3}
            for _, l in new_news:
                notified.add(l)
            state["stage"] = stage = 1

    # Stage 2: listing is actually buyable
    if 1 <= stage < 2 and product_url:
        if is_buyable(product_url) is True:
            title = "🟢 Fitbit Air — BUYABLE NOW (India)"
            body = "In stock / Add to Cart is live. Go:"
            alert(title, body, product_url)
            state["realert"] = {"title": title, "body": body,
                                "url": product_url, "left": 3}
            state["stage"] = stage = 2

    state["notified"] = list(notified)
    save_state(state)
    print("[done] stage =", state["stage"], "url =", state.get("product_url"))


if __name__ == "__main__":
    main()
