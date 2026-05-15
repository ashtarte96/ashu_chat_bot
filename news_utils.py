"""
news_utils.py — 완전 무료 RSS 기반 크립토 + 매크로 뉴스
소스: CoinDesk / Cointelegraph / Decrypt / The Block / Yahoo Finance + Google News RSS
API KEY 불필요.
"""

import hashlib
import html as _html
import json
import re
import time
from typing import Optional

import requests
from rapidfuzz import fuzz

# ── 상수 ──────────────────────────────────────────────────────────────────────

NEWS_CACHE_FILE = "news_hash_cache.json"
CACHE_TTL_HOURS = 48

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}

# ── RSS 소스 (무료, API 불필요) ───────────────────────────────────────────────

_RSS_SOURCES = {
    "CoinDesk":      "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt":       "https://decrypt.co/feed",
    "The Block":     "https://www.theblock.co/rss.xml",
    "Yahoo Finance": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=BTC-USD&region=US&lang=en-US",
}

# ── Google News RSS 키워드 ────────────────────────────────────────────────────

_GOOGLE_QUERIES = [
    "bitcoin",
    "ethereum",
    "crypto",
    "crypto ETF",
    "SEC crypto",
    "Fed interest rate",
    "CPI inflation",
    "PPI",
    "nasdaq",
    "war geopolitical",
    "AI stocks",
    "stablecoin DeFi",
    "BlackRock bitcoin",
    "crypto hack exploit",
    "Binance Coinbase exchange",
]

# ── 쿼리 필터 맵 (/news btc 등) ──────────────────────────────────────────────

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

# ── 제외 패턴 (광고·낚시만, 좁게 유지) ──────────────────────────────────────

_EXCLUDE_RE = [
    re.compile(p, re.IGNORECASE) for p in [
        r'\bprice\s+prediction\b',
        r'\b(buy|sell)\s+(signal|alert)\b',
        r'\b(top|best)\s+\d+\s+(coins?|cryptos?|tokens?)\b',
        r'\bmust[\s-]buy\b',
        r'\bpassive\s+income\b',
        r'\b\d+[xX]\s+(returns?|gains?|profits?)\b',
        r'\bclick\s+here\b',
    ]
]

# ── 소스 보너스 ───────────────────────────────────────────────────────────────

_SOURCE_BONUS = {
    'rss':    50,
    'google':  0,
}

# ── 우선순위 키워드 (3 티어) ──────────────────────────────────────────────────

# 1순위: 크립토 직접 이슈
_CRYPTO_SCORE_KW = [
    'ETF', 'SEC', 'CFTC', 'regulation', 'bill', 'congress', 'senate', 'law',
    'hack', 'exploit', 'breach', 'stolen', 'liquidation', 'whale',
    'BlackRock', 'Fidelity', 'MicroStrategy', 'institutional', 'fund',
    'Binance', 'Coinbase', 'Kraken', 'Upbit', 'Bybit', 'exchange',
    'stablecoin', 'USDT', 'USDC', 'Layer2', 'L2', 'rollup', 'DeFi', 'NFT',
    'bitcoin', 'ethereum', 'crypto', 'blockchain', 'on-chain', 'onchain',
]
# 2순위: 매크로
_MACRO_SCORE_KW = [
    'Fed', 'FOMC', 'Powell', 'Federal Reserve',
    'interest rate', 'rate hike', 'rate cut',
    'CPI', 'PPI', 'inflation', 'deflation',
    'unemployment', 'jobs', 'payroll', 'GDP',
    'treasury', 'yield', 'bond', 'liquidity', 'recession',
]
# 3순위: 글로벌 시장
_GLOBAL_SCORE_KW = [
    'Nasdaq', 'S&P', 'stock market', 'market crash', 'market rally',
    'DXY', 'dollar', 'currency', 'tariff', 'trade war', 'sanctions',
    'China', 'geopolitical', 'war', 'conflict',
    'Nvidia', 'Tesla', 'AI stocks', 'oil', 'crude', 'energy',
    'risk-on', 'risk-off', 'global markets',
]

_CRYPTO_KW_SET = {k.lower() for k in _CRYPTO_SCORE_KW}
_MACRO_KW_SET  = {k.lower() for k in _MACRO_SCORE_KW}


# ── 캐시 ──────────────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    try:
        with open(NEWS_CACHE_FILE, 'r', encoding='utf-8') as f:
            import json as _json
            return _json.load(f)
    except (FileNotFoundError, Exception):
        return {}


def _save_cache(cache: dict) -> None:
    cutoff = time.time() - CACHE_TTL_HOURS * 3600
    pruned = {k: v for k, v in cache.items() if v.get('ts', 0) > cutoff}
    try:
        with open(NEWS_CACHE_FILE, 'w', encoding='utf-8') as f:
            import json as _json
            _json.dump(pruned, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[RSS DEDUPE] cache save error: {e}")


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


# ── Source 1: Crypto + Finance RSS ───────────────────────────────────────────

def _fetch_rss_sources(hours: int) -> list:
    try:
        import feedparser
    except ImportError:
        print("[RSS FETCH] feedparser not installed")
        return []

    cutoff    = time.time() - hours * 3600
    all_items: list = []
    seen_urls: set  = set()
    per_source: dict = {}

    for source_name, rss_url in _RSS_SOURCES.items():
        count = 0
        try:
            feed = feedparser.parse(rss_url, request_headers=_HEADERS)
            if not feed.entries:
                print(f"[RSS FETCH] {source_name}: 0개 (empty or blocked)")
                per_source[source_name] = 0
                continue

            for entry in feed.entries:
                pt     = entry.get('published_parsed') or entry.get('updated_parsed')
                pub_ts = time.mktime(pt) if pt else time.time()
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
            per_source[source_name] = count

        except Exception as e:
            print(f"[RSS FETCH] {source_name} error: {e}")
            per_source[source_name] = 0

    src_str = ' '.join(f"{k}={v}" for k, v in per_source.items())
    print(f"[RSS FETCH] {src_str} total={len(all_items)} hours={hours}")
    return all_items


# ── Source 2: Google News RSS (키워드별) ──────────────────────────────────────

def _rss_google_fetch(query: str, hours: int) -> list:
    try:
        import feedparser
    except ImportError:
        return []

    q       = requests.utils.quote(query)
    rss_url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    cutoff  = time.time() - hours * 3600

    try:
        feed  = feedparser.parse(rss_url, request_headers=_HEADERS)
        items = []
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
                'source':       (entry.get('source') or {}).get('title', 'Google News'),
                'published_at': entry.get('published', ''),
                'description':  _clean_desc(entry.get('summary', '')),
                '_source':      'google',
            })
        return items
    except Exception as e:
        print(f"[RSS FETCH] Google '{query}' error: {e}")
        return []


def _fetch_google_news(hours: int) -> list:
    seen_urls: set  = set()
    all_items: list = []

    for q in _GOOGLE_QUERIES:
        for item in _rss_google_fetch(q, hours):
            url = item.get('url', '')
            if url and url not in seen_urls:
                seen_urls.add(url)
                all_items.append(item)

    print(f"[RSS FETCH] Google queries={len(_GOOGLE_QUERIES)} total={len(all_items)} hours={hours}")
    return all_items


# ── Merge ─────────────────────────────────────────────────────────────────────

def _merge_all(hours: int) -> list:
    seen_urls: set = set()
    merged: list   = []

    def _add(items: list) -> None:
        for item in items:
            url = item.get('url', '')
            if url and url not in seen_urls:
                seen_urls.add(url)
                merged.append(item)

    rss_items = _fetch_rss_sources(hours)
    gn_items  = _fetch_google_news(hours)

    _add(rss_items)
    _add(gn_items)

    print(f"[RSS MERGE] rss={len(rss_items)} google={len(gn_items)} merged_total={len(merged)}")
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
    filtered      = []
    removed_count = 0
    for item in items:
        title = item.get('title', '')
        if not title.strip():
            continue
        if any(p.search(title) for p in _EXCLUDE_RE):
            removed_count += 1
            continue
        filtered.append(item)

    crypto_n = sum(1 for i in filtered if _category(i) == 'crypto')
    macro_n  = sum(1 for i in filtered if _category(i) == 'macro')
    global_n = len(filtered) - crypto_n - macro_n
    print(
        f"[RSS DEDUPE] filter: raw={len(items)} → after={len(filtered)} "
        f"removed={removed_count} "
        f"(crypto={crypto_n} macro={macro_n} global={global_n})"
    )
    return filtered


# ── 중복 제거 ─────────────────────────────────────────────────────────────────

def _dedupe_within(items: list) -> list:
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
    print(f"[RSS DEDUPE] dedupe: before={len(items)} → after={len(result)}")
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
    print(f"[BUILD_BRIEFING START] items={len(items)} period={period}")

    if period == 'morning':
        header = "🐰 아슈 특파원 아침 출동"
    elif period == 'test':
        header = "🐰 아슈 특파원 테스트 출동"
    else:
        header = "🐰 아슈 특파원 저녁 출동"

    if len(items) == 0:
        print("[EMPTY BRANCH TRIGGERED]")
        return (
            f"{header}\n\n"
            "🐰 아슈 특파원 출동!\n"
            "오늘은 큰 이슈가 없었슈 😴"
        )

    message = f"{header}\n\n"

    for idx, item in enumerate(items):
        title_ko = (item.get('_title_ko') or item.get('title') or '').strip()
        summary  = (item.get('_summary') or '').strip()
        url      = (item.get('url')      or '').strip()
        print(f"[ITEM {idx}] title_ko={title_ko[:50]!r} summary_len={len(summary)} url={'Y' if url else 'N'}")

        if not title_ko:
            print(f"[ITEM {idx}] SKIP — title empty")
            continue

        # HTML escape (제목·요약 텍스트만, URL은 href 속성으로 별도 처리)
        safe_title   = _html.escape(title_ko)
        safe_summary = _html.escape(summary)
        # href 안의 & → &amp; 처리
        safe_url = url.replace('&', '&amp;')

        if url:
            message += f'📰 <a href="{safe_url}">{safe_title}</a>\n'
        else:
            message += f'📰 {safe_title}\n'

        if summary:
            message += f'➡️ {safe_summary}\n\n'
        else:
            message += '\n'

        print(f"[MESSAGE LEN] {len(message)}")

    print("[NEWS MESSAGE]")
    print(message[:3000])
    print(f"[RETURN MESSAGE] len={len(message)}")
    return message


# ── 공개 API ──────────────────────────────────────────────────────────────────

def get_crypto_news(
    hours: int = 12,
    max_items: int = 10,
    query_filter: Optional[str] = None,
    use_cache: bool = True,
) -> list:
    """RSS 수집 → 필터 → 중복제거 → score정렬 → truncate → summary."""
    raw = _merge_all(hours)

    if query_filter:
        qf_lower = query_filter.lower()
        kws      = _QUERY_FILTER_MAP.get(qf_lower, [qf_lower])
        before   = len(raw)
        raw = [
            i for i in raw
            if any(kw in (i['title'] + ' ' + i.get('description', '')).lower()
                   for kw in kws)
        ]
        print(f"[RSS DEDUPE] query_filter='{query_filter}' {before} → {len(raw)}")

    filtered = _filter_news(raw)
    deduped  = _dedupe_within(filtered)

    cache           = _load_cache()
    cache_skipped   = 0
    pre_cache_count = len(deduped)

    # Step 1: score + category (HTTP 없음)
    candidates: list = []
    for item in deduped:
        if use_cache and _is_cached(item, cache):
            cache_skipped += 1
            continue
        item['_score']    = _score(item)
        item['_category'] = _category(item)
        candidates.append(item)

    print(
        f"[RSS DEDUPE] cache: before={pre_cache_count} "
        f"skipped={cache_skipped} → candidates={len(candidates)} use_cache={use_cache}"
    )

    # 캐시로 인해 3개 미만이면 캐시 무시
    if use_cache and len(candidates) < 3 and cache_skipped > 0:
        print(f"[RSS DEDUPE] candidates < 3 → cache bypass")
        candidates = []
        for item in deduped:
            item['_score']    = _score(item)
            item['_category'] = _category(item)
            candidates.append(item)

    # Step 2: 정렬 + 절단 (번역 전에)
    candidates.sort(key=lambda x: x['_score'], reverse=True)
    final = candidates[:max_items]

    # Step 3: 상위 N개만 번역 (제목 + 요약)
    print(f"[RSS FINAL] count={len(final)} (번역 시작)")
    for item in final:
        # 요약 번역
        raw_sum = _make_summary(item)
        item['_summary'] = raw_sum.strip() if raw_sum.strip() else item['title']
        # 제목 번역 (HTML 링크 텍스트용)
        title_ko = _translate_ko(item['title'])
        item['_title_ko'] = title_ko.strip() if title_ko and title_ko.strip() else item['title']

    # 최종 로그
    crypto_n = sum(1 for i in final if i.get('_category') == 'crypto')
    macro_n  = sum(1 for i in final if i.get('_category') == 'macro')
    global_n = len(final) - crypto_n - macro_n
    src_rss  = sum(1 for i in final if i.get('_source') == 'rss')
    src_gn   = sum(1 for i in final if i.get('_source') == 'google')
    print(
        f"[RSS FINAL] final={len(final)} "
        f"src(rss={src_rss} google={src_gn}) "
        f"cat(crypto={crypto_n} macro={macro_n} global={global_n})"
    )
    for i, item in enumerate(final, 1):
        print(
            f"[RSS FINAL] [{i}] score={item['_score']} "
            f"src={item.get('_source','?')} "
            f"summary_len={len(item.get('_summary',''))} "
            f"title={item['title'][:55]}"
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
    """RSS 수집 + 포맷. 부족 시 12h → 24h → 48h 자동 확대."""
    label = f" filter={query_filter}" if query_filter else ""
    print(
        f"[RSS FETCH] get_briefing period={period} hours={hours} "
        f"max={max_items}{label} use_cache={use_cache}"
    )

    items: list = []
    for try_hours in _expand_hours(hours):
        items = get_crypto_news(
            hours=try_hours, max_items=max_items,
            query_filter=query_filter, use_cache=use_cache,
        )
        print(f"[RSS FINAL] try_hours={try_hours} → count={len(items)}")
        if len(items) >= 1:
            if try_hours != hours:
                print(f"[RSS FETCH] hours={hours} 부족 → hours={try_hours} 사용")
            break
        print(f"[RSS FETCH] hours={try_hours} → 0개, 범위 확대")

    print(f"[CALL BUILD_BRIEFING] items={len(items)}")
    return _build_briefing(items, period)


def _expand_hours(hours: int) -> list:
    seq = [hours]
    for h in (24, 48):
        if h not in seq:
            seq.append(h)
    return seq
