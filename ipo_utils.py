"""아이피오스탁(ipostock.co.kr) 공모주 캘린더.

오늘(KST) 청약 기간에 포함된 공모주를 조회해 텔레그램 HTML 메시지를 만든다.
텔레그램 의존성 없음 — bot.py 의 스케줄러 job / /ipo 명령어가 호출한다.

데이터 출처 (사이트 구조를 직접 확인한 결과)
  - 목록   /sub03/ipo04.asp?str1=<연도>&str2=all   공모청약일정 (연 전체, 미래 일정 포함,
           20행씩 &page=N 페이징, 시작일 내림차순)
  - 상세   /view_pg/view_04.asp?code=<코드>        청약일·환불일·상장일·확정공모가·주관사
  - 수요예측 /view_pg/view_05.asp?code=<코드>       단순 기관경쟁률
  * 목록/상세 모두 UTF-8 (meta charset). 목록의 종목명·주관사는 잘려서 표시되므로 상세 페이지 값을 쓴다.
"""
import html
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

import pytz
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

KST = pytz.timezone('Asia/Seoul')

BASE_URL   = 'http://www.ipostock.co.kr'
LIST_URL   = f'{BASE_URL}/sub03/ipo04.asp'
DETAIL_URL = f'{BASE_URL}/view_pg/view_04.asp'
DEMAND_URL = f'{BASE_URL}/view_pg/view_05.asp'

_HEADERS         = {'User-Agent': 'Mozilla/5.0 (compatible; AshuBot/1.0)'}
_TIMEOUT         = (5, 15)   # (connect, read) 초
_REQUEST_DELAY   = 1.5       # 요청 사이 간격(초) — 서버 부담 최소화
_STATE_FILE      = os.environ.get(
    'IPO_STATE_FILE',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ipo_state.json'),
)

TITLE      = '🐰 아슈 공모주 캘린더'
DISCLAIMER = '모든 투자 판단은 본인의 선택이슈!'
SEPARATOR  = '────────────'
NONE_TEXT  = '없슈'
MISSING    = '-'


class IpoError(Exception):
    """공모주 조회 실패 — '없슈'로 오인해 발송하면 안 되는 모든 오류의 부모."""


class IpoFetchError(IpoError):
    """사이트 접속 실패 (네트워크/타임아웃/HTTP 오류)."""


class IpoParseError(IpoError):
    """응답은 받았지만 예상한 구조가 아님 (사이트 구조 변경 등)."""


@dataclass
class ListRow:
    code:  str
    name:  str          # 목록의 종목명은 잘려서 표시됨
    start: date
    end:   date


@dataclass
class IpoItem:
    name:      str
    start:     date
    end:       date
    refund:    Optional[date] = None
    listing:   Optional[date] = None
    price:     Optional[int]  = None      # 확정 공모가 (원)
    inst_rate: Optional[str]  = None      # 단순 기관경쟁률 (예: '1187.74')
    brokers:   list = field(default_factory=list)


# ── HTTP ─────────────────────────────────────────────────────────────

def _decode(resp: requests.Response) -> str:
    """Content-Type → <meta charset> → utf-8 순으로 인코딩을 정하고 엄격하게 디코딩."""
    raw = resp.content
    enc = None
    m = re.search(r'charset=([\w-]+)', resp.headers.get('Content-Type', ''), re.I)
    if m:
        enc = m.group(1)
    if not enc:
        m = re.search(rb'charset\s*=\s*["\']?([\w-]+)', raw[:4096], re.I)
        enc = m.group(1).decode('ascii') if m else 'utf-8'
    for candidate in dict.fromkeys([enc, 'utf-8', 'cp949']):
        try:
            text = raw.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
        logger.info('[IPO] decode ok encoding=%s bytes=%d', candidate, len(raw))
        return text
    raise IpoParseError(f'디코딩 실패 (charset={enc})')


def _get(url: str, params: dict) -> str:
    """GET 1회 (재시도 없음 — 재시도는 호출하는 job 이 간격을 두고 결정)."""
    try:
        resp = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
    except requests.RequestException as e:
        raise IpoFetchError(f'접속 실패 {url} params={params}: {type(e).__name__}: {e}') from e
    if resp.status_code != 200:
        raise IpoFetchError(f'HTTP {resp.status_code} {url} params={params}')
    logger.info('[IPO] GET ok %s params=%s', url.replace(BASE_URL, ''), params)
    return _decode(resp)


# ── 파싱 공통 ─────────────────────────────────────────────────────────

def _clean(text: str) -> str:
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def _leaf_rows(soup: BeautifulSoup) -> list:
    """중첩 table 이 없는 tr 의 셀 텍스트 목록(빈 셀 포함)."""
    rows = []
    for tr in soup.find_all('tr'):
        if tr.find('table'):
            continue
        rows.append([_clean(td.get_text(' ', strip=True)) for td in tr.find_all(['td', 'th'])])
    return rows


def _label_values(rows: list) -> dict:
    """'라벨 | 값' 쌍(한 행에 여러 쌍 가능)을 dict 로. 라벨의 공백은 제거."""
    kv: dict = {}
    for cells in rows:
        for i in range(0, len(cells) - 1, 2):
            key = re.sub(r'\s+', '', cells[i])
            if key:
                kv.setdefault(key, cells[i + 1])
    return kv


_FULL_DATE_RE  = re.compile(r'(\d{4})\.(\d{1,2})\.(\d{1,2})')
_FULL_RANGE_RE = re.compile(
    r'(\d{4})\.(\d{1,2})\.(\d{1,2})\s*~\s*(?:(\d{4})\.)?(\d{1,2})\.(\d{1,2})')
_LIST_RANGE_RE = re.compile(r'^(\d{1,2})\.(\d{1,2})\s*~\s*(\d{1,2})\.(\d{1,2})$')


def _parse_full_date(text: Optional[str]) -> Optional[date]:
    m = _FULL_DATE_RE.search(text or '')
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _parse_range(text: Optional[str]) -> Optional[tuple]:
    """'2026.09.17 ~ 09.18' → (date, date). 연도 없는 종료일은 시작 연도 기준(연말 걸치면 +1)."""
    m = _FULL_RANGE_RE.search(text or '')
    if not m:
        return None
    y1, m1, d1 = int(m.group(1)), int(m.group(2)), int(m.group(3))
    m2, d2 = int(m.group(5)), int(m.group(6))
    y2 = int(m.group(4)) if m.group(4) else (y1 + 1 if m2 < m1 else y1)
    return date(y1, m1, d1), date(y2, m2, d2)


# ── 증권사 이름 (사이트가 6자로 잘라서 표시: 'IBK투자증' 등) ─────────────

_BROKER_FULL_NAMES = (
    'KB증권', 'NH투자증권', '삼성증권', '미래에셋증권', '한국투자증권', '신한투자증권',
    '하나증권', '대신증권', '키움증권', '메리츠증권', '한화투자증권', '유안타증권',
    '신영증권', 'LS증권', '교보증권', '현대차증권', 'DB증권', 'DB금융투자',
    '유진투자증권', 'SK증권', '에스케이증권', '하이투자증권', 'iM증권', '아이엠증권',
    '다올투자증권', '부국증권', '상상인증권', 'IBK투자증권', 'BNK투자증권',
    '케이프투자증권', '한양증권', '흥국증권', '리딩투자증권', '코리아에셋투자증권',
    '토스증권', '카카오페이증권', '우리투자증권', '이베스트투자증권', 'DS투자증권',
)


def complete_broker_name(name: str) -> str:
    """잘린 증권사명을 '유일하게' 매칭되는 정식 명칭으로 보완. 애매하면 사이트 표기 그대로."""
    n = name.strip()
    if not n or n in _BROKER_FULL_NAMES or len(n) < 3:
        return n
    matches = [f for f in _BROKER_FULL_NAMES if f.startswith(n)]
    return matches[0] if len(matches) == 1 else n


# ── 목록 페이지 ───────────────────────────────────────────────────────

def parse_list(page: str, year: int) -> list:
    """공모청약일정 목록 → ListRow. 목록 표 헤더가 없거나 행을 하나도 못 읽으면 IpoParseError."""
    soup = BeautifulSoup(page, 'lxml')
    text = soup.get_text(' ')
    if not all(label in text for label in ('공모일정', '종목명', '환불일', '상장일')):
        raise IpoParseError('공모청약일정 목록 표 헤더를 찾을 수 없음 (사이트 구조 변경 의심)')

    rows: dict = {}
    link_rows = 0
    for a in soup.select('a[href*="view_04.asp"]'):
        m = re.search(r'code=([A-Za-z0-9]+)', a.get('href', ''))
        tr = a.find_parent('tr')
        if not m or tr is None:
            continue
        link_rows += 1
        cells = [_clean(td.get_text(' ', strip=True)) for td in tr.find_all('td', recursive=False)]
        rng = next((_LIST_RANGE_RE.match(c) for c in cells if _LIST_RANGE_RE.match(c)), None)
        if rng is None:
            logger.warning('[IPO] parsing: 공모일정 셀을 읽지 못함 code=%s cells=%s', m.group(1), cells[:4])
            continue
        m1, d1, m2, d2 = (int(x) for x in rng.groups())
        try:
            start = date(year, m1, d1)
            end   = date(year + (1 if m2 < m1 else 0), m2, d2)
        except ValueError:
            logger.warning('[IPO] parsing: 잘못된 날짜 code=%s %s', m.group(1), rng.group(0))
            continue
        rows[m.group(1)] = ListRow(code=m.group(1), name=_clean(a.get_text()), start=start, end=end)

    if link_rows and not rows:
        raise IpoParseError(f'목록 행 {link_rows}개를 모두 파싱하지 못함 (사이트 구조 변경 의심)')
    logger.info('[IPO] list parsed: year=%s rows=%d (link_rows=%d)', year, len(rows), link_rows)
    return list(rows.values())


# ── 상세 페이지 (view_04) / 수요예측 (view_05) ─────────────────────────

def parse_detail(page: str) -> IpoItem:
    """공모정보 상세 → IpoItem(기관경쟁률 제외). 종목명/청약일이 없으면 IpoParseError."""
    soup = BeautifulSoup(page, 'lxml')
    title = soup.select_one('strong.view_tit')
    name = _clean(title.get_text()) if title else ''
    rows = _leaf_rows(soup)
    kv = _label_values(rows)

    rng = _parse_range(kv.get('공모청약일'))
    if not name or rng is None:
        raise IpoParseError(f'상세 페이지에서 종목명/공모청약일을 찾을 수 없음 (name={name!r}, 공모청약일={kv.get("공모청약일")!r})')

    price = None
    pm = re.search(r'([\d,]+)\s*원', kv.get('(확정)공모가격', ''))
    if pm:
        value = int(pm.group(1).replace(',', ''))
        price = value if value > 0 else None

    brokers: list = []
    in_table = False
    for cells in rows:
        if cells[:2] == ['증권회사', '배정수량']:
            in_table = True
            continue
        if in_table:
            if len(cells) >= 4 and re.search(r'[\d,]+\s*주', cells[1]):
                if '주관' in cells[3]:            # 대표주관/공동주관 (인수회사 제외)
                    brokers.append(complete_broker_name(cells[0]))
            else:
                in_table = False

    return IpoItem(
        name=name, start=rng[0], end=rng[1],
        refund=_parse_full_date(kv.get('환불일')),
        listing=_parse_full_date(kv.get('상장일')),
        price=price, brokers=brokers,
    )


def parse_institution_rate(page: str) -> Optional[str]:
    """수요예측 페이지의 '단순 기관경쟁률' → '1187.74'. 아직 발표 전이면 None."""
    soup = BeautifulSoup(page, 'lxml')
    rows = _leaf_rows(soup)
    if '수요예측일' not in _label_values(rows):
        raise IpoParseError('수요예측 페이지에서 수요예측일 항목을 찾을 수 없음 (사이트 구조 변경 의심)')
    for cells in rows:
        for i, cell in enumerate(cells):
            if '기관경쟁률' in cell:
                m = re.search(r'([\d,]+(?:\.\d+)?)\s*:\s*1', ' '.join(cells[i + 1:]))
                if m:
                    return m.group(1).replace(',', '')
    return None


# ── 조회 / 메시지 ─────────────────────────────────────────────────────

_LIST_PAGE_SIZE = 20     # 목록 1페이지 행 수 (연 전체 목록은 20행씩 페이징)
_LIST_PAGE_CAP  = 15     # 무한 순회 방지
_MAX_RANGE_DAYS = 31     # 청약 기간은 이보다 길 수 없다고 보고 조기 종료 판단에 사용


def _last_page_number(page: str) -> Optional[int]:
    """목록 하단 페이징 링크(?str1=..&page=N)에서 마지막 페이지 번호. 없으면 None."""
    soup = BeautifulSoup(page, 'lxml')
    nums = [int(n) for a in soup.find_all('a', href=True) if 'str2=' in a['href']
            for n in re.findall(r'[?&]page=(\d+)', a['href'])]
    return max(nums) if nums else None


def fetch_list_rows(year: int, today: date) -> list:
    """연 전체 청약 목록(페이지 순회). 목록이 시작일 내림차순임이 관측되고 today-31일보다
    오래된 행에 도달하면 조기 종료, 정렬이 어긋나거나 페이징 정보가 불명확하면 끝까지 조회."""
    seen: dict = {}
    last_page = None
    descending = True
    prev_min = None
    for page in range(1, _LIST_PAGE_CAP + 1):
        params = {'str1': year, 'str2': 'all'}
        if page > 1:
            params['page'] = page
        text = _get(LIST_URL, params)
        rows = parse_list(text, year)
        new = [r for r in rows if r.code not in seen]
        seen.update({r.code: r for r in new})
        if page == 1:
            last_page = _last_page_number(text)
        logger.info('[IPO] list page=%d rows=%d new=%d last_page=%s', page, len(rows), len(new), last_page)

        if not new:
            break
        if last_page is not None:
            if page >= last_page:
                break
        elif len(rows) < _LIST_PAGE_SIZE:
            break                                   # 페이징 정보 없음 + 마지막 페이지로 보임
        starts = [r.start for r in rows]
        if any(a < b for a, b in zip(starts, starts[1:])) or (prev_min is not None and max(starts) > prev_min):
            descending = False
        prev_min = min(starts)
        if descending and prev_min < today - timedelta(days=_MAX_RANGE_DAYS):
            break                                   # 이후 페이지는 더 오래된 일정
        time.sleep(_REQUEST_DELAY)
    else:
        logger.warning('[IPO] list 페이지 상한(%d) 도달', _LIST_PAGE_CAP)
    return list(seen.values())


def fetch_today_ipos(today: date) -> list:
    """today 가 청약 기간(시작~종료 포함)에 들어 있는 공모주 목록. 실패 시 IpoError."""
    list_rows = fetch_list_rows(today.year, today)
    hits = [r for r in list_rows if r.start <= today <= r.end]
    logger.info('[IPO] subscribing today=%s count=%d names=%s',
                today.isoformat(), len(hits), [r.name for r in hits])

    items = []
    for r in hits:
        time.sleep(_REQUEST_DELAY)
        item = parse_detail(_get(DETAIL_URL, {'code': r.code, 'gmenu': ''}))
        if not (item.start <= today <= item.end):
            logger.warning('[IPO] 목록/상세 청약일 불일치 → 제외 code=%s list=%s~%s detail=%s~%s',
                           r.code, r.start, r.end, item.start, item.end)
            continue
        time.sleep(_REQUEST_DELAY)
        item.inst_rate = parse_institution_rate(_get(DEMAND_URL, {'code': r.code, 'gmenu': ''}))
        logger.info('[IPO] item %s 청약=%s~%s 확정가=%s 기관=%s 주관=%s', item.name,
                    item.start, item.end, item.price, item.inst_rate, item.brokers)
        items.append(item)
    return items


def _md(d: Optional[date]) -> str:
    return d.strftime('%m.%d') if d else MISSING


def format_item(item: IpoItem) -> str:
    esc = html.escape
    period = _md(item.start) if item.start == item.end else f'{_md(item.start)}~{_md(item.end)}'
    price  = f'{item.price:,}원' if item.price else MISSING
    rate   = f'{item.inst_rate}:1' if item.inst_rate else MISSING
    broker = ', '.join(item.brokers) if item.brokers else MISSING
    return '\n'.join([
        f'종목: {esc(item.name)}',
        f'청약일: {period}',
        f'환불일: {_md(item.refund)}',
        f'상장일: {_md(item.listing)}',
        f'공모가: {price}',
        f'기관경쟁률: {rate}',
        f'주관사: {esc(broker)}',
    ])


def build_message(items: list) -> str:
    body = f'\n\n{SEPARATOR}\n\n'.join(format_item(i) for i in items) if items else NONE_TEXT
    return f'<b>{TITLE}</b>\n\n{body}\n\n{DISCLAIMER}'


def build_today_message(today: date) -> tuple:
    """(텔레그램 HTML 메시지, 종목명 목록). 조회/파싱 실패는 IpoError 로 전파 — 절대 '없슈'로 바꾸지 않는다."""
    items = fetch_today_ipos(today)
    return build_message(items), [i.name for i in items]


# ── 일일 발송 기록 (KST 날짜 기준 중복 발송 방지) ──────────────────────

def _read_state() -> dict:
    try:
        with open(_STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        logger.warning('[IPO] 발송 기록 파일을 읽지 못함(미발송으로 간주): %s', e)
        return {}


def already_sent(today: date) -> bool:
    return _read_state().get('last_sent_date') == today.isoformat()


def mark_sent(today: date, names: list) -> None:
    """자동 발송 성공 후에만 호출. 임시 파일 → os.replace 로 원자적 저장."""
    state = {
        'last_sent_date': today.isoformat(),
        'sent_at_kst': datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S'),
        'items': names,
    }
    tmp = f'{_STATE_FILE}.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, _STATE_FILE)
    except OSError as e:
        logger.error('[IPO] 발송 기록 저장 실패(재실행 시 중복 발송 가능): %s', e)
