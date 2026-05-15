"""
news_utils.py — 멀티 소스 크립토 + 매크로 뉴스
소스 우선순위: CryptoPanic > Crypto RSS > Google News(매크로)
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

GNEWS_API_KEY       = os.getenv("GOOGLE_NEWS_API_KEY", "")
CRYPTOPANIC_API_KEY = os.getenv("CRYPTOPANIC_API_KEY", "")
NEWS_CACHE_FILE     = "news_hash_cache.json"
CACHE_TTL_HOURS     = 48

# ── CryptoPanic 설정 ──────────────────────────────────────────────────────────

_CP_BASE  = "https://cryptopanic.com/api/v1/posts/"
_CP_COINS = "BTC,ETH,BNB,SOL,XRP,ADA,AVAX,LINK,DOT"

# ── Crypto RSS 소스 ───────────────────────────────────────────────────────────

_RSS_SOURCES = {
    "CoinDesk":      "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt":       "https://decrypt.co/feed",
    "The Block":     "https://www.theblock.co/rss.xml",
}

# ── Google News 쿼리 ──────────────────────────────────────────────────────────

_CRYPTO_QUERIES = [
    "crypto bitcoin",
    "ethereum blockchain",
    "bitcoin ETF",
    "crypto ETF SEC regulation",
    "Binance Coinbase Upbit Bybit exchange",
    "crypto hack DeFi exploit",
    "bitcoin institutional investment BlackRock",
    "crypto Layer2 AI stablecoin",
    "altcoin XRP Solana BNB",
    "crypto law bill congress",
]

_MACRO_QUERIES = [
    "fed fomc interest rate decision",
    "inflation CPI PPI data US",
    "powell federal reserve speech",
    "nasdaq S&P500 stock market",
    "treasury yield bond market crash",
    "dollar DXY index strength",
    "china economy growth recession",
    "war geopolitical risk conflict",
    "tariffs trade war US China",
    "nvidia AI stocks market",
    "oil crude energy price",
    "global markets liquidity risk",
    "unemployment jobs recession economy",
]

# ── 필터 맵 (/news btc 등) ────────────────────────────────────────────────────

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
    'fed':      ['fed', 'fomc', 'powell', 'interest rate'],
    'macro':    ['fed', 'fomc', 'cpi', 'ppi', 'inflation', 'nasdaq', 'recession'],
    'defi':     ['defi', 'decentralized finance'],
    'hack':     ['hack', 'exploit', 'breach', 'stolen'],
}

# ── 제외 패턴 ─────────────────────────────────────────────────────────────────

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
        r'\bclick\s+(here|now)\b',
    ]
]

# ── 소스 보너스 (점수에 더해짐) ───────────────────────────────────────────────

_SOURCE_BONUS = {
    'cryptopanic': 100,
    'rss':          50,
    'google':        0,
}

# ── 3-tier 우선순위 키워드 ────────────────────────────────────────────────────

_CRYPTO_SCORE_KW = [
    'ETF', 'SEC', 'CFTC', 'regulation', 'bill', 'congress', 'senate', 'law',
    'hack', 'exploit', 'breach', 'stolen', 'liquidation', 'whale',
    'BlackRock', 'Fidelity', 'MicroStrategy', 'institutional', 'fund',
    'Binance', 'Coinbase', 'Kraken', 'Upbit', 'Bybit', 'exchange',
    'stablecoin', 'USDT', 'USDC',
    'Layer2', 'L2', 'rollup', 'DeFi', 'NFT',
    'bitcoin', 'ethereum', 'crypto', 'blockchain',
    'on-chain', 'onchain',
]

_MACRO_SCORE_KW = [
    'Fed', 'FOMC', 'Powell', 'Federal Reserve',
    'interest rate', 'rate hike', 'rate cut',
    'CPI', 'PPI', 'inflation', 'deflation',
    'unemployment', 'jobs', 'payroll', 'GDP',
    'treasury', 'yield', 'bond',
    'liquidity', 'recession', 'stagflation',
]

_GLOBAL_SCORE_KW = [
    'Nasdaq', 'S&P', 'stock market', 'market crash', 'market rally',
    'DXY', 'dollar', 'currency',
    'tariff', 'trade war', 'sanctions',
    'China', 'geopolitical', 'war', 'conflict',
    'Nvidia', 'Tesla', 'AI stocks',
    'oil', 'crude', 'energy',
    'risk-on', 'risk-off', 'safe haven',
    'global markets',
]

_CRYPTO_KW_SET = {k.lower() for k in _CRYPTO_SCORE_KW}
_MACRO_KW_SET  = {k.lower() for k in _MACRO_SCORE_KW}


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


# ── Source 1: CryptoPanic API ─────────────────────────────────────────────────

def _fetch_cryptopanic(hours: int) -> list:
    """CryptoPanic API → 크립토 핵심 뉴스 수집."""
    if not CRYPTOPANIC_API_KEY:
        print("[CRYPTOPANIC] API key not set, skipping")
        return []

    cutoff  = time.time() - hours * 3600
    items: list = []
    next_url: str = _CP_BASE
    params: dict = {
        'auth_token': CRYPTOPANIC_API_KEY,
        'currencies': _CP_COINS,
        'kind':       'news',
        'regions':    'en',
        'filter':     'rising',
    }
    pages = 0

    while next_url and pages < 3:
        try:
            r = requests.get(
                next_url,
                params=params if pages == 0 else None,
                timeout=15,
            )
            pages += 1
            if r.status_code == 403:
                print(f"[CRYPTOPANIC] 403 Forbidden – check CRYPTOPANIC_API_KEY")
                break
            if r.status_code != 200:
                print(f"[CRYPTOPANIC] HTTP {r.status_code}: {r.text[:120]}")
                break

            data    = r.json()
            results = data.get('results', [])
            stop    = False

            for post in results:
                pub_str = post.get('published_at', '')
                try:
                    pub_dt = datetime.fromisoformat(pub_str.replace('Z', '+00:00'))
                    pub_ts = pub_dt.timestamp()
                except Exception:
                    pub_ts = 0

                if pub_ts < cutoff:
                    stop = True
                    continue

                title = (post.get('title') or '').strip()
                link  = post.get('url', '')
                if not title or not link:
                    continue

                src_name = (post.get('source') or {}).get('title', 'CryptoPanic')
                items.append({
                    'title':        title,
                    'url':          link,
                    'source':       src_name,
                    'published_at': pub_str,
                    'description':  title,
                    '_source':      'cryptopanic',
                })

            next_url = '' if stop else (data.get('next') or '')
            params   = None  # next URL already includes all params

        except Exception as e:
            print(f"[CRYPTOPANIC] exception page={pages}: {e}")
            break

    print(f"[CRYPTOPANIC] fetched={len(items)} hours={hours} pages_fetched={pages}")
    return items


# ── Source 2: Crypto RSS Feeds ────────────────────────────────────────────────

def _fetch_rss_sources(hours: int) -> list:
    """CoinDesk · Cointelegraph · Decrypt · The Block RSS 수집."""
    try:
        import feedparser
    except ImportError:
        print("[RSS] feedparser not installed — skipping crypto RSS")
        return []

    cutoff    = time.time() - hours * 3600
    all_items: list = []
    seen_urls: set  = set()

    for source_name, rss_url in _RSS_SOURCES.items():
        try:
            feed  = feedparser.parse(rss_url)
            count = 0
            for entry in feed.entries:
                pt     = entry.get('published_parsed') or entry.get('updated_parsed')
                pub_ts = time.mktime(pt) if pt else 0
                if pub_ts < cutoff:
                    continue
                title = (entry.get('title') or '').strip()
                url   = entry.get('link', '')
                if not title or not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                desc = _clean_desc(
                    entry.get('summary') or entry.get('description') or ''
                )
                all_items.append({
                    'title':        title,
                    'url':          url,
                    'source':       source_name,
                    'published_at': entry.get('published', ''),
                    'description':  desc,
                    '_source':      'rss',
                })
                count += 1
            print(f"[RSS] {source_name}: {count}개 (hours={hours})")
        except Exception as e:
            print(f"[RSS] {source_name} error: {e}")

    print(f"[RSS] total={len(all_items)}")
    return all_items


# ── Source 3: Google News (크립토 + 매크로) ───────────────────────────────────

def _gnews_api_fetch(query: str, hours: int) -> list:
    from_dt  = datetime.now(timezone.utc) - timedelta(hours=hours)
    from_str = from_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
    url = (
        "https://gnews.io/api/v4/search"
        f"?q={requests.utils.quote(query)}"
        f"&token={GNEWS_API_KEY}"
        f"&lang=en&max=10&sortby=publishedAt&from={from_str}"
    )
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            print(f"[GOOGLE NEWS] GNews API {r.status_code} for '{query}'")
            return []
        items = []
        for a in r.json().get('articles', []):
            title = (a.get('title') or '').strip()
            link  = a.get('url', '')
            if not title or not link:
                continue
            items.append({
                'title':        title,
                'url':          link,
                'source':       (a.get('source') or {}).get('name', ''),
                'published_at': a.get('publishedAt', ''),
                'description':  (a.get('description') or '').strip(),
                '_source':      'google',
            })
        return items
    except Exception as e:
        print(f"[GOOGLE NEWS] GNews API exception '{query}': {e}")
        return []


def _rss_google_fetch(query: str, hours: int) -> list:
    try:
        import feedparser
    except ImportError:
        return []
    q       = requests.utils.quote(query)
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
            if ' - ' in title:
                title = title.rsplit(' - ', 1)[0].strip()
            url = entry.get('link', '')
            if not title or not url:
                continue
            items.append({
                'title':        title,
                'url':          url,
                'source':       (entry.get('source') or {}).get('title', ''),
                'published_at': entry.get('published', ''),
                'description':  _clean_desc(entry.get('summary', '')),
                '_source':      'google',
            })
        return items
    except Exception as e:
        print(f"[GOOGLE NEWS] RSS exception '{query}': {e}")
        return []


def _fetch_google_news(hours: int) -> list:
    """Google News에서 크립토 + 매크로 이슈 수집."""
    seen_urls: set  = set()
    all_items: list = []
    use_api = bool(GNEWS_API_KEY)

    for q in _CRYPTO_QUERIES:
        print(f"[GOOGLE NEWS] crypto query='{q}'")
        batch = _gnews_api_fetch(q, hours) if use_api else _rss_google_fetch(q, hours)
        for item in batch:
            url = item.get('url', '')
            if url and url not in seen_urls:
                seen_urls.add(url)
                all_items.append(item)

    for q in _MACRO_QUERIES:
        print(f"[GOOGLE NEWS] macro query='{q}'")
        batch = _gnews_api_fetch(q, hours) if use_api else _rss_google_fetch(q, hours)
        for item in batch:
            url = item.get('url', '')
            if url and url not in seen_urls:
                seen_urls.add(url)
                all_items.append(item)

    print(f"[GOOGLE NEWS] total={len(all_items)} hours={hours}")
    return all_items


# ── Merge ─────────────────────────────────────────────────────────────────────

def _merge_all(hours: int) -> list:
    """CryptoPanic + RSS + Google News 병합 (소스 우선순위 순서 유지, URL 중복 제거)."""
    seen_urls: set = set()
    merged: list   = []

    def _add(items: list) -> None:
        for item in items:
            url = item.get('url', '')
            if url and url not in seen_urls:
                seen_urls.add(url)
                merged.append(item)

    cp_items  = _fetch_cryptopanic(hours)
    rss_items = _fetch_rss_sources(hours)
    gn_items  = _fetch_google_news(hours)

    _add(cp_items)
    _add(rss_items)
    _add(gn_items)

    print(
        f"[NEWS MERGE] cryptopanic={len(cp_items)} rss={len(rss_items)} "
        f"google={len(gn_items)} merged_total={len(merged)}"
    )
    return merged


# ── 필터 ──────────────────────────────────────────────────────────────────────

def _category(item: dict) -> str:
    text = (item['title'] + ' ' + item.get('description', '')).lower()
    if any(kw in text for kw in _CRYPTO_KW_SET):
        return 'crypto'
    if any(kw in text for kw in _MACRO_KW_SET):
        return 'macro'
    return 'global'


def _score(item: dict) -> int:
    """소스 보너스(CryptoPanic+100, RSS+50) + 3-tier 키워드 가중치."""
    base = _SOURCE_BONUS.get(item.get('_source', 'google'), 0)
    text = (item['title'] + ' ' + item.get('description', '')).lower()
    for i, kw in enumerate(_CRYPTO_SCORE_KW):
        if kw.lower() in text:
            base += 50 - i
    for i, kw in enumerate(_MACRO_SCORE_KW):
        if kw.lower() in text:
            base += 30 - i
    for i, kw in enumerate(_GLOBAL_SCORE_KW):
        if kw.lower() in text:
            base += 15 - i
    return max(base, 0)


def _filter_news(items: list) -> list:
    filtered = []
    for item in items:
        title = item.get('title', '')
        if not title.strip():
            continue
        if any(p.search(title) for p in _EXCLUDE_RE):
            continue
        filtered.append(item)
    crypto_n = sum(1 for i in filtered if _category(i) == 'crypto')
    macro_n  = sum(1 for i in filtered if _category(i) == 'macro')
    global_n = len(filtered) - crypto_n - macro_n
    print(
        f"[FILTER] before={len(items)} after={len(filtered)} "
        f"(crypto={crypto_n} macro={macro_n} global={global_n})"
    )
    return filtered


# ── 중복 제거 ─────────────────────────────────────────────────────────────────

def _dedupe_within(items: list) -> list:
    """배치 내 URL 중복 + 제목 유사도(rapidfuzz ≥ 80%) 중복 제거."""
    result: list      = []
    seen_titles: list = []
    seen_urls: set    = set()
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
        translated = re.sub(r'[​‌‍⁠﻿]', '', translated)
        return translated.strip() or text
    except Exception:
        return text


def _clean_desc(raw: str) -> str:
    """HTML 태그·엔티티 제거, Google News RSS source suffix 제거."""
    text = re.sub(r'<[^>]+>', '', raw)
    text = text.replace('&amp;nbsp;', ' ').replace('&nbsp;', ' ')
    text = text.replace('&amp;amp;', '&').replace('&amp;', '&')
    text = text.replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"')
    text = re.sub(r'\s{2,}.{0,40}$', '', text)
    return re.sub(r'\s+', ' ', text).strip()


def _make_summary(item: dict) -> str:
    desc = _clean_desc(item.get('description', ''))
    if not desc or desc == item.get('title', ''):
        desc = item.get('title', '')
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
            "오늘은 큰 이슈가 없었슈 😴"
        )

    lines = [header, ""]
    for item in items:
        lines.append(f"📰 {item['title']}")
        lines.append(f"➡️ {item['_summary']}")
        lines.append("")
        lines.append("🔗 링크:")
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
    """멀티 소스 수집 → 필터 → 중복제거 → 점수 정렬 → max_items 반환."""
    raw = _merge_all(hours)

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
        item['_score']    = _score(item)
        item['_category'] = _category(item)
        item['_summary']  = _make_summary(item)
        final.append(item)

    final.sort(key=lambda x: x['_score'], reverse=True)
    final = final[:max_items]

    crypto_n = sum(1 for i in final if i.get('_category') == 'crypto')
    macro_n  = sum(1 for i in final if i.get('_category') == 'macro')
    global_n = len(final) - crypto_n - macro_n
    src_cp  = sum(1 for i in final if i.get('_source') == 'cryptopanic')
    src_rss = sum(1 for i in final if i.get('_source') == 'rss')
    src_gn  = sum(1 for i in final if i.get('_source') == 'google')
    print(
        f"[NEWS DEDUPE] final={len(final)} use_cache={use_cache} "
        f"src(cp={src_cp} rss={src_rss} google={src_gn}) "
        f"cat(crypto={crypto_n} macro={macro_n} global={global_n})"
    )

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
    """뉴스 수집 + 포맷 합성.
    5개 미만 → 24h → 48h 자동 확대."""
    label = f" filter={query_filter}" if query_filter else ""
    print(
        f"[NEWS TEST] get_briefing period={period} hours={hours} "
        f"max={max_items}{label}"
    )

    items = get_crypto_news(
        hours=hours, max_items=max_items,
        query_filter=query_filter, use_cache=use_cache,
    )

    if len(items) < 5 and hours < 24:
        print(f"[NEWS FETCH] {len(items)}개 부족 → 24h로 재시도")
        items = get_crypto_news(
            hours=24, max_items=max_items,
            query_filter=query_filter, use_cache=use_cache,
        )

    if len(items) < 5:
        print(f"[NEWS FETCH] {len(items)}개 부족 → 48h로 재시도")
        items = get_crypto_news(
            hours=48, max_items=max_items,
            query_filter=query_filter, use_cache=use_cache,
        )

    return _build_briefing(items, period)
