#!/usr/bin/env python3
"""
AI Research Feed Agent
- Scrapes tweets using snscrape
- Enriches startup links (website meta, GitHub)
- Classifies domain
- Writes entries to Notion or CSV
- Deduplicates by tweet id stored in local sqlite or json
"""

import os
import re
import json
import time
import hashlib
import sqlite3
import logging
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
import snscrape.modules.twitter as sntwitter
from notion_client import Client as NotionClient
from dateutil import parser as dateparser
from tqdm import tqdm
from dotenv import load_dotenv

load_dotenv()

# -------------- Configuration (env) -----------------
NOTION_TOKEN = os.getenv("NOTION_TOKEN")  # required if using Notion
NOTION_DB_ID = os.getenv("NOTION_DB_ID")
WORKDIR = Path(os.getenv("WORKDIR", "."))

# Query terms (you can tune or add)
QUERY = os.getenv("SNS_QUERY", '"AI startup" OR "AI breakthrough" OR "LLM" OR "Machine Learning" OR "Deep Learning" OR "AI company" OR "new AI model" OR "algorithmic improvement" OR "AI research paper" OR "AI funding" OR "AI launch" OR "AI tool"')
# Max tweets per run
MAX_TWEETS = int(os.getenv("MAX_TWEETS", "200"))
# Age limit for tweets (days)
MAX_TWEET_AGE_DAYS = int(os.getenv("MAX_TWEET_AGE_DAYS", "14"))

# Dedupe DB
SQLITE_DB = WORKDIR / "ai_feed_state.db"

# Logging config
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# Domain classification rules (keywords -> domain)
DOMAIN_RULES = {
    "Generative AI": ["gpt", "llama", "stable diffusion", "diffusion", "text-to-image", "text-to-speech", "generative"],
    "Computer Vision": ["vision", "cv", "image", "object detection", "segmentation", "pose", "3d", "video"],
    "Natural Language Processing": ["nlp", "transformer", "language model", "question answering", "summariz", "llm"],
    "MLOps / Dev Tools": ["mlo ps", "mlops", "monitor", "observability", "deploy", "pipeline", "kubernetes", "docker"],
    "AI Infrastructure": ["infrastructure", "gpu", "tpu", "accelerator", "inference", "vector db", "faiss", "milvus", "redis vector"],
    "Robotics": ["robot", "robotics", "manipulation", "autonomy"],
    "Healthcare AI": ["clinical", "medical", "diagnostic", "healthcare", "radiology"],
    "Fintech AI": ["finance", "fintech", "fraud", "trading", "risk"],
    "Research & Academia": ["paper", "arxiv", "icml", "neurips", "cvpr", "iclr"],
}

# Heuristics for skipping hype
MIN_LINKS_REQUIRED = 1  # prefer tweets with links
MIN_WORDS = 6

# ---------------------------------------------------

def init_db():
    conn = sqlite3.connect(SQLITE_DB)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS processed (
            id TEXT PRIMARY KEY,
            tweet_id TEXT,
            created_at TEXT,
            domain TEXT,
            added_at TEXT
        )
    """)
    conn.commit()
    return conn

def mark_processed(conn, unique_id, tweet_id, domain):
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO processed (id, tweet_id, created_at, domain, added_at) VALUES (?, ?, ?, ?, ?)",
                (unique_id, tweet_id, datetime.utcnow().isoformat(), domain, datetime.utcnow().isoformat()))
    conn.commit()

def is_processed(conn, unique_id):
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM processed WHERE id = ?", (unique_id,))
    return cur.fetchone() is not None

def hash_text(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

def extract_links_from_text(text):
    return re.findall(r'(https?://\S+)', text)

def fetch_metadata(url, timeout=8):
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "ai-research-agent/1.0"})
        if resp.status_code != 200:
            return {}
        soup = BeautifulSoup(resp.text, "html.parser")
        meta = {}
        meta["title"] = (soup.title.string.strip() if soup.title else "")[:300]
        og_desc = soup.find("meta", {"property": "og:description"}) or soup.find("meta", {"name":"description"})
        meta["description"] = og_desc["content"].strip() if og_desc and og_desc.get("content") else ""
        # collect social links common patterns
        links = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if any(x in href for x in ["twitter.com", "github.com", "linkedin.com", "angel.co"]):
                links.append(href)
        meta["social_links"] = list(dict.fromkeys(links))
        return meta
    except Exception as e:
        logging.debug("fetch_metadata error for %s: %s", url, e)
        return {}

def classify_text(text):
    t = text.lower()
    for domain, keywords in DOMAIN_RULES.items():
        for kw in keywords:
            if kw in t:
                return domain
    return "Other"

def clean_url(u):
    try:
        parsed = urlparse(u)
        if not parsed.scheme:
            u = "http://" + u
    except Exception:
        pass
    return u

def build_notion_page_payload(entry):
    # Convert entry dict to Notion page payload under your schema
    props = {
        "Domain": {"select": {"name": entry.get("domain", "Other")}},
        "Startup / Entity": {"title": [{"text": {"content": entry.get("startup", entry.get("title","Unknown"))}}]},
        "Description": {"rich_text": [{"text": {"content": entry.get("description","")}}]},
        "Business Model": {"select": {"name": entry.get("business_model","Unknown")}},
        "Tech Used": {"multi_select": [{"name": t} for t in entry.get("tech_used", [])]},
        "Advancements": {"rich_text": [{"text": {"content": entry.get("advancements","")}}]},
        "Tweet Link": {"url": entry.get("tweet_link")},
        "Tweet Date": {"date": {"start": entry.get("tweet_date")}},
        "Author Handle": {"rich_text": [{"text": {"content": entry.get("author_handle","")}}]},
        "Source Links": {"rich_text": [{"text": {"content": ", ".join(entry.get("source_links", []))}}]},
        "Unique ID": {"rich_text": [{"text": {"content": entry.get("unique_id")}}]},
        "Ingested At": {"date": {"start": datetime.utcnow().isoformat()}}
    }
    return {"parent": {"database_id": NOTION_DB_ID}, "properties": props}

def push_to_notion(notion, entry):
    try:
        payload = build_notion_page_payload(entry)
        notion.pages.create(payload)
        logging.info("Pushed to Notion: %s", entry.get("startup") or entry.get("title"))
    except Exception as e:
        logging.warning("Failed to push to Notion: %s", e)

def is_tweet_sensible(tweet):
    text = tweet.content.strip()
    if len(text.split()) < MIN_WORDS:
        return False
    if len(extract_links_from_text(text)) < MIN_LINKS_REQUIRED:
        return False
    return True

def parse_tweet(tweet):
    # tweet is snscrape Tweet object
    tweet_text = tweet.content
    tweet_date = tweet.date.isoformat()
    tweet_id = str(tweet.id)
    author = tweet.user.username
    author_link = f"https://twitter.com/{author}"
    links = extract_links_from_text(tweet_text)
    return {
        "tweet_text": tweet_text,
        "tweet_date": tweet_date,
        "tweet_id": tweet_id,
        "author": author,
        "author_link": author_link,
        "links": links
    }

def enrich_entry(parsed):
    entry = {}
    entry["tweet_link"] = f"https://twitter.com/{parsed['author']}/status/{parsed['tweet_id']}"
    entry["tweet_date"] = parsed["tweet_date"]
    entry["author_handle"] = parsed["author"]
    entry["tweet_content"] = parsed["tweet_text"]
    entry["source_links"] = parsed["links"]
    entry["unique_id"] = parsed["tweet_id"]

    # Basic classification
    entry["domain"] = classify_text(parsed["tweet_text"])

    # If first link looks like a startup website or github, fetch metadata
    if parsed["links"]:
        url = clean_url(parsed["links"][0])
        meta = fetch_metadata(url)
        entry["title"] = meta.get("title") or url
        entry["description"] = meta.get("description", "")
        entry["social_links"] = meta.get("social_links", [])
        # heuristics for business model and tech stack by keywords in meta
        t = (entry["description"] + " " + parsed["tweet_text"]).lower()
        techs = []
        if "transformer" in t or "gpt" in t or "llm" in t: techs.append("Transformers / LLMs")
        if "diffusion" in t or "stable diffusion" in t or "ddpm" in t: techs.append("Diffusion models")
        if "vector" in t or "faiss" in t or "milvus" in t: techs.append("Vector DB")
        if "api" in t: entry["business_model"] = "API"
        else: entry.setdefault("business_model", "Unknown")
        entry["tech_used"] = techs
        entry["advancements"] = parsed["tweet_text"]
    else:
        entry["title"] = parsed["tweet_text"][:60]
        entry["description"] = ""
        entry["business_model"] = "Unknown"
        entry["tech_used"] = []
        entry["advancements"] = parsed["tweet_text"]

    return entry

def generate_weekly_highlights(conn, top_n=5):
    # naive: count by domain in last 7 days
    cur = conn.cursor()
    week_ago = (datetime.utcnow() - timedelta(days=7)).isoformat()
    cur.execute("SELECT domain, count(*) as cnt FROM processed WHERE added_at >= ? GROUP BY domain ORDER BY cnt DESC", (week_ago,))
    rows = cur.fetchall()
    highlights = []
    for domain, cnt in rows[:top_n]:
        highlights.append(f"{domain}: {cnt} new items in last 7 days")
    return highlights

def main():
    conn = init_db()
    last_run_file = WORKDIR / ".last_run"
    last_run = None
    if last_run_file.exists():
        last_run = dateparser.parse(last_run_file.read_text().strip())
    else:
        # default: look back MAX_TWEET_AGE_DAYS days
        last_run = datetime.utcnow() - timedelta(days=MAX_TWEET_AGE_DAYS)

    since_str = last_run.strftime("%Y-%m-%d")
    query = f'{QUERY} since:{since_str}'

    logging.info("Running query: %s", query)
    tweets = []
    for i, tweet in enumerate(sntwitter.TwitterSearchScraper(query).get_items()):
        if i >= MAX_TWEETS:
            break
        # skip old tweets beyond MAX_TWEET_AGE_DAYS
        if (datetime.utcnow() - tweet.date).days > MAX_TWEET_AGE_DAYS:
            continue
        tweets.append(tweet)

    logging.info("Found %d tweets", len(tweets))
    notion = None
    if NOTION_TOKEN and NOTION_DB_ID:
        notion = NotionClient(auth=NOTION_TOKEN)

    saved = 0
    for tweet in tqdm(tweets):
        parsed = parse_tweet(tweet)
        unique_id = parsed["tweet_id"]
        if is_processed(conn, unique_id):
            continue
        # Basic filtering: skip tiny/no-link tweets
        if not is_tweet_sensible(tweet):
            logging.debug("Skipping tweet %s: not sensible", unique_id)
            mark_processed(conn, unique_id, unique_id, "Filtered")
            continue
        entry = enrich_entry(parsed)
        # push to Notion or save to CSV/json
        if notion:
            push_to_notion(notion, entry)
        else:
            # fallback: append to local JSONL
            out_file = WORKDIR / "ai_feed_out.jsonl"
            with open(out_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            logging.info("Saved locally: %s", entry.get("title"))
        mark_processed(conn, unique_id, unique_id, entry.get("domain","Other"))
        saved += 1

    # update last run
    last_run_file.write_text(datetime.utcnow().isoformat())
    logging.info("Saved %d new items", saved)

    # generate weekly highlights
    highlights = generate_weekly_highlights(conn)
    if highlights:
        logging.info("Weekly highlights:\n- %s", "\n- ".join(highlights))

if __name__ == "__main__":
    main()
