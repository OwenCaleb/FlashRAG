#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Robust site crawler -> FlashRAG-ready JSONL

Features
- Clear logging with timestamps, progress, heartbeat
- robots.txt aware (toggleable)
- Optional sitemap.xml seeding
- Resume-safe: appends to existing outputs; skips already-seen URLs
- Saves raw HTML + cleaned text for audit
- Streams JSONL/CSV while crawling (no giant memory buffers)
- Optional chunking (by characters with overlap)
- Deduplicates chunks by hash to avoid repeated content
- Inclusion/Exclusion regex filters
- Graceful Ctrl+C (SIGINT) handling with clean summary

Outputs in --out-dir:
  html/                  raw HTML
  text/                  cleaned plaintext
  corpus_min.jsonl       {"id","contents"} lines
  corpus_full.jsonl      {"id","url","title","chunk_index","chunk_count","contents","hash"} lines
  manifest.csv           id,url,title,html_path,txt_path,chars
  crawl.log              detailed log (if --log-file not set)
  stats.json             run summary (pages, chunks, skipped,...)
"""

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, Set, Tuple, List, Iterable
from urllib.parse import urljoin, urlparse, urlunparse
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.robotparser import RobotFileParser

# ---------------------- Config & Defaults ----------------------

TEXT_TAGS_TO_REMOVE = ["script", "style", "noscript", "svg", "img", "iframe", "button", "form", "nav", "header", "footer", "aside"]
ALLOWED_SCHEMES = {"http", "https"}
HTML_EXTS = (".html", ".htm", ".md", ".txt", ".php", ".aspx", "")  # "" for clean directory URLs

# ---------------------- Utilities ----------------------

def normalize_url(url: str) -> str:
    """Canonicalize URL: strip query/fragment, normalize trailing slash for directories."""
    p = urlparse(url)
    if p.scheme not in ALLOWED_SCHEMES:
        return ""
    # Drop query & fragment
    p2 = p._replace(query="", fragment="")
    # Normalize directory path to end with slash
    path = p2.path or "/"
    _, ext = os.path.splitext(path)
    if not ext and not path.endswith("/"):
        path = path + "/"
    p2 = p2._replace(path=path)
    return urlunparse(p2)

def is_within_base(url: str, base_netloc: str, base_path: str) -> bool:
    p = urlparse(url)
    return p.netloc == base_netloc and p.path.startswith(base_path)

def url_to_relpath(url: str, base_path: str) -> str:
    """Return a RELATIVE file path (no leading slash). Directory URLs -> index.html"""
    p = urlparse(url)
    path = p.path
    if not path.startswith(base_path):
        rel = path.lstrip("/")
    else:
        rel = path[len(base_path):].lstrip("/")
    # Directory -> index.html
    root_like = (rel == "" or rel.endswith("/"))
    if root_like or not os.path.splitext(rel)[1]:
        rel = (rel.rstrip("/") + "/index.html").lstrip("/")
    rel = rel.replace("..", "_")
    return rel or "index.html"

def extract_text_and_title(html: str) -> Tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    for tname in TEXT_TAGS_TO_REMOVE:
        for tag in soup.find_all(tname):
            tag.decompose()
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    # Preserve headings as markdown-like markers for readability
    for lvl in range(1, 7):
        for h in soup.find_all(f"h{lvl}"):
            txt = h.get_text(" ", strip=True)
            h.insert_before(soup.new_string("\n" + ("#" * lvl) + " " + txt + "\n"))
            h.decompose()
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, title

def make_session(user_agent: str, timeout: float, retries: int = 3, backoff: float = 0.5) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": user_agent})
    retry = Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=64)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.request_timeout = timeout
    return s

def read_existing_urls_from_manifest(manifest_path: Path) -> Set[str]:
    seen = set()
    try:
        with manifest_path.open("r", encoding="utf-8") as fr:
            reader = csv.DictReader(fr)
            for row in reader:
                if "url" in row:
                    seen.add(row["url"])
    except FileNotFoundError:
        pass
    return seen

def chunk_text(text: str, max_chars: int, overlap: int) -> List[str]:
    if max_chars <= 0:
        return [text] if text else []
    # Split on double-newlines as paragraph boundaries
    paras = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    chunks, buf = [], ""
    for p in paras:
        if len(buf) + len(p) + 1 <= max_chars:
            buf = (buf + "\n" + p).strip()
        else:
            if buf:
                chunks.append(buf)
            buf = (buf[-overlap:] + "\n" + p).strip() if overlap and len(buf) > overlap else p
    if buf:
        chunks.append(buf)
    # Edge case: still empty
    return chunks or ([] if not text.strip() else [text[:max_chars]])

def hash_text(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()

def parse_sitemap_candidates(base_url: str) -> Iterable[str]:
    """Yield URLs from likely sitemap locations; ignore errors."""
    p = urlparse(base_url)
    site_root = f"{p.scheme}://{p.netloc}"
    candidates = [site_root + "/sitemap.xml", site_root + "/sitemap_index.xml"]
    for sm_url in candidates:
        try:
            resp = requests.get(sm_url, timeout=8)
            if resp.status_code != 200 or "xml" not in resp.headers.get("Content-Type",""):
                continue
            root = ET.fromstring(resp.content)
            for loc in root.iter():
                if loc.tag.endswith("loc") and loc.text:
                    yield loc.text.strip()
        except Exception:
            continue

# ---------------------- Data structures ----------------------

@dataclass
class Stats:
    pages_fetched: int = 0
    pages_skipped: int = 0
    pages_failed: int = 0
    chunks_written: int = 0
    chunks_deduped: int = 0
    queue_peek: int = 0

# ---------------------- Crawler ----------------------

class Crawler:
    def __init__(self, base_url: str, out_dir: Path, max_pages: int, delay: float, include_re: Optional[str],
                 exclude_re: Optional[str], respect_robots: bool, user_agent: str, timeout: float,
                 use_sitemap: bool, resume: bool, chunk_size: int, chunk_overlap: int, heartbeat_every: int,
                 save_html: bool, save_text: bool, logger: logging.Logger):
        self.base_url = normalize_url(base_url)
        if not self.base_url:
            raise ValueError("Invalid base-url")
        self.base_p = urlparse(self.base_url)
        self.base_netloc, self.base_path = self.base_p.netloc, self.base_p.path if self.base_p.path.endswith("/") else (self.base_p.path + "/")

        self.out_dir = out_dir.resolve()
        self.out_html = self.out_dir / "html"
        self.out_txt  = self.out_dir / "text"
        self.out_html.mkdir(parents=True, exist_ok=True)
        self.out_txt.mkdir(parents=True, exist_ok=True)

        self.max_pages = max_pages
        self.delay = delay
        self.include_re = re.compile(include_re) if include_re else None
        self.exclude_re = re.compile(exclude_re) if exclude_re else None
        self.respect_robots = respect_robots
        self.user_agent = user_agent
        self.timeout = timeout
        self.use_sitemap = use_sitemap
        self.resume = resume
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.heartbeat_every = heartbeat_every
        self.save_html = save_html
        self.save_text = save_text

        self.logger = logger
        self.session = make_session(user_agent=self.user_agent, timeout=self.timeout)

        self.visited: Set[str] = set()
        self.queue: deque[str] = deque()
        self.page_id = 0
        self.stats = Stats()
        self.chunk_hashes: Set[str] = set()

        # Writers
        self.full_jsonl = (self.out_dir / "corpus_full.jsonl")
        self.min_jsonl  = (self.out_dir / "corpus_min.jsonl")
        self.manifest_csv = (self.out_dir / "manifest.csv")

        # Resume: preload seen URLs and adjust page_id
        if self.resume and self.manifest_csv.exists():
            seen_urls = read_existing_urls_from_manifest(self.manifest_csv)
            self.visited.update(seen_urls)
            self.page_id = sum(1 for _ in open(self.full_jsonl, "r", encoding="utf-8")) if self.full_jsonl.exists() else 0
            self.logger.info(f"Resume enabled: loaded {len(seen_urls)} visited URLs, starting page_id={self.page_id}")
        # Open writers (append mode if resume)
        self.fw_full = open(self.full_jsonl, "a", encoding="utf-8")
        self.fw_min  = open(self.min_jsonl, "a", encoding="utf-8")
        self.fw_mani = open(self.manifest_csv, "a", encoding="utf-8", newline="")
        self.manifest_writer = csv.DictWriter(self.fw_mani, fieldnames=["id","url","title","html_path","txt_path","chars"])
        if not self.resume or (self.resume and os.stat(self.manifest_csv).st_size == 0):
            self.manifest_writer.writeheader()

        # robots.txt
        self.robot_parser = None
        if self.respect_robots:
            self.robot_parser = RobotFileParser()
            robots_url = f"{self.base_p.scheme}://{self.base_p.netloc}/robots.txt"
            try:
                self.robot_parser.set_url(robots_url)
                self.robot_parser.read()
                self.logger.info(f"robots.txt loaded from {robots_url}")
            except Exception as e:
                self.logger.warning(f"robots.txt load failed: {e}")

        # Seed queue
        self.seed_queue()

        # Graceful shutdown
        signal.signal(signal.SIGINT, self._handle_sigint)

    def _handle_sigint(self, signum, frame):
        self.logger.warning("SIGINT received, flushing and exiting gracefully...")
        self.close_writers()
        self.write_stats()
        sys.exit(130)

    def seed_queue(self):
        self.queue.append(self.base_url)
        if self.use_sitemap:
            for link in parse_sitemap_candidates(self.base_url):
                n = normalize_url(link)
                if n and is_within_base(n, self.base_netloc, self.base_path):
                    self.queue.append(n)
            self.logger.info(f"Sitemap seeded: queue size now {len(self.queue)}")

    def can_fetch(self, url: str) -> bool:
        if self.robot_parser:
            try:
                return self.robot_parser.can_fetch(self.user_agent, url)
            except Exception:
                return True
        return True

    def enqueue_links(self, html: str, base_url: str):
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith(("mailto:", "javascript:")):
                continue
            abs_url = normalize_url(urljoin(base_url, href))
            if not abs_url or abs_url in self.visited:
                continue
            if not is_within_base(abs_url, self.base_netloc, self.base_path):
                continue
            # Filter by ext
            ext = os.path.splitext(urlparse(abs_url).path)[1].lower()
            if ext not in HTML_EXTS:
                continue
            # Regex filters
            if self.include_re and not self.include_re.search(abs_url):
                continue
            if self.exclude_re and self.exclude_re.search(abs_url):
                continue
            self.queue.append(abs_url)

    def write_records(self, page_id: int, url: str, title: str, text: str, html_path: Optional[Path], txt_path: Optional[Path]):
        # Chunking
        chunks = chunk_text(text, self.chunk_size, self.chunk_overlap) if text else []
        if not chunks:
            return 0, 0
        total = len(chunks)
        deduped = 0
        for ci, ck in enumerate(chunks):
            h = hash_text(ck)
            if h in self.chunk_hashes:
                deduped += 1
                continue
            self.chunk_hashes.add(h)
            cid = f"page-{page_id}-c{ci}" if total > 1 else f"page-{page_id}"
            # full
            full_obj = {
                "id": cid, "url": url, "title": title,
                "chunk_index": ci, "chunk_count": total,
                "contents": ck, "hash": h
            }
            self.fw_full.write(json.dumps(full_obj, ensure_ascii=False) + "\n")
            # min
            min_obj = {"id": cid, "contents": ck}
            self.fw_min.write(json.dumps(min_obj, ensure_ascii=False) + "\n")
        # manifest
        chars = len(text or "")
        self.manifest_writer.writerow({
            "id": f"page-{page_id}", "url": url, "title": title,
            "html_path": str(html_path.relative_to(self.out_dir)) if html_path else "",
            "txt_path":  str(txt_path.relative_to(self.out_dir)) if txt_path else "",
            "chars": chars,
        })
        self.fw_mani.flush()
        self.fw_min.flush()
        self.fw_full.flush()
        return total, deduped

    def fetch(self, url: str) -> Optional[str]:
        try:
            resp = self.session.get(url, timeout=getattr(self.session, "request_timeout", 15.0))
            if 200 <= resp.status_code < 300:
                return resp.text
            self.logger.warning(f"[{resp.status_code}] {url}")
        except Exception as e:
            self.logger.warning(f"[FETCH ERROR] {url} -> {e}")
        return None

    def write_stats(self):
        stats_path = self.out_dir / "stats.json"
        with stats_path.open("w", encoding="utf-8") as fw:
            json.dump(asdict(self.stats), fw, ensure_ascii=False, indent=2)

    def close_writers(self):
        try:
            self.fw_full.close()
        except Exception:
            pass
        try:
            self.fw_min.close()
        except Exception:
            pass
        try:
            self.fw_mani.close()
        except Exception:
            pass

    def run(self):
        start = time.time()
        self.logger.info(f"Start crawl: base={self.base_url}  out={self.out_dir}")
        while self.queue and len(self.visited) < self.max_pages:
            url = self.queue.popleft()
            if url in self.visited:
                continue
            if not self.can_fetch(url):
                self.logger.info(f"[ROBOTS BLOCKED] {url}")
                self.stats.pages_skipped += 1
                continue
            self.visited.add(url)
            self.stats.queue_peek = max(self.stats.queue_peek, len(self.queue))

            self.logger.info(f"[{len(self.visited)}/{self.max_pages}] Fetch #{self.page_id}: {url} (queue={len(self.queue)})")
            html = self.fetch(url)
            if html is None:
                self.stats.pages_failed += 1
                time.sleep(self.delay)
                continue

            # Save raw HTML / extract text & title / save text
            rel_html = url_to_relpath(url, self.base_path)
            html_path = (self.out_html / rel_html).resolve()
            html_path.parent.mkdir(parents=True, exist_ok=True)
            txt_path = (self.out_txt / Path(rel_html).with_suffix(".txt")).resolve()
            txt_path.parent.mkdir(parents=True, exist_ok=True)

            text, title = extract_text_and_title(html)

            if self.save_html:
                try:
                    html_path.write_text(html, encoding="utf-8", errors="ignore")
                except Exception as e:
                    self.logger.warning(f"[SAVE HTML ERROR] {html_path}: {e}")
            if self.save_text:
                try:
                    txt_path.write_text(text, encoding="utf-8", errors="ignore")
                except Exception as e:
                    self.logger.warning(f"[SAVE TXT ERROR] {txt_path}: {e}")

            # Write JSONL/manifest
            total, deduped = self.write_records(self.page_id, url, title, text, html_path if self.save_html else None, txt_path if self.save_text else None)
            self.stats.pages_fetched += 1
            self.stats.chunks_written += total
            self.stats.chunks_deduped += deduped

            # Enqueue links
            self.enqueue_links(html, url)
            self.page_id += 1

            # Heartbeat
            if self.stats.pages_fetched % self.heartbeat_every == 0:
                elapsed = time.time() - start
                self.logger.info(f"[HEARTBEAT] pages={self.stats.pages_fetched} failed={self.stats.pages_failed} "
                                 f"chunks={self.stats.chunks_written} deduped={self.stats.chunks_deduped} "
                                 f"queue~{len(self.queue)} elapsed={elapsed:.1f}s")

            time.sleep(self.delay)

        self.write_stats()
        self.close_writers()
        elapsed = time.time() - start
        self.logger.info(f"[DONE] pages_fetched={self.stats.pages_fetched}, failed={self.stats.pages_failed}, "
                         f"chunks={self.stats.chunks_written} (deduped {self.stats.chunks_deduped}), "
                         f"queue_peak={self.stats.queue_peek}, took={elapsed:.1f}s")

# ---------------------- CLI ----------------------

def setup_logger(log_level: str, log_file: Optional[str], out_dir: Path) -> logging.Logger:
    logger = logging.getLogger("crawler")
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    if log_file is None:
        log_file = str((out_dir / "crawl.log").resolve())
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger

def main():
    ap = argparse.ArgumentParser(description="Crawl a site into FlashRAG-ready JSONL (robust version).")
    ap.add_argument("--base-url", default="https://robotwin-platform.github.io/doc/", help="Base URL to crawl (e.g., https://robotwin-platform.github.io/doc/)")
    ap.add_argument("--out-dir", default="./corpus_out", help="Output directory")
    ap.add_argument("--max-pages", type=int, default=2000000, help="Max number of pages to crawl")
    ap.add_argument("--delay", type=float, default=0.5, help="Delay (seconds) between requests")
    ap.add_argument("--timeout", type=float, default=15.0, help="HTTP request timeout (seconds)")
    ap.add_argument("--user-agent", default="FlashRAG-Crawler/1.0 (+1962672280@qq.com)", help="User-Agent string")

    # New robustness options
    ap.add_argument("--respect-robots", action="store_true", help="Respect robots.txt rules")
    ap.add_argument("--use-sitemap", action="store_true", help="Try to seed queue from sitemap.xml under site root")
    ap.add_argument("--resume", action="store_true", help="Append to existing outputs and skip URLs already in manifest.csv")
    ap.add_argument("--include-regex", default="", help="Only crawl URLs matching this regex (applied after base filter)")
    ap.add_argument("--exclude-regex", default="", help="Skip URLs matching this regex")
    ap.add_argument("--chunk-size", type=int, default=0, help="Max characters per chunk (0 = no chunking)")
    ap.add_argument("--chunk-overlap", type=int, default=120, help="Overlap characters between chunks")
    ap.add_argument("--heartbeat-every", type=int, default=10, help="Log a heartbeat every N fetched pages")
    ap.add_argument("--save-html", action="store_true", help="Save raw HTML files")
    ap.add_argument("--save-text", action="store_true", help="Save cleaned plaintext files")
    ap.add_argument("--log-level", default="INFO", help="Logging level: DEBUG/INFO/WARNING/ERROR")
    ap.add_argument("--log-file", default="", help="Log file path (default: <out_dir>/crawl.log)")

    args = ap.parse_args()

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    log_file = args.log_file or None
    logger = setup_logger(args.log_level, log_file, out_dir)

    try:
        crawler = Crawler(
            base_url=args.base_url,
            out_dir=out_dir,
            max_pages=args.max_pages,
            delay=args.delay,
            include_re=args.include_regex,
            exclude_re=args.exclude_regex,
            respect_robots=args.respect_robots,
            user_agent=args.user_agent,
            timeout=args.timeout,
            use_sitemap=args.use_sitemap,
            resume=args.resume,
            chunk_size=args.chunk_size,
            chunk_overlap=args.chunk_overlap,
            heartbeat_every=args.heartbeat_every,
            save_html=args.save_html,
            save_text=args.save_text,
            logger=logger,
        )
        crawler.run()
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
