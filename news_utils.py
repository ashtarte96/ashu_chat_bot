"""
news_utils.py — 글로벌 크립토 뉴스 수집·필터·중복제거·번역·포맷
"""

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests
from rapidfuzz import fuzz

# ── 환경변수 ──────────────────────────────────────────────────────────────────

GNEWS_API_KEY   = os.getenv("GOOGLE_NEWS_API_KEY", "")
NEWS_CACHE_FILE = "news_hash_cache.json"
CACHE_TTL_HOURS = 48

# ── 수집 키워드 ───────────────────────────────────────────────────────────────

CRYPTO_QUERIES = [
    "crypto bitcoin",
    "ethereum blockchain",
    "crypto altcoin",
    "bitcoin ETF",
    "crypto ETF SEC",
    "SEC crypto regulation",
    "Binance cryptocurrency",
    "Upbit Bybit exchange",
    "crypto hack DeFi exploit",
    "bitcoin institutional investment",
    "crypto Layer2 AI",
    "crypto policy global",
]

# 특정 티커/키워드 필터 매핑
_QUERY_FILTER_MAP: dict[str, list[str]] = {
    'btc':      ['bitcoin', 'btc'],
    'bitcoin':  ['bitcoin', 'btc'],
    'eth':      ['ethereum', 'eth'],
    'ethereum': ['ethereum', 'eth'],
    'sol':      ['solana', 'sol'],
    'bnb':      ['bnb', 'binance'],
    'xrp':      ['xrp', 'ripple'],
    'etf':      ['etf'],
    'sec':      ['sec', 'regulation', 'regulatory'],
    'defi':     ['defi', 'decentralized finance'],
    'hack':     ['hack', 'exploit', 'breach', 'stolen'],
}

# ── 제외 패턴 (낚시·광고·단순 예측) ─────────────────────────────────────────

_EXCLUDE_RE = [
    re.compile(p, re.IGNORECASE) for p in [
        r'\b(will|could|might)\s+(reach|hit|pump|moon|surge|rally|skyrocket)\b.*\$[\d,]+',
        r'\bprice\s+(prediction|forecast|target)\b',
        r'\b(buy|sell)\s+(signal|alert|now|immediately)\b',
        r'\b(top|best)\s+\d+\s+(coins?|cryptos?|tokens?|altcoins?)\b',
        r'\bmust[\s-](buy|watch|own|have)\b',
        r'\b(passive\s+income|get\s+rich|financial\s+freedom)\b',
        r'\bhow\s+to\s+(make|earn)\b',
        r'\b\d+[xX]\s+(returns?|gains?|profits?)\b',
        r'\b(shiba?\s+inu|dogecoin|doge)\s+(price|prediction|surge|pump)\b',
        r'\b(all[\s-]time\s+high|ATH)\s+(incoming|coming|soon)\b',
        r'\bclick\s+(here|now)\b',
    ]
]

# ── 우선순위 키워드 (앞 = 높은 가중치) ───────────────────────────────────────

_PRIORITY_KW = [
    'ETF', 'SEC', 'CFTC', 'regulation', 'law', 'bill', 'congress', 'senate',
    'hack', 'exploit', 'breach', 'stolen', 'vulnerability',
    'BlackRock', 'Fidelity', 'MicroStrategy', 'institutional',
    'Binance', 'Coinbase', 'Kraken', 'Upbit', 'Bybit', 'exchange',
    'Layer2', 'L2', 'lightning', 'rollup', 'zkEVM',
    'AI', 'DeFi', 'NFT', 'stablecoin',
    'bitcoin', 'ethereum',
    'on-chain', 'onchain', 'blockchain',
    'global', 'policy', 'government',
]


# ── 캐시 ──────────────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    try:
        with open(NEWS_CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict) -> None:
    cutoff = time.time() - CACHE_TTL_HOURS * 3600
    pruned = {k: v for k, v in cache.items() if v.get('ts', 0) > cutoff}
    try:
        with open(NEWS_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(pruned, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[NEWS DEDUPE] cache save error: {e}")


def _url_key(url: str) -> str:
    return hashlib.md5(url.encode('utf-8')).hexdigest()


def _is_cached(item: dict, cache: dict) -> bool:
    if _url_key(item['url']) in cache:
        return True
    title = item['title']
    for v in cache.values():
        if fuzz.ratio(title, v.get('title', '')) >= 80:
            return True
    return False


def _add_to_cache(item: dict, cache: dict) -> None:
    cache[_url_key(item['url'])] = {
        'title': item['title'],
        'url':   item['url'],
        'ts':    time.time(),
    }


# ── 수집 ──────────────────────────────────────────────────────────────────────

def _gnews_fetch(query: str, hours: int) -> list:
    from_dt  = datetime.now(timezone.utc) - timedelta(hours=hours)
    from_str = from_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
    url = (
        f"https://gnews.io/api/v4/search"
        f"?q={requests.utils.quote(query)}"
        f"&token={GNEWS_API_KEY}"
        f"&lang=en&max=10&sortby=publishedAt"
        f"&from={from_str}"
    )
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            print(f"[NEWS FETCH] GNews {r.status_code} for '{query}': {r.text[:150]}")
            return []
        data = r.json()
        items = []
        for a in data.get('articles', []):
            title = (a.get('title') or '').strip()
            url_  = a.get('url', '')
            if not title or not url_:
                continue
            items.append({
                'title':       title,
                'url':         url_,
                'source':      (a.get('source') or {}).get('name', ''),
                'published_at': a.get('publishedAt', ''),
                'description': (a.get('description') or '').strip(),
            })
        return items
    except Exception as e:
        print(f"[NEWS FETCH] GNews exception '{query}': {e}")
        return []


def _rss_fetch(query: str, hours: int) -> list:
    try:
        import feedparser
    except ImportError:
        print("[NEWS FETCH] feedparser not installed")
        return []

    q = requests.utils.quote(query)
    rss_url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    try:
        feed   = feedparser.parse(rss_url)
        cutoff = time.time() - hours * 3600
        items  = []
        for entry in feed.entries:
            pt     = entry.get('published_parsed')
            pub_ts = time.mktime(pt) if pt else 0
            if pub_ts < cutoff:
                continue
            title = entry.get('title', '')
            # Google News RSS format: "Article Title - Source Name"
            if ' - ' in title:
                title = title.rsplit(' - ', 1)[0].strip()
            url = entry.get('link', '')
            if not title or not url:
                continue
            desc = _clean_desc(entry.get('summary', ''))
            items.append({
                'title':       title,
                'url':         url,
                'source':      (entry.get('source') or {}).get('title', ''),
                'published_at': entry.get('published', ''),
                'description': desc,
            })
        return items
    except Exception as e:
        print(f"[NEWS FETCH] RSS exception '{query}': {e}")
        return []


def _fetch_all(hours: int) -> list:
    seen_urls: set = set()
    all_items: list = []
    source = 'gnews' if GNEWS_API_KEY else 'rss'
    for q in CRYPTO_QUERIES:
        print(f"[NEWS FETCH] query='{q}' hours={hours} source={source}")
        items = _gnews_fetch(q, hours) if GNEWS_API_KEY else _rss_fetch(q, hours)
        for item in items:
            url = item.get('url', '')
            if url and url not in seen_urls:
                seen_urls.add(url)
                all_items.append(item)
    print(f"[NEWS FETCH] total_raw={len(all_items)}")
    return all_items


# ── 필터 ──────────────────────────────────────────────────────────────────────

def _score(item: dict) -> int:
    text = (item['title'] + ' ' + item.get('description', '')).lower()
    score = 0
    for i, kw in enumerate(_PRIORITY_KW):
        if kw.lower() in text:
            score += len(_PRIORITY_KW) - i
    return score


def _filter_news(items: list) -> list:
    filtered = []
    for item in items:
        title = item.get('title', '')
        if not title.strip():
            continue
        if any(p.search(title) for p in _EXCLUDE_RE):
            continue
        filtered.append(item)
    print(f"[NEWS FILTER] before={len(items)} after={len(filtered)}")
    return filtered


# ── 중복 제거 (배치 내) ───────────────────────────────────────────────────────

def _dedupe_within(items: list) -> list:
    result: list = []
    seen_titles: list = []
    seen_urls: set = set()
    for item in items:
        url   = item['url']
        title = item['title']
        if url in seen_urls:
            continue
        if any(fuzz.ratio(title, t) >= 80 for t in seen_titles):
            continue
        seen_urls.add(url)
        seen_titles.append(title)
        result.append(item)
    print(f"[NEWS DEDUPE] within_batch: {len(items)} → {len(result)}")
    return result


# ── 번역 ──────────────────────────────────────────────────────────────────────

def _translate_ko(text: str) -> str:
    if not text:
        return text
    try:
        from deep_translator import GoogleTranslator
        translated = GoogleTranslator(source='auto', target='ko').translate(text[:500])
        if not translated:
            return text
        # Strip zero-width and invisible Unicode chars
        translated = re.sub(r'[​‌‍⁠﻿]', '', translated)
        return translated.strip() or text
    except Exception:
        return text


def _clean_desc(raw: str) -> str:
    """HTML 태그·엔티티 제거, Google News RSS source suffix 제거."""
    text = re.sub(r'<[^>]+>', '', raw)
    # &amp;nbsp; → space,  &nbsp; → space, etc.
    text = text.replace('&amp;nbsp;', ' ').replace('&nbsp;', ' ')
    text = text.replace('&amp;amp;', '&').replace('&amp;', '&')
    text = text.replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"')
    # Google News RSS: "Article text&nbsp;&nbsp;Source Name - Date"
    text = re.sub(r'\s{2,}.{0,40}$', '', text)
    return re.sub(r'\s+', ' ', text).strip()


def _make_summary(item: dict) -> str:
    desc = _clean_desc(item.get('description', ''))
    if not desc:
        desc = item.get('title', '')
    # Take first 2 sentences or up to 250 chars
    if len(desc) > 250:
        sentences = re.split(r'(?<=[.!?])\s+', desc)
        desc = ' '.join(sentences[:2])
        if len(desc) > 250:
            desc = desc[:247] + '...'
    return _translate_ko(desc)


# ── 포맷 ──────────────────────────────────────────────────────────────────────

def _build_briefing(items: list, period: str) -> str:
    if period == 'morning':
        header = "🐰 아슈 특파원 아침 출동"
    elif period == 'test':
        header = "🐰 아슈 특파원 테스트 출동"
    else:
        header = "🐰 아슈 특파원 저녁 출동"

    if not items:
        return (
            f"{header}\n\n"
            "🐰 아슈 특파원 출동!\n"
            "오늘은 큰 이슈가 없었쥐 😴"
        )

    lines = [header, ""]
    for item in items:
        lines.append(f"📰 {item['title']}")
        lines.append(f"➡️ {item['_summary']}")
        lines.append("")
        lines.append(f"🔗 링크:")
        lines.append(item['url'])
        lines.append("")

    return '\n'.join(lines).rstrip()


# ── 공개 API ──────────────────────────────────────────────────────────────────

def get_crypto_news(
    hours: int = 12,
    max_items: int = 10,
    query_filter: Optional[str] = None,
    use_cache: bool = True,
) -> list:
    """수집 → 필터 → 중복제거(캐시 포함) → 점수 정렬 → max_items 반환."""
    raw = _fetch_all(hours)

    if query_filter:
        qf_lower = query_filter.lower()
        kws = _QUERY_FILTER_MAP.get(qf_lower, [qf_lower])
        raw = [
            i for i in raw
            if any(kw in (i['title'] + ' ' + i.get('description', '')).lower()
                   for kw in kws)
        ]

    filtered = _filter_news(raw)
    deduped  = _dedupe_within(filtered)

    cache = _load_cache()
    final = []
    for item in deduped:
        if use_cache and _is_cached(item, cache):
            continue
        item['_score']   = _score(item)
        item['_summary'] = _make_summary(item)
        final.append(item)

    final.sort(key=lambda x: x['_score'], reverse=True)
    final = final[:max_items]

    print(f"[NEWS DEDUPE] final_after_cache={len(final)} use_cache={use_cache}")

    if use_cache:
        for item in final:
            _add_to_cache(item, cache)
        _save_cache(cache)

    return final


def get_briefing(
    hours: int,
    period: str,
    query_filter: Optional[str] = None,
    use_cache: bool = True,
    max_items: int = 10,
) -> str:
    """뉴스 수집 + 포맷 합성 → Telegram 전송용 문자열 반환."""
    label = f" filter={query_filter}" if query_filter else ""
    print(f"[NEWS TEST] get_briefing period={period} hours={hours} max={max_items}{label}")
    items = get_crypto_news(
        hours=hours,
        max_items=max_items,
        query_filter=query_filter,
        use_cache=use_cache,
    )
    return _build_briefing(items, period)
