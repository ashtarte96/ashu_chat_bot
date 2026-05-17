"""
calendar_utils.py — 글로벌 경제 캘린더 (ForexFactory XML 기반)
API KEY 불필요.
날짜 기준: Asia/Seoul (KST). ET 시간 → KST 자동 변환 (DST 대응).
"""

import html as _html
from datetime import datetime, date as _date, timedelta
from xml.etree import ElementTree as ET

import pytz
import requests

# ── 상수 ──────────────────────────────────────────────────────────────────────

_FF_CAL_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
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

_ET_TZ  = pytz.timezone('America/New_York')  # EST/EDT DST 자동 처리
_KST_TZ = pytz.timezone('Asia/Seoul')        # KST UTC+9 고정

_NO_TIME = frozenset({'all day', 'tentative', 'day 1', 'day 2', 'weekend', ''})

# ── 국가 정보 ─────────────────────────────────────────────────────────────────

_COUNTRY_INFO: dict[str, tuple[str, str]] = {
    'USD': ('🇺🇸', '미국'),
    'KRW': ('🇰🇷', '한국'),
    'CNY': ('🇨🇳', '중국'),
    'JPY': ('🇯🇵', '일본'),
    'EUR': ('🇪🇺', '유럽'),
    'GBP': ('🇬🇧', '영국'),
}
_PRIORITY_CURRENCIES = list(_COUNTRY_INFO.keys())
_OTHER_FLAG, _OTHER_NAME = '🌍', '기타'

# ── 중요도 ────────────────────────────────────────────────────────────────────

_IMPACT_EMOJI = {'High': '🔥', 'Medium': '⭐'}
_SHOW_IMPACT  = {'High', 'Medium'}
_IMPACT_ORDER = {'High': 0, 'Medium': 1}

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
    ('Fed Chair',                       '연준 의장'),
    # 미국 — 성장
    ('Prelim GDP q/q',                  'GDP 예비치 (전분기비)'),
    ('GDP q/q',                         'GDP (전분기비)'),
    # 미국 — 소비
    ('Core Retail Sales m/m',           '근원 소매판매 (전월비)'),
    ('Retail Sales m/m',                '소매판매 (전월비)'),
    ('Consumer Confidence',             '소비자신뢰지수'),
    ('Consumer Sentiment',              '소비자심리지수'),
    # 미국 — 제조/무역
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
    # 기타
    ('OPEC',                            'OPEC 회의'),
]


def _ko_title(title: str) -> str:
    title_lower = title.lower()
    for eng, kor in _EVENT_KO:
        if eng.lower() in title_lower:
            return kor
    return title


# ── 날짜/시간 유틸 ────────────────────────────────────────────────────────────

def _parse_ff_date(date_str: str) -> _date | None:
    for fmt in ('%m-%d-%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _et_to_kst(ev_date: _date, time_str: str) -> tuple[datetime | None, str]:
    """
    ForexFactory ET 날짜+시간 → (KST datetime, 'HH:MM').
    All Day / Tentative → (None, '종일').
    DST는 pytz 자동 처리.
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


# ── 데이터 fetch ──────────────────────────────────────────────────────────────

def _fetch_all_week_events() -> list:
    """ForexFactory 주간 XML에서 Medium+ 이벤트 전체 파싱."""
    print(f"[ECONOMIC CALENDAR FETCH] url={_FF_CAL_URL}")
    try:
        resp = requests.get(_FF_CAL_URL, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except requests.exceptions.RequestException as exc:
        print(f"[ECONOMIC CALENDAR FETCH] HTTP error: {exc}")
        return []
    except ET.ParseError as exc:
        print(f"[ECONOMIC CALENDAR FETCH] XML parse error: {exc}")
        return []

    events: list = []
    total_xml    = 0

    for ev in root.findall('event'):
        total_xml += 1

        impact = (ev.findtext('impact') or '').strip()
        if impact not in _SHOW_IMPACT:
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

        # KST 날짜 기준: 시간 있으면 변환 결과, All Day면 ET 날짜를 그대로 사용
        kst_date: _date = kst_dt.date() if kst_dt else ev_date

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
        })

    print(f"[ECONOMIC CALENDAR FETCH] xml_total={total_xml} medium_plus={len(events)}")
    return events


def fetch_calendar_by_kst_dates(target_dates: list[_date]) -> dict[_date, list]:
    """
    KST 날짜 리스트 기준으로 이벤트를 그룹핑해 반환.
    각 날짜 내 중복 제거 포함.
    """
    all_events = _fetch_all_week_events()
    target_set = set(target_dates)

    buckets: dict[_date, list] = {d: [] for d in target_dates}
    for ev in all_events:
        if ev['kst_date'] in target_set:
            buckets[ev['kst_date']].append(ev)

    # 날짜별 중복 제거
    for d in buckets:
        buckets[d] = _dedupe(buckets[d])

    return buckets


# ── 중복 제거 ─────────────────────────────────────────────────────────────────

def _dedupe(events: list) -> list:
    seen: set = set()
    result    = []
    for ev in events:
        key = (ev['country'], ev['title_ko'])
        if key not in seen:
            seen.add(key)
            result.append(ev)
    return result


# ── 정렬 ──────────────────────────────────────────────────────────────────────

def _sort_key(ev: dict) -> tuple:
    """KST 시간 오름차순 → 같은 시간 내 High 먼저 → 시간 없는 것 맨 뒤."""
    kst_dt = ev.get('kst_dt')
    return (
        kst_dt.timestamp() if kst_dt else float('inf'),
        _IMPACT_ORDER.get(ev['impact'], 9),
    )


# ── 포맷 ──────────────────────────────────────────────────────────────────────

def _fmt_line(ev: dict) -> str:
    """🕘 HH:MM 🔥/⭐ 제목 (예상 X%)"""
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
    """하루치 이벤트를 국가별로 묶어 포맷."""
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
        section += f"{flag} <b>{_html.escape(name)}</b>\n"
        for ev in by_country[currency]:
            section += _fmt_line(ev) + "\n"
        section += "\n"

    # 기타 (우선 국가 이외 High 한정)
    others = [
        ev for cur, evs in by_country.items()
        if cur not in _PRIORITY_CURRENCIES
        for ev in evs
        if ev['impact'] == 'High'
    ]
    if others:
        others.sort(key=_sort_key)
        section += f"{_OTHER_FLAG} <b>{_html.escape(_OTHER_NAME)}</b>\n"
        for ev in others:
            section += _fmt_line(ev) + "\n"
        section += "\n"

    return section


def build_calendar_message(is_test: bool = False) -> str:
    """오늘 + 내일 (KST 기준) 경제 캘린더 텔레그램 메시지 빌드."""
    header = "🐰 아슈 경제 캘린더 테스트" if is_test else "🐰 아슈 출근길 글로벌 경제 캘린더"

    now_kst      = datetime.now(_KST_TZ)
    today_kst    = now_kst.date()
    tomorrow_kst = (now_kst + timedelta(days=1)).date()

    buckets = fetch_calendar_by_kst_dates([today_kst, tomorrow_kst])

    today_events    = buckets.get(today_kst,    [])
    tomorrow_events = buckets.get(tomorrow_kst, [])

    print(f"[GC TODAY COUNT]    {len(today_events)}")
    print(f"[GC TOMORROW COUNT] {len(tomorrow_events)}")

    if not today_events and not tomorrow_events:
        return (
            f"<b>{_html.escape(header)}</b>\n\n"
            "📭 오늘/내일 주요 경제 일정이 없습니다."
        )

    message = f"<b>{_html.escape(header)}</b>\n\n"

    # ── 오늘 일정 ──
    if today_events:
        message += "📅 <b>오늘 일정</b>\n\n"
        message += _format_day_section(today_events)

    # ── 구분선 ──
    if today_events and tomorrow_events:
        message += "---\n\n"

    # ── 내일 일정 ──
    if tomorrow_events:
        message += "📅 <b>내일 일정</b>\n\n"
        message += _format_day_section(tomorrow_events)

    return message.rstrip()
