"""
calendar_utils.py — 글로벌 경제 캘린더
소스 1: ForexFactory XML  (primary)
소스 2: Investing.com AJAX (secondary, best-effort)
날짜 기준: Asia/Seoul (KST). ET 시간 → KST 자동 변환 (DST 대응).
"""

import html as _html
from datetime import datetime, date as _date, timedelta
from xml.etree import ElementTree as ET

import pytz
import requests

# ── 상수 ──────────────────────────────────────────────────────────────────────

_FF_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.xml",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.xml",   # 404면 skip
]

_INVESTING_URL = (
    "https://www.investing.com/economic-calendar/Service/getCalendarFilteredData"
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/xml,text/xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.forexfactory.com/",
}
_TIMEOUT = 15

_ET_TZ  = pytz.timezone('America/New_York')
_KST_TZ = pytz.timezone('Asia/Seoul')

_NO_TIME = frozenset({'all day', 'tentative', 'day 1', 'day 2', 'weekend', ''})

# ── 국가 정보 ─────────────────────────────────────────────────────────────────

_COUNTRY_INFO: dict[str, tuple[str, str]] = {
    'USD': ('🇺🇸', '미국'),
    'KRW': ('🇰🇷', '한국'),
    'CNY': ('🇨🇳', '중국'),
    'JPY': ('🇯🇵', '일본'),
    'EUR': ('🇪🇺', '유럽'),
    'GBP': ('🇬🇧', '영국'),
    'CAD': ('🇨🇦', '캐나다'),
    'AUD': ('🇦🇺', '호주'),
    'NZD': ('🇳🇿', '뉴질랜드'),
    'CHF': ('🇨🇭', '스위스'),
}
_PRIORITY_CURRENCIES = ['USD', 'KRW', 'CNY', 'JPY', 'EUR', 'GBP']
_OTHER_FLAG, _OTHER_NAME = '🌍', '기타'

# Investing.com 국가 ID (내부 API 파라미터)
_IC_COUNTRY_IDS = ["5", "4", "35", "37", "17", "72"]  # US, UK, JP, CN, DE, EMU

# ── 중요도 ────────────────────────────────────────────────────────────────────

# Low 포함: 별 1개 이상 전체 표시
_SHOW_IMPACT  = {'High', 'Medium', 'Low'}
_IMPACT_EMOJI = {'High': '🔥', 'Medium': '⭐', 'Low': ''}
_IMPACT_ORDER = {'High': 0, 'Medium': 1, 'Low': 2}

# ── 이벤트 제목 한국어 맵 ─────────────────────────────────────────────────────

_EVENT_KO: list[tuple[str, str]] = [
    # 미국 — 고용
    ('Non-Farm Payrolls',               '비농업 고용지수'),
    ('ADP Non-Farm Employment Change',  'ADP 고용변화'),
    ('JOLTS Job Openings',              'JOLTS 구인건수'),
    ('Initial Jobless Claims',          '초기 실업수당 청구'),
    ('Unemployment Claims',             '실업수당 청구건수'),
    # 미국 — 물가
    ('Core CPI m/m',                    '근원 CPI (전월비)'),
    ('Core CPI y/y',                    '근원 CPI (전년비)'),
    ('CPI m/m',                         'CPI (전월비)'),
    ('CPI y/y',                         'CPI (전년비)'),
    ('Core PPI m/m',                    '근원 PPI (전월비)'),
    ('PPI m/m',                         'PPI (전월비)'),
    ('Core PCE Price Index m/m',        '근원 PCE 물가지수'),
    ('PCE Price Index m/m',             'PCE 물가지수'),
    # 미국 — 연준
    ('FOMC Statement',                  'FOMC 성명'),
    ('Federal Funds Rate',              '연방기금금리 결정'),
    ('FOMC Press Conference',           'FOMC 기자회견'),
    ('FOMC Meeting Minutes',            'FOMC 의사록'),
    ('Powell Speaks',                   '파월 의장 연설'),
    ('Fed Chair',                       '연준 의장 발언'),
    # 미국 — 성장
    ('Prelim GDP q/q',                  'GDP 예비치 (전분기비)'),
    ('GDP q/q',                         'GDP (전분기비)'),
    # 미국 — 소비/제조
    ('Core Retail Sales m/m',           '근원 소매판매 (전월비)'),
    ('Retail Sales m/m',                '소매판매 (전월비)'),
    ('Consumer Confidence',             '소비자신뢰지수'),
    ('Consumer Sentiment',              '소비자심리지수'),
    ('ISM Manufacturing PMI',           '제조업 PMI (ISM)'),
    ('ISM Services PMI',                '서비스업 PMI (ISM)'),
    ('Durable Goods Orders m/m',        '내구재주문 (전월비)'),
    ('Trade Balance',                   '무역수지'),
    # 유럽 / ECB
    ('ECB Main Refinancing Rate',       'ECB 기준금리'),
    ('ECB Monetary Policy Statement',   'ECB 통화정책 성명'),
    ('ECB Press Conference',            'ECB 기자회견'),
    ('Flash CPI y/y',                   'EU CPI 예비치'),
    ('Flash GDP q/q',                   'EU GDP 예비치'),
    # 영국 / BOE
    ('Official Bank Rate',              '영란은행 기준금리'),
    ('MPC Rate Statement',              'BOE 통화정책 성명'),
    # 일본 / BOJ
    ('BOJ Policy Rate',                 'BOJ 정책금리'),
    ('Monetary Policy Statement',       '통화정책 성명'),
    ('Tankan Manufacturing Index',      '단칸 제조업지수'),
    # 중국
    ('Caixin Manufacturing PMI',        'Caixin 제조업 PMI'),
    ('Caixin Services PMI',             'Caixin 서비스업 PMI'),
    ('Non-Manufacturing PMI',           '비제조업 PMI'),
    ('Manufacturing PMI',               '제조업 PMI'),
    # 한국
    ('BOK Interest Rate Decision',      '한국은행 기준금리 결정'),
    ('Exports y/y',                     '수출 (전년비)'),
    ('Imports y/y',                     '수입 (전년비)'),
    # 호주/뉴질랜드/캐나다
    ('Employment Change',               '고용변화'),
    ('Unemployment Rate',               '실업률'),
    ('Claimant Count Change',           '실업수당 신청건수'),
    # 기타
    ('OPEC',                            'OPEC 회의'),
    ('New Home Prices',                 '신규 주택가격'),
]


def _ko_title(title: str) -> str:
    title_lower = title.lower()
    for eng, kor in _EVENT_KO:
        if eng.lower() in title_lower:
            return kor
    return title


# ── 날짜/시간 변환 ────────────────────────────────────────────────────────────

def _parse_ff_date(date_str: str) -> _date | None:
    for fmt in ('%m-%d-%Y', '%Y-%m-%d', '%m/%d/%Y'):
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _et_to_kst(ev_date: _date, time_str: str) -> tuple[datetime | None, str]:
    """
    ForexFactory ET 날짜+시간 → (KST datetime, 'HH:MM').
    All Day / Tentative → (None, '종일').
    pytz 로 DST(EST/EDT) 자동 처리.
    """
    t_norm = (time_str or '').strip().lower()
    if t_norm in _NO_TIME:
        return None, '종일'

    naive_dt: datetime | None = None
    for fmt in ('%I:%M%p', '%I%p'):
        try:
            t = datetime.strptime(t_norm, fmt)
            naive_dt = datetime(ev_date.year, ev_date.month, ev_date.day,
                                t.hour, t.minute, 0)
            break
        except ValueError:
            continue

    if naive_dt is None:
        return None, '미정'

    try:
        et_dt  = _ET_TZ.localize(naive_dt)
        kst_dt = et_dt.astimezone(_KST_TZ)
        return kst_dt, kst_dt.strftime('%H:%M')
    except Exception as exc:
        print(f"[ECONOMIC CALENDAR] timezone 변환 오류: {exc}")
        return None, '미정'


# ── Source 1: ForexFactory XML ────────────────────────────────────────────────

def _fetch_ff_xml(url: str) -> list:
    """단일 ForexFactory XML URL 파싱. 실패·404면 빈 리스트 반환."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        if resp.status_code == 404:
            print(f"[FOREXFACTORY FETCH] 404 skip: {url.split('/')[-1]}")
            return []
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except requests.exceptions.RequestException as exc:
        print(f"[FOREXFACTORY FETCH] HTTP error ({url.split('/')[-1]}): {exc}")
        return []
    except ET.ParseError as exc:
        print(f"[FOREXFACTORY FETCH] XML parse error ({url.split('/')[-1]}): {exc}")
        return []

    events: list = []
    skipped_impact = 0

    for ev in root.findall('event'):
        impact = (ev.findtext('impact') or '').strip()
        if impact not in _SHOW_IMPACT:
            skipped_impact += 1
            continue

        date_str = (ev.findtext('date') or '').strip()
        ev_date  = _parse_ff_date(date_str)
        if not ev_date:
            continue

        title   = (ev.findtext('title')   or '').strip()
        country = (ev.findtext('country') or '').strip().upper()
        if not title or not country:
            continue

        time_str = (ev.findtext('time')     or '').strip()
        forecast = (ev.findtext('forecast') or '').strip()
        previous = (ev.findtext('previous') or '').strip()
        actual   = (ev.findtext('actual')   or '').strip()

        kst_dt, kst_time = _et_to_kst(ev_date, time_str)
        kst_date: _date  = kst_dt.date() if kst_dt else ev_date

        events.append({
            'title':    title,
            'title_ko': _ko_title(title),
            'country':  country,
            'impact':   impact,
            'forecast': forecast,
            'previous': previous,
            'actual':   actual,
            'kst_dt':   kst_dt,
            'kst_time': kst_time,
            'kst_date': kst_date,
            '_src':     'ff',
        })

    print(
        f"[FOREXFACTORY FETCH] {url.split('/')[-1]}: "
        f"included={len(events)} skipped_low_impact={skipped_impact}"
    )
    return events


# ── Source 2: Investing.com AJAX (best-effort) ────────────────────────────────

def _fetch_investing_calendar(from_date: _date, to_date: _date) -> list:
    """
    Investing.com 경제 캘린더 AJAX 엔드포인트 (best-effort).
    응답이 HTML이므로 BeautifulSoup으로 파싱.
    봇 탐지 등으로 실패하면 빈 리스트 반환.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("[INVESTING FETCH] beautifulsoup4 not installed, skip")
        return []

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "X-Requested-With": "XMLHttpRequest",
        "Referer":           "https://www.investing.com/economic-calendar/",
        "Content-Type":      "application/x-www-form-urlencoded",
        "Origin":            "https://www.investing.com",
        "Accept":            "text/html,*/*;q=0.8",
        "Accept-Language":   "en-US,en;q=0.9",
    }
    # timeZone=0 = UTC; 변환은 우리가 직접 수행
    payload = {
        "country[]":      _IC_COUNTRY_IDS,
        "importance[]":   ["1", "2", "3"],          # Low/Medium/High 전부
        "timeZone":       "0",                      # UTC
        "timeFilter":     "timeRemain",
        "currentTab":     "custom",
        "submitFilters":  "1",
        "dateFrom":       from_date.strftime('%Y-%m-%d'),
        "dateTo":         to_date.strftime('%Y-%m-%d'),
        "limit_from":     "0",
    }

    try:
        resp = requests.post(_INVESTING_URL, headers=headers,
                             data=payload, timeout=_TIMEOUT)
        resp.raise_for_status()

        # 응답이 JSON {"data": "<html>..."} 형태인 경우 처리
        try:
            import json as _json
            j = _json.loads(resp.text)
            html_body = j.get('data', '') or resp.text
        except Exception:
            html_body = resp.text

        soup = BeautifulSoup(html_body, 'html.parser')
        rows = soup.find_all('tr', attrs={'data-event-datetime': True})

        _UTC_TZ = pytz.utc
        events  = []
        skipped = 0

        # importance: 별 수 (grayFullBullishIcon 개수)
        _imp_map = {3: 'High', 2: 'Medium', 1: 'Low'}

        for row in rows:
            try:
                dt_str  = row['data-event-datetime']          # "2025-01-17 08:30:00"
                cur_td  = row.find('td', class_='flagCur')
                evt_td  = row.find('td', class_='event')
                fore_td = row.find('td', class_='fore')

                if not cur_td or not evt_td:
                    continue

                currency = cur_td.get_text(strip=True).upper()[-3:]  # "USD"
                title    = evt_td.get_text(strip=True)
                forecast = fore_td.get_text(strip=True) if fore_td else ''

                # 중요도
                icons   = row.find_all('i', class_='grayFullBullishIcon')
                n_stars = len(icons)
                impact  = _imp_map.get(n_stars, 'Low')

                # UTC → KST 변환
                naive   = datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S')
                utc_dt  = _UTC_TZ.localize(naive)
                kst_dt  = utc_dt.astimezone(_KST_TZ)

                events.append({
                    'title':    title,
                    'title_ko': _ko_title(title),
                    'country':  currency,
                    'impact':   impact,
                    'forecast': forecast,
                    'previous': '',
                    'actual':   '',
                    'kst_dt':   kst_dt,
                    'kst_time': kst_dt.strftime('%H:%M'),
                    'kst_date': kst_dt.date(),
                    '_src':     'ic',
                })
            except Exception:
                skipped += 1
                continue

        print(f"[INVESTING FETCH] total={len(events)} skipped={skipped}")
        return events

    except Exception as exc:
        print(f"[INVESTING FETCH] failed (expected if blocked): {exc}")
        return []


# ── 전체 이벤트 fetch ─────────────────────────────────────────────────────────

def _fetch_all_events() -> list:
    """ForexFactory(primary) + Investing.com(secondary) 병합, 중복 제거."""
    all_events: list = []

    # Source 1: ForexFactory (this week + next week)
    ff_total = 0
    for url in _FF_URLS:
        evs = _fetch_ff_xml(url)
        all_events.extend(evs)
        ff_total += len(evs)
    print(f"[FOREXFACTORY FETCH] combined_total={ff_total}")

    # Source 2: Investing.com (best-effort, 5일 범위)
    now_kst    = datetime.now(_KST_TZ)
    from_date  = now_kst.date()
    to_date    = from_date + timedelta(days=4)
    ic_events  = _fetch_investing_calendar(from_date, to_date)

    # IC 이벤트 중 FF에 없는 것만 추가 (url 중복 없으므로 제목+날짜로 판단)
    ff_keys = {(e['title'].lower(), e['kst_date']) for e in all_events}
    for ev in ic_events:
        key = (ev['title'].lower(), ev['kst_date'])
        if key not in ff_keys:
            all_events.append(ev)
            ff_keys.add(key)

    print(f"[ECONOMIC CALENDAR FETCH] merged_total={len(all_events)}")
    return all_events


# ── 날짜별 그룹핑 ─────────────────────────────────────────────────────────────

def _dedupe(events: list) -> list:
    seen: set = set()
    result    = []
    for ev in events:
        key = (ev['country'], ev['title_ko'])
        if key not in seen:
            seen.add(key)
            result.append(ev)
    return result


def fetch_calendar_by_kst_dates(target_dates: list[_date]) -> dict[_date, list]:
    all_events = _fetch_all_events()
    target_set = set(target_dates)

    buckets: dict[_date, list] = {d: [] for d in target_dates}
    for ev in all_events:
        if ev['kst_date'] in target_set:
            buckets[ev['kst_date']].append(ev)

    for d in buckets:
        buckets[d] = _dedupe(buckets[d])

    # GC 필터 로그
    total_after = sum(len(v) for v in buckets.values())
    print(f"[GC FILTER] after_dedup={total_after} for dates={[str(d) for d in target_dates]}")
    return buckets


# ── 정렬 ──────────────────────────────────────────────────────────────────────

def _sort_key(ev: dict) -> tuple:
    kst_dt = ev.get('kst_dt')
    return (
        kst_dt.timestamp() if kst_dt else float('inf'),
        _IMPACT_ORDER.get(ev['impact'], 9),
    )


# ── 포맷 ──────────────────────────────────────────────────────────────────────

def _fmt_line(ev: dict) -> str:
    impact_e = _IMPACT_EMOJI.get(ev['impact'], '')
    title_ko = _html.escape(ev['title_ko'])
    kst_time = ev.get('kst_time', '미정')
    forecast = f" (예상 {_html.escape(ev['forecast'])})" if ev['forecast'] else ""

    if kst_time not in ('종일', '미정'):
        time_part = f"🕘 {kst_time} "
    else:
        time_part = "• "

    imp_part = f"{impact_e} " if impact_e else ""
    return f"{time_part}{imp_part}{title_ko}{forecast}"


def _format_day_section(events: list) -> str:
    """하루치 이벤트를 국가별로 묶어 포맷. 각 국가 내 KST 시간순 정렬."""
    by_country: dict[str, list] = {}
    for ev in events:
        by_country.setdefault(ev['country'], []).append(ev)

    for items in by_country.values():
        items.sort(key=_sort_key)

    section = ""

    for currency in _PRIORITY_CURRENCIES:
        if currency not in by_country:
            continue
        flag, name = _COUNTRY_INFO[currency]
        lines = "\n".join(_fmt_line(ev) for ev in by_country[currency])
        section += f"{flag} <b>{_html.escape(name)}</b>\n"
        section += f"<blockquote>{lines}</blockquote>\n\n"

    others = [
        ev for cur, evs in by_country.items()
        if cur not in _PRIORITY_CURRENCIES
        for ev in evs
        if ev['impact'] in ('High', 'Medium')   # 기타는 Medium+ 만
    ]
    if others:
        others.sort(key=_sort_key)
        lines = "\n".join(_fmt_line(ev) for ev in others)
        section += f"{_OTHER_FLAG} <b>{_html.escape(_OTHER_NAME)}</b>\n"
        section += f"<blockquote>{lines}</blockquote>\n\n"

    return section


# ── 메인 빌더 ─────────────────────────────────────────────────────────────────

def build_calendar_message(is_test: bool = False) -> str:
    """오늘+내일 (KST 기준) 경제 캘린더. 둘 다 비면 +2~+4일까지 자동 확장."""
    header = "🐰 아슈 경제 캘린더 테스트" if is_test else "🐰 아슈 출근길 글로벌 경제 캘린더"

    now_kst      = datetime.now(_KST_TZ)
    today_kst    = now_kst.date()
    tomorrow_kst = today_kst + timedelta(days=1)

    # 오늘/내일 fetch
    buckets = fetch_calendar_by_kst_dates([today_kst, tomorrow_kst])
    today_events    = buckets.get(today_kst,    [])
    tomorrow_events = buckets.get(tomorrow_kst, [])

    print(f"[GC TODAY COUNT]    {len(today_events)}")
    print(f"[GC TOMORROW COUNT] {len(tomorrow_events)}")

    # 오늘+내일 모두 비면 최대 4일 후까지 확장
    extra_label  = ""
    extra_events = []
    if not today_events and not tomorrow_events:
        for delta in range(2, 5):
            extra_date = today_kst + timedelta(days=delta)
            extra_bkt  = fetch_calendar_by_kst_dates([extra_date])
            cands      = extra_bkt.get(extra_date, [])
            if cands:
                extra_events = cands
                extra_label  = extra_date.strftime('%m/%d') + " 일정"
                print(f"[GC FINAL] no today/tomorrow → fallback to +{delta}d ({extra_date})")
                break

    total_final = len(today_events) + len(tomorrow_events) + len(extra_events)
    print(f"[GC FINAL] total_shown={total_final}")

    message = f"<b>{_html.escape(header)}</b>\n\n"

    # 오늘 섹션 — 항상 출력
    message += "📅 <b>오늘 일정</b>\n\n"
    if today_events:
        message += _format_day_section(today_events)
    else:
        message += "<blockquote>오늘 일정 없슈 😴</blockquote>\n"

    message += "\n---\n\n"

    # 내일 섹션 — 항상 출력
    message += "📅 <b>내일 일정</b>\n\n"
    if tomorrow_events:
        message += _format_day_section(tomorrow_events)
    else:
        message += "<blockquote>내일 일정 없슈 😴</blockquote>\n"

    # 오늘+내일 모두 비었을 때만 추가 fallback 섹션 표시
    if extra_events:
        message += f"\n---\n\n📅 <b>{_html.escape(extra_label)}</b>\n\n"
        message += _format_day_section(extra_events)

    return message.rstrip()
