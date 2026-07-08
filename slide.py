# slide.py
"""
Slide — AI News Aggregator + Nova Chat backend.
Single-file FastAPI service. Deploy target: https://pulse-1-sq0g.onrender.com

──────────────────────────────────────────────────────────────────────────
ENDPOINT TABLE
──────────────────────────────────────────────────────────────────────────
Method  Path            What it does
------  --------------  ------------------------------------------------------------
GET     /               Service info + this endpoint table, as JSON.
GET     /health         Liveness check.
GET     /news           Kicks off the background ingestion job: pulls every RSS feed
                         in RSS_FEEDS, scrapes the full article, rewrites it with
                         Gemini into {headline, news, notification, categories}, and
                         inserts new rows into the Supabase `news` table. Returns 202
                         immediately (safe to call from a cron job).
POST    /get_news       Returns personalised, previously-unseen news for a user.
                         Body: {email, categories?, max_results?}
                         - Matches the user's saved `suggestions` (or an override).
                         - Excludes anything already in `history`.
                         - Falls back to any unseen item if too few category matches.
                         - Appends served links to `history`.
POST    /like           Adds a news `link` to the user's `likes` array.
                         Body: {email, link}
POST    /unlike         Removes a news `link` from the user's `likes` array.
                         Body: {email, link}
POST    /save            Adds a news `link` to the user's `saved` array.
                         Body: {email, link}
POST    /unsave          Removes a news `link` from the user's `saved` array.
                         Body: {email, link}
POST    /get_liked       Returns the full news rows the user has liked.
                         Body: {email}
POST    /get_saved       Returns the full news rows the user has saved.
                         Body: {email}
POST    /list_chats      Lists a user's Nova conversations (name + preview + count).
                         Body: {email}
POST    /get_chat        Returns the full message list for one conversation.
                         Body: {email, conversation_name}
POST    /getchat         Alias of /get_chat (same body/response).
                         Body: {email, conversation_name}
POST    /append_chat     Appends a message into a conversation's `chat_history`
                         entry. Matches the existing Nova frontend contract.
                         Body: {user_email, conversation_name, element_json_string}
POST    /rename_chat     Renames a conversation.
                         Body: {user_email, old_name, new_name}
POST    /delete_chat     Deletes a conversation.
                         Body: {user_email, conversation_name}
POST    /chat            Talks to Nova: grounds the reply in matching rows from the
                         `news` table, generates a response with Gemini, and stores
                         both the user's message and Nova's reply into `chat_history`
                         under the given (or auto-created) conversation name.
                         Body: {email, message, conversation_name?}
POST    /ws/chat         WebSocket endpoint for streaming Nova chat.
POST    /update_suggestions
                         Updates user's category suggestions based on three lists.
──────────────────────────────────────────────────────────────────────────
Supabase schema this file relies on:
  news  : headline, news, notification, categories, link, image, original, published_date
  users : email, likes(_text), saved(_text), suggestions(_text), history(_text),
          chat_history(_jsonb)
"""
import os
import re
import json
import time
import logging
import random
from datetime import datetime
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr

import requests
import feedparser
from bs4 import BeautifulSoup
from supabase import create_client
import google.generativeai as genai
from dotenv import load_dotenv

# ─────────────────────────── SETUP ──────────────────────────
load_dotenv()  # no-op on platforms (Render, etc.) that inject env vars directly

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("slide")

SUPABASE_URL   = os.getenv("SUPABASE_URL")
SUPABASE_KEY   = os.getenv("SUPABASE_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "8185149536")

if not SUPABASE_URL or not SUPABASE_KEY or not GEMINI_API_KEY:
    raise Exception("Missing environment variables: SUPABASE_URL / SUPABASE_KEY / GEMINI_API_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro")

# behavior constants
DEFAULT_MAX_RESULTS = 15         # how many items /get_news returns by default
MIN_UNSEEN_TARGET   = 5          # if fewer than this match category filter, broaden the search
NEWS_POOL_LIMIT      = 150       # how many recent rows to pull from Supabase to filter from
MAX_HISTORY_ITEMS    = 500       # cap on stored `history` (seen-links) length per user
CHAT_CONTEXT_ROWS    = 3         # how many matching news rows to ground a /chat reply in

# Fallback reply for Nova when generation fails
FALLBACK_REPLY = "Oops! My newsroom just had a paper jam — let’s try that again."

app = FastAPI(title="Slide - AI News Aggregator")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────── RSS FEEDS ──────────────────────
# Grouped by topic purely for readability — the ingestion loop treats them as one pool.
# Gemini decides the actual category tags per article via the prompt below.
RSS_FEEDS: List[str] = [
    # ── World / General ──
    "http://feeds.bbci.co.uk/news/rss.xml",
    "http://feeds.bbci.co.uk/news/world/rss.xml",
    "http://feeds.bbci.co.uk/news/uk/rss.xml",
    "https://timesofindia.indiatimes.com/rssfeedstopstories.cms",
    "https://timesofindia.indiatimes.com/rssfeeds/296589292.cms",       # TOI World
    "https://timesofindia.indiatimes.com/rssfeeds/-2128936835.cms",     # TOI India
    "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",

    # ── Politics ──
    "http://feeds.bbci.co.uk/news/politics/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",

    # ── Business & Finance ──
    "http://feeds.bbci.co.uk/news/business/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
    "https://timesofindia.indiatimes.com/rssfeeds/1898055.cms",         # TOI Business

    # ── Technology & Startups ──
    "http://feeds.bbci.co.uk/news/technology/rss.xml",
    "https://techcrunch.com/feed/",
    "https://www.theverge.com/rss/index.xml",
    "https://feeds.arstechnica.com/arstechnica/index",
    "https://timesofindia.indiatimes.com/rssfeeds/66949542.cms",        # TOI Tech
    "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
    "https://medium.com/feed/tag/technology",
    "https://medium.com/feed/tag/artificial-intelligence",

    # ── Space & Science ──
    "https://www.space.com/feeds/all",
    "https://www.nasa.gov/rss/dyn/breaking_news.rss",
    "https://rss.nytimes.com/services/xml/rss/nyt/Science.xml",
    "https://www.sciencedaily.com/rss/all.xml",

    # ── Health ──
    "https://rss.nytimes.com/services/xml/rss/nyt/Health.xml",
    "https://www.medicalnewstoday.com/rss",
    "https://www.who.int/rss-feeds/news-english.xml",

    # ── Sports ──
    "http://feeds.bbci.co.uk/sport/rss.xml",
    "https://www.espncricinfo.com/rss/content/story/feeds/6.xml",
    "https://timesofindia.indiatimes.com/rssfeeds/4719148.cms",         # TOI Sports

    # ── Entertainment ──
    "http://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml",
    "https://variety.com/feed/",
    "https://timesofindia.indiatimes.com/rssfeeds/1081479906.cms",      # TOI Entertainment
]

# Kept for backward compatibility with any code still referencing the old name.
RSS_URLS = RSS_FEEDS

# ─────────────────────────── PROMPT ─────────────────────────
# Prompt-engineered for Slide: the goal is copy that makes someone stop scrolling,
# while staying strictly faithful to the source article (no invented facts/quotes).
PROMPT = """
You are Nova, an award-winning digital news editor for "Slide" — a fast, mobile-first
news app read by busy, curious people who want to feel instantly informed and hooked
from the very first line.

Rewrite the article below into scroll-stopping, addictive-but-accurate news copy.

VOICE & STYLE RULES:
- Open the "news" field with a hook — a surprising fact, a stake, a tension, a "why
  this matters now" — never a flat "X announced Y today."
- Write like a sharp human journalist, not a press release: active voice, punchy and
  varied sentence lengths, no filler adjectives, no throat-clearing.
- Be 100% factually faithful to the source article. Never invent details, numbers,
  quotes, or outcomes that aren't in the article.
- 2–3 short, tight paragraphs. Every sentence has to earn its place.
- "headline": clear AND compelling — the kind of line that makes someone tap while
  scrolling — but never clickbait, never misleading.
- "notification": a push-notification teaser, under 80 characters, punchy enough to
  make someone open the app right now.
- "categories": sub-category first (lowercase), then the main category, from the list
  below. You may use multiple categories if genuinely relevant.

Output ONLY valid JSON. No markdown, no code fences, no commentary before or after.

Main categories:
general / global, sub categories - Breaking News, National News, World News, Politics, Government Policy, Elections, International Relations, Crime Reports, Cyber Crime
business & finance, sub categories - Stock Market, Banking & Loans, Cryptocurrency, Economy & Inflation, Corporate News, Investments & Funding
science & technology, sub categories - Technology News, Artificial Intelligence, Machine Learning, Robotics, Cybersecurity, Space & Astronomy, Space Missions, ISRO / NASA News, Gadgets & Reviews, Startup News, Tech Startups, AI Startups, Innovation & Research
sports, sub categories - Cricket, Football, Match Results, Player News, Tournaments, Sports Events
trending, sub categories - Viral News, Social Media Trends, Memes & Challenges, Internet Sensations, Public Buzz
entertainment, sub categories - Movies, Music, Celebrity News, OTT / Streaming, TV Shows
lifestyle & society, sub categories - Health & Wellness, Mental Health, Food & Nutrition, Travel, Fashion, Fitness

JSON FORMAT:
{
  "headline": "",
  "news": "",
  "notification": "",
  "categories": ""
}

Title: {title}
Article: {article}
"""

# ─────────────────────────── INGESTION HELPERS ──────────────
def article_text(url: str) -> str:
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        art = soup.find("article")
        if art:
            return art.get_text(" ", strip=True)
    except Exception as e:
        log.warning(f"article_text failed for {url}: {e}")
    return ""


def already_exists(link: str) -> bool:
    res = supabase.table("news").select("id").eq("link", link).execute()
    return bool(res.data)


def clean_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(text)


def run_news_fetch():
    """Fetch RSS -> scrape -> Gemini rewrite -> insert into Supabase. Runs in the background."""
    inserted, errors = [], []

    for rss_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(rss_url)
        except Exception as e:
            errors.append(f"feed parse failed for {rss_url}: {e}")
            continue

        for entry in feed.entries[:15]:
            try:
                if not hasattr(entry, "link"):
                    continue

                if already_exists(entry.link):
                    continue

                if not hasattr(entry, "published_parsed") or entry.published_parsed is None:
                    continue

                pub_date = datetime(*entry.published_parsed[:6])
                if (datetime.now() - pub_date).days > 3:
                    continue

                article = article_text(entry.link)
                if not article:
                    continue

                prompt   = PROMPT.format(title=entry.title, article=article)
                response = model.generate_content(prompt)
                ai_json  = clean_json(response.text)

                image = ""
                if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
                    image = entry.media_thumbnail[0].get("url", "")

                data = {
                    "headline":       ai_json["headline"],
                    "news":           ai_json["news"],
                    "notification":   ai_json["notification"],
                    "categories":     ai_json["categories"],
                    "link":           entry.link,
                    "image":          image,
                    "original":       entry.title,
                    "published_date": pub_date.isoformat(),
                }

                supabase.table("news").insert(data).execute()
                inserted.append(ai_json["headline"])

            except Exception as e:
                errors.append(str(e))

    log.info(f"News fetch done — inserted: {len(inserted)}, errors: {len(errors)}")
    if errors:
        log.warning(f"Errors (first 5): {errors[:5]}")


# ─────────────────────────── SERVING HELPERS (get_news) ─────
def get_user_row(email: str, select: str = "suggestions, history") -> Optional[Dict[str, Any]]:
    res = supabase.table("users").select(select).eq("email", email).execute()
    if not res.data:
        return None
    return res.data[0]


def normalize_categories(raw: Optional[List[str]]) -> List[str]:
    """Lowercase + dedupe a list of category strings."""
    if not raw:
        return []
    out = []
    for c in raw:
        tok = str(c).strip().lower()
        if tok and tok not in out:
            out.append(tok)
    return out


def matches_categories(row_categories: str, user_cats: List[str]) -> bool:
    if not user_cats:
        return True
    categories = (row_categories or "").lower()
    return any(cat in categories for cat in user_cats)


def to_history_key(link: str) -> str:
    return (link or "").strip()


def build_news_item(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "headline":       row["headline"],
        "news":           row["news"],
        "notification":   row["notification"],
        "categories":     row["categories"],
        "link":           row["link"],
        "image":          row["image"],
        "published_date": row["published_date"],
    }


def fetch_news_pool(limit: int = NEWS_POOL_LIMIT) -> List[Dict[str, Any]]:
    res = supabase.table("news") \
        .select("*") \
        .order("published_date", desc=True) \
        .limit(limit) \
        .execute()
    return res.data or []


def _as_list(value) -> List[Any]:
    """Supabase `_text`/`_jsonb` columns usually come back parsed already, but be defensive."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def update_user_history(email: str, existing_history: List[str], newly_served_links: List[str]) -> None:
    if not newly_served_links:
        return
    merged = existing_history + [l for l in newly_served_links if l not in existing_history]
    if len(merged) > MAX_HISTORY_ITEMS:
        merged = merged[-MAX_HISTORY_ITEMS:]
    try:
        supabase.table("users").update({"history": merged}).eq("email", email).execute()
    except Exception as e:
        log.warning(f"Failed to update history for {email}: {e}")


# ─────────────────────────── LIKE / SAVE HELPERS ────────────
def categories_to_list(cat_str: str) -> List[str]:
    """Turn the stored 'sub-category, main category' string into a clean list."""
    if not cat_str:
        return []
    parts = re.split(r"[,]+", str(cat_str))
    return [p.strip() for p in parts if p.strip()]


def _item_key(item: Dict[str, Any]) -> str:
    """Unique identity for a liked/saved item = its url."""
    return str((item or {}).get("url") or "").strip()


def _parse_item_array(raw_arr: List[Any]) -> List[Dict[str, Any]]:
    """`likes`/`saved` are _text columns, so each element is stored as a JSON string.
    Parse them back into dicts. Falls back gracefully if an old plain-link string
    is encountered (pre-existing data)."""
    parsed = []
    for el in raw_arr:
        if isinstance(el, dict):
            parsed.append(el)
        elif isinstance(el, str):
            try:
                parsed.append(json.loads(el))
            except Exception:
                parsed.append({"url": el})
    return parsed


def _toggle_user_items(email: str, field: str, item: Dict[str, Any], add: bool) -> Optional[List[Dict[str, Any]]]:
    """Add or remove a full news item (matched by its `url`) from a user's `likes`/`saved`
    column. Returns the updated list of items, or None if the user doesn't exist."""
    res = supabase.table("users").select(field).eq("email", email).execute()
    if not res.data:
        return None

    parsed = _parse_item_array(_as_list(res.data[0].get(field)))
    target_key = _item_key(item)
    changed = False

    if add:
        if not any(_item_key(p) == target_key for p in parsed):
            parsed.append(item)
            changed = True
    else:
        filtered = [p for p in parsed if _item_key(p) != target_key]
        if len(filtered) != len(parsed):
            parsed = filtered
            changed = True

    if changed:
        stored = [json.dumps(p, ensure_ascii=False) for p in parsed]
        supabase.table("users").update({field: stored}).eq("email", email).execute()

    return parsed


# ─────────────────────────── CHAT HELPERS ───────────────────
def get_user_chat_history(email: str) -> List[Dict[str, Any]]:
    res = supabase.table("users").select("chat_history").eq("email", email).execute()
    if not res.data:
        return []
    return _as_list(res.data[0].get("chat_history"))


def save_user_chat_history(email: str, chat_list: List[Dict[str, Any]]) -> None:
    supabase.table("users").update({"chat_history": chat_list}).eq("email", email).execute()


def find_conv_index(chat_list: List[Dict[str, Any]], name: str) -> Optional[int]:
    for i, entry in enumerate(chat_list):
        if isinstance(entry, dict) and name in entry:
            return i
    return None


def conv_messages(chat_list: List[Dict[str, Any]], name: str) -> List[Dict[str, str]]:
    idx = find_conv_index(chat_list, name)
    if idx is None:
        return []
    return chat_list[idx].get(name, {}).get("messages", []) or []


def append_messages(chat_list: List[Dict[str, Any]], name: str, messages: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    idx = find_conv_index(chat_list, name)
    if idx is not None:
        existing = chat_list[idx].setdefault(name, {}).setdefault("messages", [])
        existing.extend(messages)
    else:
        chat_list.append({name: {"messages": list(messages)}})
    return chat_list


def slugify_conv_name(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug[:40] or "chat"


def unique_conv_name(chat_list: List[Dict[str, Any]], base: str) -> str:
    existing_names = {k for entry in chat_list if isinstance(entry, dict) for k in entry.keys()}
    if base not in existing_names:
        return base
    suffix = int(time.time())
    return f"{base}_{suffix}"


def find_related_news(message: str, limit: int = CHAT_CONTEXT_ROWS) -> List[Dict[str, Any]]:
    """Lightweight keyword grounding: pull recent news whose headline/categories/body
    mention any meaningful word from the user's message, so Nova's reply can reference
    real stored articles instead of guessing."""
    words = [w for w in re.findall(r"[a-zA-Z]{4,}", message.lower())]
    if not words:
        return []
    pool = fetch_news_pool(limit=100)
    scored = []
    for row in pool:
        haystack = f"{row.get('headline','')} {row.get('categories','')} {row.get('news','')}".lower()
        score = sum(1 for w in words if w in haystack)
        if score > 0:
            scored.append((score, row))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [build_news_item(r) for _, r in scored[:limit]]


def compose_nova_reply(message: str, prior_messages: List[Dict[str, str]]) -> Dict[str, Any]:
    """Compose Nova's reply and grounding info without making the Gemini call.
    Returns dict with keys: 'reply' (str), 'grounded_in' (str: 'local'|'rss'|'none').
    The actual Gemini call should be done elsewhere; this function only prepares
    the prompt and determines grounding.
    """
    context_rows = find_related_news(message)
    context_block = ""
    grounded_in = "none"
    if context_rows:
        lines = []
        for item in context_rows:
            lines.append(f"- {item['headline']}: {item['news'][:220]}")
        context_block = "Relevant stories from Slide's news database:\n" + "\n".join(lines)
        grounded_in = "local"

    history_block = ""
    if prior_messages:
        turns = []
        for m in prior_messages[-6:]:
            for sender, text in m.items():
                turns.append(f"{sender}: {text}")
        history_block = "Recent conversation:\n" + "\n".join(turns)

    prompt = f"""
You are Nova, the friendly, sharp AI assistant inside the Slide news app.
Answer conversationally and concisely. If the user asks about a news topic and
relevant stories are provided below, ground your answer in them and mention it's
based on Slide's coverage. If nothing relevant is provided, answer from general
knowledge and say so plainly rather than guessing at specifics.

{history_block}

{context_block}

User: {message}
Nova:
""".strip()

    # We do NOT call the model here; just return the prompt and grounding info.
    # The caller will invoke model.generate_content(prompt) and handle errors.
    return {
        "prompt": prompt,
        "grounded_in": grounded_in,
        "history_block": history_block,
        "context_block": context_block,
    }


def generate_nova_reply(message: str, prior_messages: List[Dict[str, str]]) -> str:
    """Generate a reply from Nova, with graceful fallback."""
    try:
        composed = compose_nova_reply(message, prior_messages)
        response = model.generate_content(composed["prompt"])
        reply = (response.text or "").strip()
        if not reply:
            return FALLBACK_REPLY
        return reply
    except Exception as e:
        log.error(f"/chat generation failed: {e}")
        return FALLBACK_REPLY


# ─────────────────────────── TELEGRAM HELPER ────────────────
def send_telegram_message(text: str) -> None:
    """Send a message via Telegram Bot API. Silently fails if token missing."""
    if not TELEGRAM_BOT_TOKEN:
        log.warning("Telegram bot token not set; skipping message send.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text
    }
    try:
        resp = requests.post(url, json=payload, timeout=5)
        if not resp.ok:
            log.error(f"Telegram send failed: {resp.status_code} {resp.text}")
    except Exception as e:
        log.error(f"Telegram send exception: {e}")


# ─────────────────────────── ROUTES: CORE ───────────────────
@app.get("/")
def root():
    return {
        "service": "Slide - AI News Aggregator + Nova Chat",
        "status": "running",
        "endpoints": {
            "GET /health": "Liveness check",
            "GET /news": "Trigger background RSS ingestion",
            "POST /get_news": "Personalised, unseen news for a user",
            "POST /like": "Like a news link",
            "POST /unlike": "Unlike a news link",
            "POST /save": "Save a news link",
            "POST /unsave": "Unsave a news link",
            "POST /get_liked": "Full news rows a user has liked",
            "POST /get_saved": "Full news rows a user has saved",
            "POST /list_chats": "List a user's Nova conversations",
            "POST /get_chat": "Get one conversation's messages",
            "POST /getchat": "Alias of /get_chat",
            "POST /append_chat": "Append a message to a conversation",
            "POST /rename_chat": "Rename a conversation",
            "POST /delete_chat": "Delete a conversation",
            "POST /chat": "Talk to Nova (Gemini-powered, grounded in Slide's news)",
            "POST /ws/chat": "WebSocket endpoint for streaming Nova chat",
            "POST /update_suggestions": "Update user's category suggestions",
        },
    }


@app.get("/health")
def health():
    return {"status": "running"}


@app.get("/news")
def trigger_news_fetch(background_tasks: BackgroundTasks):
    """
    Immediately returns 202 Accepted.
    The RSS fetch + Gemini rewrite pipeline runs in the background so it never
    times out on Render's 30-second request limit. Safe to call from a cron job
    or the frontend.
    """
    background_tasks.add_task(run_news_fetch)
    return JSONResponse(
        status_code=202,
        content={"status": "accepted", "message": "News fetch started in background"}
    )


class GetNews(BaseModel):
    email: EmailStr
    categories: Optional[List[str]] = None   # optional override of saved suggestions
    max_results: Optional[int] = None        # optional override of default page size


@app.post("/get_news")
def get_news(req: GetNews):
    """
    Returns personalised, previously-unseen news for a user, instantly from
    Supabase — no AI calls, no scraping, no timeouts.
    """
    email       = req.email
    max_results = req.max_results or DEFAULT_MAX_RESULTS

    user_row = get_user_row(email, select="suggestions, history")
    if not user_row:
        return {"news": [], "trigger_refresh": True}

    saved_suggestions = user_row.get("suggestions") or []
    history: List[str] = _as_list(user_row.get("history"))
    seen_links = set(to_history_key(l) for l in history)

    user_cats = normalize_categories(req.categories or saved_suggestions)

    pool = fetch_news_pool()

    # Pass 1: unseen + category match
    matched: List[Dict[str, Any]] = []
    seen_this_request = set()
    for row in pool:
        link = row.get("link")
        if not link or link in seen_this_request or to_history_key(link) in seen_links:
            continue
        if matches_categories(row.get("categories") or "", user_cats):
            matched.append(build_news_item(row))
            seen_this_request.add(link)
        if len(matched) >= max_results:
            break

    fallback_used = False
    # Pass 2: fallback — unseen, ignore category filter, fill remaining slots
    if len(matched) < MIN_UNSEEN_TARGET:
        fallback_used = True
        for row in pool:
            link = row.get("link")
            if not link or link in seen_this_request or to_history_key(link) in seen_links:
                continue
            matched.append(build_news_item(row))
            seen_this_request.add(link)
            if len(matched) >= max_results:
                break

    served_links = [item["link"] for item in matched]
    update_user_history(email, history, served_links)

    return {
        "news": matched,
        "trigger_refresh": len(matched) < 5,
        "meta": {
            "categories_used": user_cats,
            "fallback_used": fallback_used,
            "served_count": len(matched),
        },
    }


# ─────────────────────────── ROUTES: LIKE / SAVE ────────────
class NewsItemAction(BaseModel):
    email: EmailStr
    headline: Optional[str] = None
    news: Optional[str] = None
    url: str
    image_url: Optional[str] = None
    categories: Optional[List[str]] = None


@app.post("/like")
def like_news(req: NewsItemAction):
    item = req.dict(exclude={"email"}, exclude_none=True)
    arr = _toggle_user_items(req.email, "likes", item, add=True)
    if arr is None:
        return JSONResponse(status_code=404, content={"error": "user not found"})
    return {"status": "ok", "likes": arr}


@app.post("/unlike")
def unlike_news(req: NewsItemAction):
    item = req.dict(exclude={"email"}, exclude_none=True)
    arr = _toggle_user_items(req.email, "likes", item, add=False)
    if arr is None:
        return JSONResponse(status_code=404, content={"error": "user not found"})
    return {"status": "ok", "likes": arr}


@app.post("/save")
def save_news(req: NewsItemAction):
    item = req.dict(exclude={"email"}, exclude_none=True)
    arr = _toggle_user_items(req.email, "saved", item, add=True)
    if arr is None:
        return JSONResponse(status_code=404, content={"error": "user not found"})
    return {"status": "ok", "saved": arr}


@app.post("/unsave")
def unsave_news(req: NewsItemAction):
    item = req.dict(exclude={"email"}, exclude_none=True)
    arr = _toggle_user_items(req.email, "saved", item, add=False)
    if arr is None:
        return JSONResponse(status_code=404, content={"error": "user not found"})
    return {"status": "ok", "saved": arr}


class EmailOnly(BaseModel):
    email: EmailStr


@app.post("/get_liked")
def get_liked(req: EmailOnly):
    user_row = get_user_row(req.email, select="likes")
    if not user_row:
        return JSONResponse(status_code=404, content={"error": "user not found"})
    return {"news": _parse_item_array(_as_list(user_row.get("likes")))}


@app.post("/get_saved")
def get_saved(req: EmailOnly):
    user_row = get_user_row(req.email, select="saved")
    if not user_row:
        return JSONResponse(status_code=404, content={"error": "user not found"})
    return {"news": _parse_item_array(_as_list(user_row.get("saved")))}


# ─────────────────────────── ROUTES: CHAT / CONVERSATIONS ───
class ListChatsRequest(BaseModel):
    email: EmailStr


@app.post("/list_chats")
def list_chats(req: ListChatsRequest):
    chat_list = get_user_chat_history(req.email)
    out = []
    for entry in chat_list:
        if not isinstance(entry, dict):
            continue
        for name, payload in entry.items():
            messages = (payload or {}).get("messages", []) or []
            last_preview = ""
            if messages:
                last = messages[-1]
                for _, text in last.items():
                    last_preview = text
                    break
            out.append({
                "name": name,
                "message_count": len(messages),
                "last_message": last_preview,
            })
    return {"conversations": out}


class GetChatRequest(BaseModel):
    email: EmailStr
    conversation_name: str


def _get_chat_impl(req: GetChatRequest):
    chat_list = get_user_chat_history(req.email)
    messages = conv_messages(chat_list, req.conversation_name)
    return {"conversation_name": req.conversation_name, "messages": messages}


@app.post("/get_chat")
def get_chat(req: GetChatRequest):
    return _get_chat_impl(req)


@app.post("/getchat")
def getchat(req: GetChatRequest):
    """Alias of /get_chat for naming compatibility."""
    return _get_chat_impl(req)


class AppendChatRequest(BaseModel):
    user_email: EmailStr
    conversation_name: str
    element_json_string: str


@app.post("/append_chat")
def append_chat(req: AppendChatRequest):
    try:
        parsed = json.loads(req.element_json_string)
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid element_json_string"})

    entry = parsed.get(req.conversation_name) or {}
    new_messages = entry.get("messages", []) or []
    if not new_messages:
        return JSONResponse(status_code=400, content={"error": "no messages found in element_json_string"})

    chat_list = get_user_chat_history(req.user_email)
    chat_list = append_messages(chat_list, req.conversation_name, new_messages)
    save_user_chat_history(req.user_email, chat_list)

    return {"status": "ok", "conversation_name": req.conversation_name}


class RenameChatRequest(BaseModel):
    user_email: EmailStr
    old_name: str
    new_name: str


@app.post("/rename_chat")
def rename_chat(req: RenameChatRequest):
    chat_list = get_user_chat_history(req.user_email)
    idx = find_conv_index(chat_list, req.old_name)
    if idx is None:
        return JSONResponse(status_code=404, content={"error": "conversation not found"})
    if find_conv_index(chat_list, req.new_name) is not None:
        return JSONResponse(status_code=409, content={"error": "a conversation with new_name already exists"})

    content = chat_list[idx].pop(req.old_name)
    chat_list[idx][req.new_name] = content
    save_user_chat_history(req.user_email, chat_list)

    return {"status": "ok", "old_name": req.old_name, "new_name": req.new_name}


class DeleteChatRequest(BaseModel):
    user_email: EmailStr
    conversation_name: str


@app.post("/delete_chat")
def delete_chat(req: DeleteChatRequest):
    chat_list = get_user_chat_history(req.user_email)
    idx = find_conv_index(chat_list, req.conversation_name)
    if idx is None:
        return JSONResponse(status_code=404, content={"error": "conversation not found"})
    del chat_list[idx]
    save_user_chat_history(req.user_email, chat_list)
    return {"status": "ok", "deleted": req.conversation_name}


class ChatRequest(BaseModel):
    email: EmailStr
    message: str
    conversation_name: Optional[str] = None


@app.post("/chat")
def chat(req: ChatRequest):
    """
    Talks to Nova. Grounds the reply in matching Slide news rows when possible,
    generates a response with Gemini, and persists both the user's message and
    Nova's reply into the conversation's chat_history entry.
    """
    chat_list = get_user_chat_history(req.email)

    conversation_name = req.conversation_name
    if not conversation_name:
        conversation_name = unique_conv_name(chat_list, slugify_conv_name(req.message))

    prior_messages = conv_messages(chat_list, conversation_name)

    try:
        reply_text = generate_nova_reply(req.message, prior_messages)
    except Exception as e:
        log.error(f"/chat generation failed: {e}")
        return JSONResponse(status_code=500, content={"error": "failed to generate a reply: " + str(e)})

    chat_list = append_messages(chat_list, conversation_name, [
        {"Sir": req.message},
        {"Nova": reply_text},
    ])
    save_user_chat_history(req.email, chat_list)

    return {
        "reply": reply_text,
        "conversation_name": conversation_name,
    }


# ─────────────────────────── WEBSOCKET CHAT ────────────────
# Status message pools per stage
THINKING_MESSAGES = [
    "🧠 Hang tight — Nova's grabbing a coffee and scanning the desk...",
    "🧠 One sec, letting the gears in the newsroom spin up...",
    "🧠 Nova's cracking her knuckles, about to dig in...",
]
SEARCHING_LOCAL_MESSAGES = [
    "📚 Rifling through Slide's back issues for you...",
    "📚 Checking if we've already scooped this one...",
    "📚 Peeking into the archive drawer marked 'recent'...",
]
SEARCHING_RSS_MESSAGES = [
    "📡 Nothing on file — patching into the newswire...",
    "📡 Going live to the source for the freshest take...",
    "📡 Dialing into the wire room for a fresh lead...",
]
WRITING_MESSAGES = [
    "✍️ Nova's tapping away at the keyboard...",
    "✍️ Turning the raw story into something worth reading...",
    "✍️ Just cleaning up the copy before it hits your screen...",
]


def _random_from_pool(pool: List[str]) -> str:
    return random.choice(pool)


@app.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_json()
            email: str = data.get("email", "")
            message: str = data.get("message", "")
            conversation_name: Optional[str] = data.get("conversation_name")

            if not email:
                await websocket.send_json({
                    "type": "final",
                    "reply": "I need your email to keep chatting — please provide it.",
                    "conversation_name": "",
                    "grounded_in": "none"
                })
                continue

            # Retrieve or create chat history
            chat_list = get_user_chat_history(email)
            if conversation_name is None:
                conversation_name = unique_conv_name(chat_list, slugify_conv_name(message))

            prior_messages = conv_messages(chat_list, conversation_name)

            # Build history block (same as in compose)
            history_block = ""
            if prior_messages:
                turns = []
                for m in prior_messages[-6:]:
                    for sender, text in m.items():
                        turns.append(f"{sender}: {text}")
                history_block = "Recent conversation:\n" + "\n".join(turns)

            # Determine if this is a news request based on presence of keywords
            words = [w for w in re.findall(r"[a-zA-Z]{4,}", message.lower())]
            is_news_request = bool(words)

            # Initialize grounding info
            context_rows: List[Dict[str, Any]] = []
            context_block = ""
            grounded_in = "none"

            # Status: thinking
            await websocket.send_json({
                "type": "status",
                "stage": "thinking",
                "text": _random_from_pool(THINKING_MESSAGES)
            })

            if is_news_request:
                # Searching local (Supabase)
                await websocket.send_json({
                    "type": "status",
                    "stage": "searching_local",
                    "text": _random_from_pool(SEARCHING_LOCAL_MESSAGES)
                })
                context_rows = find_related_news(message)
                if context_rows:
                    lines = []
                    for item in context_rows:
                        lines.append(f"- {item['headline']}: {item['news'][:220]}")
                    context_block = "Relevant stories from Slide's news database:\n" + "\n".join(lines)
                    grounded_in = "local"
                else:
                    # No local results, try searching RSS (simulated)
                    await websocket.send_json({
                        "type": "status",
                        "stage": "searching_rss",
                        "text": _random_from_pool(SEARCHING_RSS_MESSAGES)
                    })
                    # In a real implementation we might trigger a background fetch here.
                    # For now we treat as none.
                    grounded_in = "rss"
            else:
                # Not a news request; we still go to writing stage.
                pass

            # Status: writing
            await websocket.send_json({
                "type": "status",
                "stage": "writing",
                "text": _random_from_pool(WRITING_MESSAGES)
            })

            # Compose prompt and attempt generation
            try:
                composed = compose_nova_reply(message, prior_messages)
                response = model.generate_content(composed["prompt"])
                reply = (response.text or "").strip()
                if not reply:
                    reply = FALLBACK_REPLY
            except Exception as e:
                log.error(f"/ws/chat generation failed: {e}")
                reply = FALLBACK_REPLY

            # Update chat history
            chat_list = append_messages(chat_list, conversation_name, [
                {"Sir": message},
                {"Nova": reply},
            ])
            save_user_chat_history(email, chat_list)

            # Send final frame
            await websocket.send_json({
                "type": "final",
                "reply": reply,
                "conversation_name": conversation_name,
                "grounded_in": grounded_in
            })
    except WebSocketDisconnect:
        # Client disconnected; clean up if needed
        pass
    except Exception as e:
        log.error(f"WebSocket error: {e}")
        try:
            await websocket.send_json({
                "type": "final",
                "reply": "Something went wrong on our end. Please reconnect and try again.",
                "conversation_name": "",
                "grounded_in": "none"
            })
        except Exception:
            pass
    finally:
        await websocket.close()


# ─────────────────────────── UPDATE SUGGESTIONS ────────────
class UpdateSuggestionsRequest(BaseModel):
    email: EmailStr
    categories1: Optional[List[str]] = None
    categories2: Optional[List[str]] = None
    categories3: Optional[List[str]] = None


@app.post("/update_suggestions")
def update_suggestions(req: UpdateSuggestionsRequest):
    email = req.email
    # Normalize input lists (ensure they are lists)
    cat1 = req.categories1 or []
    cat2 = req.categories2 or []
    cat3 = req.categories3 or []

    # We'll work with lowercased versions for counting/dedup, but keep first original case.
    freq: Dict[str, int] = {}
    source_priority: Dict[str, int] = {}  # lower number = higher priority
    original_map: Dict[str, str] = {}     # lower -> first original seen

    def process_list(categories: List[str], priority: int):
        for cat in categories:
            if not isinstance(cat, str):
                continue
            cat_lower = cat.strip().lower()
            if not cat_lower:
                continue
            freq[cat_lower] = freq.get(cat_lower, 0) + 1
            if cat_lower not in source_priority or priority < source_priority[cat_lower]:
                source_priority[cat_lower] = priority
            if cat_lower not in original_map:
                original_map[cat_lower] = cat  # keep first occurrence

    process_list(cat1, 1)
    process_list(cat2, 2)
    process_list(cat3, 3)

    # Sort by frequency desc, then source priority asc
    sorted_cats = sorted(freq.items(), key=lambda x: (-x[1], source_priority[x[0]]))
    # Take top 2 unique categories
    top2_lower = [cat for cat, _ in sorted_cats[:2]]
    top2_original = [original_map[cat] for cat in top2_lower]

    # Fetch existing suggestions
    user_row = get_user_row(email, select="suggestions")
    existing_raw = user_row.get("suggestions") if user_row else []
    existing = _as_list(existing_raw)  # ensure list of strings
    # Normalize existing for comparison
    existing_lower_set = {str(s).strip().lower() for s in existing if isinstance(s, str)}

    # Build final list
    final: List[str] = []
    # Add top 2
    final.extend(top2_original)
    # Find first existing suggestion not already in top2 (case-insensitive)
    added_third = False
    for sug in existing:
        if not isinstance(sug, str):
            continue
        sug_low = sug.strip().lower()
        if sug_low not in {c.lower() for c in top2_original}:
            final.append(sug)
            added_third = True
            break
    # Append remaining existing suggestions in order, skipping duplicates, until we have 4
    for sug in existing:
        if not isinstance(sug, str):
            continue
        sug_low = sug.strip().lower()
        if sug_low in {c.lower() for c in final}:
            continue
        final.append(sug)
        if len(final) >= 4:
            break

    # Ensure we don't exceed 4
    final = final[:4]

    # Persist back to Supabase
    try:
        supabase.table("users").update({"suggestions": final}).eq("email", email).execute()
    except Exception as e:
        log.error(f"Failed to update suggestions for {email}: {e}")
        # Still try to send telegram? We'll send failure message.
        send_telegram_message(f"{email} suggestions update FAILED: {e}")
        return JSONResponse(status_code=500, content={"error": "failed to update suggestions"})

    # Send telegram success notification
    send_telegram_message(f"{email} suggestions updated successfully")

    return {"status": "ok", "suggestions": final}


# ─────────────────────────── MAIN ───────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)