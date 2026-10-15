"""Automated News Monitoring & Email Notification System.

Collects energy-industry news from Google News and publisher RSS feeds, filters and ranks
articles by relevance, and emails a formatted HTML digest.

Usage:
    export GMAIL_USER=you@gmail.com GMAIL_APP_PASSWORD=xxxx NEWS_RECIPIENTS=a@x.com,b@y.com
    python news_monitor.py
Set SEND_EMAIL=false to only build the preview in output/.
"""

import re, html, time, hashlib, smtplib, os
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import quote_plus, urlparse

import feedparser
import pandas as pd

# ============================================================================
# Configuration
# ============================================================================

# --- Sources -----------------------------------------------------------------
# Google News search queries (each becomes its own RSS feed). Edit freely.
SEARCH_QUERIES = [
    "Petronas",
    "Malaysia energy policy",
    "Malaysia oil and gas regulation",
    "LNG market Asia",
    "OPEC+ production",
    "South China Sea oil gas",
    "energy transition Malaysia",
    "crude oil prices",
]
GOOGLE_NEWS_REGION = "hl=en-MY&gl=MY&ceid=MY:en"

# Direct publisher RSS feeds (name -> url)
DIRECT_FEEDS = {
    "The Star Business": "https://www.thestar.com.my/rss/Business/Business-News",
    "Malay Mail Money":  "https://www.malaymail.com/feed/rss/money",
    "OilPrice.com":      "https://oilprice.com/rss/main",
    "Rigzone":           "https://www.rigzone.com/news/rss/rigzone_latest.aspx",
}

# --- Relevance -----------------------------------------------------------------
# Category -> keywords. An article's score = number of keyword hits (title counts double).
CATEGORIES = {
    "Regulatory & Policy": ["regulation", "regulatory", "policy", "tariff", "subsidy", "tax", "budget",
                            "ministry", "government", "law", "bill", "licence", "license", "petroleum act"],
    "Geopolitical":        ["sanction", "conflict", "war", "south china sea", "tension", "dispute",
                            "election", "embargo", "middle east", "russia", "iran", "china"],
    "Market & Prices":     ["crude", "brent", "wti", "price", "opec", "lng", "gas", "supply", "demand",
                            "output", "production", "barrel"],
    "Corporate":           ["petronas", "shell", "exxon", "chevron", "totalenergies", "contract",
                            "acquisition", "merger", "earnings", "profit", "investment", "project"],
    "Energy Transition":   ["renewable", "solar", "hydrogen", "carbon", "ccs", "emission", "net zero",
                            "climate", "transition", "ev"],
}
MIN_SCORE      = 1        # drop articles with fewer keyword hits
LOOKBACK_HOURS = 24       # only keep articles published in this window
MAX_ARTICLES   = 25       # cap for the email
FETCH_SUMMARIES = False   # True = download each article for a longer summary (slower)

# --- Email -------------------------------------------------------------------
RECIPIENTS     = [a.strip() for a in os.environ.get("NEWS_RECIPIENTS", "you@example.com").split(",") if a.strip()]
EMAIL_SUBJECT  = "Energy News Digest"
SEND_EMAIL     = os.environ.get("SEND_EMAIL", "true").lower() == "true"  # "false" = preview only

# --- History (avoids resending the same story on later runs) -----------------
USE_HISTORY    = True                         # keep a CSV of sent stories so they are not repeated
HISTORY_PATH   = "data/sent_history.csv"

# ============================================================================
# Collect
# ============================================================================

def google_news_url(query):
    return f"https://news.google.com/rss/search?q={quote_plus(query)}&{GOOGLE_NEWS_REGION}"

def parse_time(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None

def strip_html(text):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()

def fetch_feed(url, source_label):
    feed = feedparser.parse(url, agent="Mozilla/5.0 (news-monitor)")
    rows = []
    for e in feed.entries:
        title = strip_html(e.get("title", ""))
        source = "Google News" if "news.google.com" in url else source_label
        # Google News titles look like "Headline - Publisher"
        if "news.google.com" in url and " - " in title:
            title, source = title.rsplit(" - ", 1)
            source = e.get("source", {}).get("title", source)
        rows.append({
            "title": title,
            "link": e.get("link", ""),
            "source": source,
            "published": parse_time(e),
            "summary": strip_html(e.get("summary", ""))[:400],
            "feed": source_label,
        })
    return rows

def collect_all():
    rows, log = [], []
    jobs = [(google_news_url(q), f"Google News: {q}") for q in SEARCH_QUERIES]
    jobs += [(url, name) for name, url in DIRECT_FEEDS.items()]
    for url, label in jobs:
        try:
            got = fetch_feed(url, label)
            rows += got
            log.append((label, len(got), "ok"))
        except Exception as ex:
            log.append((label, 0, f"error: {ex}"))
        time.sleep(0.5)  # be polite to servers
    print(pd.DataFrame(log, columns=["source", "articles", "status"]).to_string(index=False))
    return pd.DataFrame(rows)

# ============================================================================
# Clean, filter and rank
# ============================================================================

def normalise_title(t):
    t = re.sub(r"\s[-|–]\s[^-|–]{2,40}$", "", t)   # drop trailing " - Publisher"
    return re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()[:90]

def score_article(title, summary):
    t, s = title.lower(), summary.lower()
    cats, score = [], 0
    for cat, words in CATEGORIES.items():
        hits = sum(2 * len(re.findall(rf"\b{re.escape(w)}\b", t)) +
                   len(re.findall(rf"\b{re.escape(w)}\b", s)) for w in words)
        if hits:
            cats.append((cat, hits))
            score += hits
    cats.sort(key=lambda x: -x[1])
    return score, (cats[0][0] if cats else "Other"), ", ".join(c for c, _ in cats)

def load_history():
    if USE_HISTORY and os.path.exists(HISTORY_PATH):
        return set(pd.read_csv(HISTORY_PATH)["key"])
    return set()

def process(df):
    if df.empty:
        return df
    df = df.copy()
    df["key"] = df["title"].map(normalise_title).map(lambda s: hashlib.md5(s.encode()).hexdigest())
    df = df.drop_duplicates("key")                                   # same story from several feeds
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    df = df[df["published"].isna() | (df["published"] >= cutoff)]    # recent only
    df = df[~df["key"].isin(load_history())]                         # not sent before
    scored = df.apply(lambda r: score_article(r["title"], r["summary"]), axis=1, result_type="expand")
    df[["score", "category", "tags"]] = scored
    df = df[df["score"] >= MIN_SCORE]
    return df.sort_values(["score", "published"], ascending=False).head(MAX_ARTICLES).reset_index(drop=True)

# ============================================================================
# Optional enrichment
# ============================================================================

def enrich(df):
    import trafilatura
    out = []
    for link, summary in zip(df["link"], df["summary"]):
        try:
            text = trafilatura.extract(trafilatura.fetch_url(link)) or ""
            sents = re.split(r"(?<=[.!?])\s+", text)
            out.append(" ".join(sents[:2])[:400] or summary)
        except Exception:
            out.append(summary)
    df = df.copy(); df["summary"] = out
    return df

# ============================================================================
# Email
# ============================================================================

COLOURS = {"Regulatory & Policy": "#1f6feb", "Geopolitical": "#cf222e", "Market & Prices": "#bf8700",
           "Corporate": "#00a19c", "Energy Transition": "#1a7f37", "Other": "#6e7781"}

def fmt_time(ts):
    if ts is None or pd.isna(ts):
        return ""
    return ts.astimezone(timezone(timedelta(hours=8))).strftime("%d %b, %H:%M MYT")

def build_email_html(df):
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%A, %d %B %Y, %H:%M MYT")
    counts = df["category"].value_counts() if len(df) else pd.Series(dtype=int)
    chips = " ".join(
        f'<span style="background:{COLOURS.get(c,"#6e7781")};color:#fff;padding:3px 9px;border-radius:12px;'
        f'font-size:12px;margin-right:4px">{html.escape(c)}: {n}</span>' for c, n in counts.items())
    sections = ""
    for cat in counts.index:
        items = ""
        for _, r in df[df["category"] == cat].iterrows():
            summary = f'<div style="color:#444;font-size:13px;margin-top:3px">{html.escape(r["summary"])}</div>' \
                      if r["summary"] and r["summary"].lower() not in r["title"].lower() else ""
            items += f'''
            <tr><td style="padding:10px 0;border-bottom:1px solid #eee">
              <a href="{html.escape(r["link"])}" style="color:#0b3d91;font-weight:600;font-size:15px;text-decoration:none">{html.escape(r["title"])}</a>
              <div style="color:#888;font-size:12px;margin-top:2px">{html.escape(str(r["source"]))} · {fmt_time(r["published"])}</div>
              {summary}
            </td></tr>'''
        colour = COLOURS.get(cat, "#6e7781")
        sections += f'''
        <h3 style="color:{colour};border-left:4px solid {colour};padding-left:8px;margin:24px 0 4px">{html.escape(cat)}</h3>
        <table width="100%" cellpadding="0" cellspacing="0">{items}</table>'''
    if not len(df):
        sections = '<p style="color:#666">No new relevant articles in this period.</p>'
    return f'''<html><head><meta charset="utf-8"></head><body style="margin:0;background:#f4f6f8;font-family:Arial,Helvetica,sans-serif">
    <table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:20px">
    <table width="680" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:8px;overflow:hidden">
      <tr><td style="background:#00a19c;color:#fff;padding:20px 24px">
        <div style="font-size:22px;font-weight:bold">{html.escape(EMAIL_SUBJECT)}</div>
        <div style="font-size:13px;opacity:.9;margin-top:4px">{now} · {len(df)} articles · last {LOOKBACK_HOURS}h</div>
      </td></tr>
      <tr><td style="padding:16px 24px 0">{chips}</td></tr>
      <tr><td style="padding:0 24px 24px">{sections}</td></tr>
      <tr><td style="background:#f0f0f0;color:#888;font-size:11px;padding:12px 24px">
        Automated digest generated in Python from {len(SEARCH_QUERIES) + len(DIRECT_FEEDS)} news sources.
      </td></tr>
    </table></td></tr></table></body></html>'''

def build_email_text(df):
    lines = [f"{EMAIL_SUBJECT} — {len(df)} articles", ""]
    for cat in df["category"].unique() if len(df) else []:
        lines.append(f"== {cat} ==")
        for _, r in df[df["category"] == cat].iterrows():
            lines.append(f"- {r['title']} ({r['source']})\n  {r['link']}")
        lines.append("")
    return "\n".join(lines)

def get_secret(name):
    try:
        from google.colab import userdata
        return userdata.get(name)
    except Exception:
        return os.environ.get(name)

def send_email(df, html_body):
    user, pwd = get_secret("GMAIL_USER"), get_secret("GMAIL_APP_PASSWORD")
    if not user or not pwd:
        raise RuntimeError("Add GMAIL_USER and GMAIL_APP_PASSWORD to Colab Secrets first.")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"{EMAIL_SUBJECT} | {datetime.now(timezone(timedelta(hours=8))):%d %b %Y} | {len(df)} articles"
    msg["From"], msg["To"] = user, ", ".join(RECIPIENTS)
    msg.attach(MIMEText(build_email_text(df), "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(user, pwd)
        server.sendmail(user, RECIPIENTS, msg.as_string())
    print(f"Sent to {len(RECIPIENTS)} recipient(s).")

def save_history(df):
    if not (USE_HISTORY and len(df)):
        return
    os.makedirs(os.path.dirname(HISTORY_PATH) or ".", exist_ok=True)
    new = df[["key", "title", "link"]].assign(sent_at=datetime.now(timezone.utc).isoformat())
    if os.path.exists(HISTORY_PATH):
        new = pd.concat([pd.read_csv(HISTORY_PATH), new]).drop_duplicates("key")
    new.to_csv(HISTORY_PATH, index=False)

# ============================================================================
# Run
# ============================================================================

def main():
    raw = collect_all()
    print(f"Collected {len(raw)} articles")
    articles = process(raw)
    print(f"{len(articles)} relevant articles after filtering")
    if FETCH_SUMMARIES and len(articles):
        articles = enrich(articles)

    os.makedirs("output", exist_ok=True)
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M")
    email_html = build_email_html(articles)
    with open(f"output/digest_{stamp}.html", "w", encoding="utf-8") as f:
        f.write(email_html)
    articles.drop(columns=["key"], errors="ignore").to_csv(f"output/news_{stamp}.csv", index=False)

    if SEND_EMAIL and len(articles):
        send_email(articles, email_html)
        save_history(articles)
    else:
        print("Email not sent (SEND_EMAIL is false or no articles). Preview saved in output/.")


if __name__ == "__main__":
    main()
