"""키움증권 REST API (국내주식) — 인증 / 종목 검색 / 차트(OHLCV) 조회.

공식 사양 (Kiwoom-Securities/Kiwoom-REST-API, api.kiwoom.com)
  - Base URL   https://api.kiwoom.com
  - 토큰       POST /oauth2/token  {"grant_type":"client_credentials","appkey":..,"secretkey":..}
               → {"token","token_type","expires_dt"(KST, YYYYMMDDHHMMSS),"return_code","return_msg"}
  - 호출 헤더  api-id / authorization: Bearer <token> / (연속조회) cont-yn, next-key
  - 종목 목록  ka10099 /api/dostk/stkinfo  body {"mrkt_tp": 시장구분}
  - 차트       /api/dostk/chart  ka10081 일봉 · ka10082 주봉 · ka10083 월봉 · ka10080 분봉
  - 만료/무효 토큰은 HTTP 401 또는 본문 return_code 8005 → 재발급 후 1회만 재시도

App Key / Secret Key 는 소스에 두지 않고 다음 순서로 읽는다.
  1) 환경변수 KIWOOM_APP_KEY / KIWOOM_SECRET_KEY
  2) 프로젝트 폴더의 .env 파일 (Git 제외)
  3) 기존 방식의 키 파일 *_appkey.txt / *_secretkey.txt (하위 호환)
키 값과 토큰은 로그·에러 메시지에 절대 출력하지 않는다.
"""
import glob
import json
import logging
import os
import re
import threading
import time
import unicodedata
from datetime import datetime
from typing import Optional

import pandas as pd
import pytz
import requests

logger = logging.getLogger(__name__)

BASE_URL   = 'https://api.kiwoom.com'
TOKEN_PATH = '/oauth2/token'
KST        = pytz.timezone('Asia/Seoul')

_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
_TIMEOUT     = (5, 20)          # (connect, read) 초
_PAGE_DELAY  = 0.3              # 연속조회 요청 간격(초) — 키움 호출 제한 회피
_MAX_PAGES   = 10

ENV_APP_KEY    = 'KIWOOM_APP_KEY'
ENV_SECRET_KEY = 'KIWOOM_SECRET_KEY'


class KiwoomError(Exception):
    """키움 REST API 관련 오류의 부모."""


class KiwoomAuthError(KiwoomError):
    """App Key/Secret Key 없음, 토큰 발급 실패 등 인증 문제."""


class KiwoomDataError(KiwoomError):
    """종목 목록/차트 조회 실패 (네트워크, API 오류, 빈 데이터)."""


# ── App Key / Secret Key 로딩 ────────────────────────────────────────

def _read_dotenv(path: str) -> dict:
    """KEY=VALUE 형식의 .env 를 읽는다 (주석/따옴표 처리). 프로세스 환경변수는 건드리지 않는다."""
    values: dict = {}
    try:
        with open(path, 'r', encoding='utf-8-sig') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, _, val = line.partition('=')
                key = key.strip()
                if key.startswith('export '):
                    key = key[len('export '):].strip()
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                    val = val[1:-1]
                values[key] = val
    except OSError:
        pass
    return values


def _read_key_file(suffix: str) -> str:
    """프로젝트 폴더의 *_appkey.txt / *_secretkey.txt (하위 호환)."""
    for path in sorted(glob.glob(os.path.join(_PROJECT_DIR, f'*{suffix}'))):
        try:
            with open(path, 'r', encoding='utf-8-sig') as f:
                value = f.read().strip()
            if value:
                return value
        except OSError:
            continue
    return ''


def _resolve(env_name: str, file_suffix: str) -> tuple:
    """(값, 출처) — 출처: 'env' / '.env' / 'file' / ''. 값 자체는 로그에 쓰지 않는다."""
    value = os.environ.get(env_name, '').strip()
    if value:
        return value, 'env'
    value = _read_dotenv(os.path.join(_PROJECT_DIR, '.env')).get(env_name, '').strip()
    if value:
        return value, '.env'
    value = _read_key_file(file_suffix)
    if value:
        return value, 'file'
    return '', ''


def get_credentials() -> tuple:
    """(app_key, secret_key). 없으면 빈 문자열."""
    return _resolve(ENV_APP_KEY, '_appkey.txt')[0], _resolve(ENV_SECRET_KEY, '_secretkey.txt')[0]


def has_credentials() -> bool:
    app, secret = get_credentials()
    return bool(app and secret)


def log_credentials_status() -> None:
    """봇 시작 시 키 로딩 여부만 로그 (값은 출력하지 않음)."""
    app, app_src = _resolve(ENV_APP_KEY, '_appkey.txt')
    sec, sec_src = _resolve(ENV_SECRET_KEY, '_secretkey.txt')
    if app:
        logger.info('[KIWOOM] APP KEY loaded: YES')
    else:
        logger.error('[KIWOOM ERROR] APP KEY not found')
    if sec:
        logger.info('[KIWOOM] SECRET KEY loaded: YES')
    else:
        logger.error('[KIWOOM ERROR] SECRET KEY not found')
    if app or sec:
        logger.info('[KIWOOM] key source: app=%s secret=%s', app_src or '-', sec_src or '-')


def _redact(text: str) -> str:
    """서버 메시지에 혹시 섞여 있을 수 있는 키/토큰 값을 가린다."""
    text = str(text)
    secrets = list(get_credentials())
    with _TOKEN_LOCK:
        if _TOKEN['token']:
            secrets.append(_TOKEN['token'])
    for s in secrets:
        if s and len(s) >= 8:
            text = text.replace(s, '***')
    return text


# ── Access Token (캐시 · 만료 시에만 재발급) ─────────────────────────

_TOKEN: dict = {'token': None, 'expires_at': 0.0}
_TOKEN_LOCK = threading.RLock()
_TOKEN_MARGIN = 300             # 만료 5분 전이면 재발급
_TOKEN_FALLBACK_TTL = 6 * 3600  # expires_dt 를 못 읽었을 때


def _parse_expiry(value: str) -> Optional[float]:
    try:
        return KST.localize(datetime.strptime(str(value), '%Y%m%d%H%M%S')).timestamp()
    except (ValueError, TypeError):
        return None


def invalidate_token() -> None:
    with _TOKEN_LOCK:
        _TOKEN['token'] = None
        _TOKEN['expires_at'] = 0.0


def get_access_token(force: bool = False) -> str:
    """캐시된 유효 토큰을 재사용하고, 만료(또는 force)일 때만 새로 발급."""
    with _TOKEN_LOCK:
        now = time.time()
        if not force and _TOKEN['token'] and now < _TOKEN['expires_at'] - _TOKEN_MARGIN:
            return _TOKEN['token']
        return _issue_token()


def _issue_token() -> str:
    app_key, secret = get_credentials()
    if not app_key or not secret:
        if not app_key:
            logger.error('[KIWOOM ERROR] APP KEY not found')
        if not secret:
            logger.error('[KIWOOM ERROR] SECRET KEY not found')
        raise KiwoomAuthError('키움 App Key/Secret Key 가 설정되지 않았습니다')
    try:
        resp = requests.post(
            BASE_URL + TOKEN_PATH,
            json={'grant_type': 'client_credentials', 'appkey': app_key, 'secretkey': secret},
            headers={'Content-Type': 'application/json;charset=UTF-8'},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as e:
        logger.error('[KIWOOM AUTH ERROR] status=network')
        logger.error('[KIWOOM AUTH ERROR] message=%s', type(e).__name__)
        raise KiwoomAuthError(f'토큰 요청 실패: {type(e).__name__}') from e

    try:
        data = resp.json()
    except ValueError:
        data = {}
    token = data.get('token')
    code = data.get('return_code')
    if resp.status_code != 200 or code not in (None, 0, '0') or not token:
        msg = _redact(data.get('return_msg') or resp.text[:200])
        logger.error('[KIWOOM AUTH ERROR] status=%s', resp.status_code)
        logger.error('[KIWOOM AUTH ERROR] message=%s', msg)
        raise KiwoomAuthError(f'토큰 발급 실패 (status={resp.status_code})')

    _TOKEN['token'] = token
    _TOKEN['expires_at'] = _parse_expiry(data.get('expires_dt')) or (time.time() + _TOKEN_FALLBACK_TTL)
    logger.info('[KIWOOM AUTH] token issued successfully')
    return token


# ── 공통 요청 ────────────────────────────────────────────────────────

def _request(api_id: str, path: str, body: dict, cont_yn: str = '', next_key: str = '',
             _retry: bool = True) -> tuple:
    """(응답 dict, cont-yn, next-key). 무효 토큰이면 재발급 후 1회만 재시도."""
    token = get_access_token()
    headers = {
        'Content-Type': 'application/json;charset=UTF-8',
        'api-id': api_id,
        'authorization': f'Bearer {token}',
    }
    if cont_yn:
        headers['cont-yn'] = cont_yn
        headers['next-key'] = next_key
    try:
        resp = requests.post(BASE_URL + path, headers=headers, json=body, timeout=_TIMEOUT)
    except requests.RequestException as e:
        raise KiwoomDataError(f'{api_id} 네트워크 오류: {type(e).__name__}') from e

    try:
        data = resp.json()
    except ValueError:
        data = {}
    rc = data.get('return_code')
    msg = str(data.get('return_msg') or '')

    if resp.status_code == 401 or str(rc) == '8005' or '8005' in msg:
        if _retry:
            logger.warning('[KIWOOM AUTH] token rejected (%s) → re-issuing once', api_id)
            invalidate_token()
            return _request(api_id, path, body, cont_yn, next_key, _retry=False)
        logger.error('[KIWOOM AUTH ERROR] status=%s', resp.status_code)
        logger.error('[KIWOOM AUTH ERROR] message=%s', _redact(msg))
        raise KiwoomAuthError('토큰 재발급 후에도 인증 실패')
    if resp.status_code >= 400:
        raise KiwoomDataError(f'{api_id} HTTP {resp.status_code}: {_redact(msg)}')
    if rc not in (None, 0, '0'):
        raise KiwoomDataError(f'{api_id} 오류 return_code={rc}: {_redact(msg)}')
    return data, resp.headers.get('cont-yn', 'N'), resp.headers.get('next-key', '')


# ── 국내 종목 판별 / 정규화 ──────────────────────────────────────────

_HANGUL_RE    = re.compile(r'[가-힣ㄱ-ㅎㅏ-ㅣ]')
_CODE_RE      = re.compile(r'[0-9][0-9A-Z]{5}')   # 005930, 0015M0 등 KRX 6자리 단축코드


def is_korean_input(text: str) -> bool:
    """한글이 있거나 6자리 종목코드이면 국내주식으로 본다 (AAPL 같은 영문 티커는 해외)."""
    t = (text or '').strip()
    return bool(_HANGUL_RE.search(t)) or bool(_CODE_RE.fullmatch(t.upper()))


def _norm(text: str) -> str:
    text = unicodedata.normalize('NFC', str(text))
    return re.sub(r'[\s\-_./()\[\]·&,+*%@#$!^~|]', '', text).casefold()


# ── 종목 마스터 (ka10099 종목정보 리스트) ────────────────────────────

# (mrkt_tp, 이름) — 0 코스피, 10 코스닥, 50 코넥스, 8 ETF, 60 ETN, 6 리츠
_MASTER_MARKETS = (('0', 'KOSPI'), ('10', 'KOSDAQ'), ('50', 'KONEX'), ('8', 'ETF'), ('60', 'ETN'), ('6', 'REIT'))
_MASTER_TTL     = 24 * 3600
_MASTER_FILE    = os.environ.get('KIWOOM_MASTER_CACHE', os.path.join(_PROJECT_DIR, 'kiwoom_stock_master.json'))
_MASTER_LOCK    = threading.RLock()
_MASTER: dict = {'loaded_at': 0.0, 'items': [], 'by_code': {}, 'by_norm': {}}


def _to_int(value) -> int:
    try:
        return int(str(value).strip() or 0)
    except ValueError:
        return 0


def _fetch_master_from_api() -> list:
    items: list = []
    ok_markets = set()
    for mrkt_tp, label in _MASTER_MARKETS:
        try:
            data, _, _ = _request('ka10099', '/api/dostk/stkinfo', {'mrkt_tp': mrkt_tp})
        except KiwoomAuthError:
            raise
        except KiwoomError as e:
            logger.warning('[KIWOOM] 종목 목록 조회 실패 mrkt_tp=%s: %s', mrkt_tp, e)
            continue
        ok_markets.add(mrkt_tp)
        for row in data.get('list') or []:
            code = str(row.get('code', '')).strip().upper()
            name = str(row.get('name', '')).strip()
            if not code or not name:
                continue
            items.append({
                'code': code,
                'name': name,
                'market': label,
                'market_name': str(row.get('marketName', '')).strip(),
                'cap': _to_int(row.get('lastPrice')) * _to_int(row.get('listCount')),
            })
        time.sleep(_PAGE_DELAY)
    if not ({'0', '10'} & ok_markets):
        raise KiwoomDataError('종목 목록(코스피/코스닥) 조회 실패')
    logger.info('[KIWOOM] stock master loaded from API: %d items', len(items))
    return items


def _index_master(items: list, loaded_at: float) -> None:
    by_code: dict = {}
    by_norm: dict = {}
    prepared = []
    for it in items:
        it = dict(it)
        it['norm'] = _norm(it['name'])
        prepared.append(it)
        by_code.setdefault(it['code'], it)
        cur = by_norm.get(it['norm'])
        if cur is None or it['cap'] > cur['cap']:
            by_norm[it['norm']] = it
    _MASTER.update(loaded_at=loaded_at, items=prepared, by_code=by_code, by_norm=by_norm)


def _load_master_file() -> Optional[tuple]:
    try:
        with open(_MASTER_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        items = data.get('items')
        if isinstance(items, list) and items:
            return items, float(data.get('saved_at', 0))
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _save_master_file(items: list) -> None:
    tmp = f'{_MASTER_FILE}.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'saved_at': time.time(), 'items': items}, f, ensure_ascii=False)
        os.replace(tmp, _MASTER_FILE)
    except OSError as e:
        logger.warning('[KIWOOM] 종목 마스터 캐시 저장 실패: %s', e)


def load_master(force: bool = False) -> None:
    """메모리 → 파일 캐시(24h) → 공식 API 순으로 종목 마스터를 준비. API 실패 시 오래된 캐시라도 사용."""
    with _MASTER_LOCK:
        now = time.time()
        if not force and _MASTER['items'] and now - _MASTER['loaded_at'] < _MASTER_TTL:
            return
        cached = _load_master_file()
        if not force and cached and now - cached[1] < _MASTER_TTL:
            _index_master(cached[0], cached[1])
            logger.info('[KIWOOM] stock master loaded from cache file: %d items', len(cached[0]))
            return
        try:
            items = _fetch_master_from_api()
        except KiwoomError as e:
            stale = cached[0] if cached else (_MASTER['items'] or None)
            if stale:
                logger.warning('[KIWOOM] 종목 목록 갱신 실패 → 기존 캐시 사용(10분 뒤 재시도): %s', e)
                _index_master(stale, now - _MASTER_TTL + 600)
                return
            raise
        _index_master(items, now)
        _save_master_file(items)


def search_stock(query: str) -> Optional[dict]:
    """종목명 또는 6자리 종목코드 → {'code','name','market','market_name'} (없으면 None).

    우선순위: 코드 일치 → 이름 정규화 일치 → 이름 접두 일치 → 이름 포함.
    부분 일치가 여러 개면 시가총액(전일종가×상장주식수)이 큰 종목을 선택한다.
    """
    q = (query or '').strip()
    if not q:
        return None
    load_master()
    if _CODE_RE.fullmatch(q.upper()):
        hit = _MASTER['by_code'].get(q.upper())
        return _public(hit) if hit else None
    nq = _norm(q)
    if not nq:
        return None
    exact = _MASTER['by_norm'].get(nq)
    if exact:
        return _public(exact)
    for match in (lambda n: n.startswith(nq), lambda n: nq in n):
        cands = [it for it in _MASTER['items'] if match(it['norm'])]
        if cands:
            return _public(max(cands, key=lambda it: it['cap']))
    return None


def find_exact_name(query: str) -> Optional[dict]:
    """영문 이름만 있는 국내 종목(예: NAVER) 구제용 — 이름이 정확히 같을 때만, 키가 없으면 조용히 None."""
    if not has_credentials():
        return None
    try:
        load_master()
    except KiwoomError:
        return None
    hit = _MASTER['by_norm'].get(_norm(query))
    return _public(hit) if hit else None


def _public(item: dict) -> dict:
    return {k: item[k] for k in ('code', 'name', 'market', 'market_name')}


# ── 차트 (OHLCV) ─────────────────────────────────────────────────────

# 봉 종류 → (api-id, 응답 리스트 키)
_CHART_API = {
    'day':   ('ka10081', 'stk_dt_pole_chart_qry'),
    'week':  ('ka10082', 'stk_stk_pole_chart_qry'),
    'month': ('ka10083', 'stk_mth_pole_chart_qry'),
    'hour':  ('ka10080', 'stk_min_pole_chart_qry'),     # tic_scope=60
}
_REGULAR_HOURS = range(9, 16)    # 60분봉 라벨 09:00~15:00 = 정규장 (16시 이후는 시간외/NXT)


def _num(value) -> float:
    """'+275000' / '-199400' 처럼 부호가 붙어도 절대값 (부호는 전일대비 방향 표시)."""
    text = str(value).strip().replace(',', '')
    return abs(float(text)) if text else 0.0


def fetch_ohlcv(code: str, kind: str = 'day', min_rows: int = 60) -> pd.DataFrame:
    """국내주식 OHLCV (오름차순, KST tz-aware index, 컬럼 open/high/low/close/volume).

    kind: 'day' | 'week' | 'month' | 'hour'(60분봉, 정규장만). 연속조회로 min_rows 이상 모은다.
    """
    if kind not in _CHART_API:
        raise KiwoomDataError(f'지원하지 않는 봉 종류: {kind}')
    api_id, key = _CHART_API[kind]
    body = {'stk_cd': code, 'upd_stkpc_tp': '1'}
    if kind == 'hour':
        body['tic_scope'] = '60'
    else:
        body['base_dt'] = datetime.now(KST).strftime('%Y%m%d')
    logger.info('[KIWOOM CHART] symbol=%s kind=%s api=%s', code, kind, api_id)

    records: dict = {}
    cont_yn = next_key = ''
    for page in range(_MAX_PAGES):
        data, cont_yn, next_key = _request(api_id, '/api/dostk/chart', body, cont_yn, next_key)
        for row in data.get(key) or []:
            try:
                close = _num(row.get('cur_prc'))
                if kind == 'hour':
                    tm = str(row.get('cntr_tm', ''))
                    if len(tm) < 12 or int(tm[8:10]) not in _REGULAR_HOURS:
                        continue
                    ts = pd.Timestamp(datetime.strptime(tm[:14], '%Y%m%d%H%M%S')).tz_localize(KST)
                else:
                    ts = pd.Timestamp(datetime.strptime(str(row.get('dt', '')), '%Y%m%d')).tz_localize(KST)
                o, h, l = _num(row.get('open_pric')), _num(row.get('high_pric')), _num(row.get('low_pric'))
                if close <= 0 or o <= 0 or h <= 0 or l <= 0:
                    continue          # 존재하지 않는 코드가 주는 빈 행 등
                records[ts] = (o, h, l, close, _num(row.get('trde_qty')))
            except (ValueError, TypeError):
                continue
        if cont_yn != 'Y' or len(records) >= min_rows:
            break
        time.sleep(_PAGE_DELAY)

    if not records:
        raise KiwoomDataError(f'차트 데이터 없음: {code}')
    df = pd.DataFrame.from_dict(records, orient='index',
                                columns=['open', 'high', 'low', 'close', 'volume']).sort_index()
    df.index.name = 'timestamp'
    logger.info('[KIWOOM CHART] received rows=%d', len(df))
    return df
