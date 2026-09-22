"""
chart_utils.py
Binance REST API (primary) + Bybit v5 REST API (fallback) + MEXC (last resort)
+ Kiwoom REST API (Korean & US stocks, via kiwoom_api.py)
"""

import json as _json
import logging
import os
import re
import tempfile
import time as _time
import traceback as tb
import unicodedata
from datetime import date, datetime, timedelta

import pandas as pd
import requests
import kiwoom_api
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.ticker as mticker

logger = logging.getLogger(__name__)


def _setup_korean_font_fallback() -> bool:
    """기본 글꼴(DejaVu Sans)에 없는 한글 글리프만 설치된 한글 폰트로 대체 (차트 디자인은 그대로).
    한글 폰트가 하나도 없으면 False — 한글 제목 대신 종목코드를 쓰도록 호출부가 판단."""
    try:
        from matplotlib import font_manager as fm
        available = {f.name for f in fm.fontManager.ttflist}
        found = [n for n in ('Malgun Gothic', 'NanumGothic', 'Nanum Gothic', 'Noto Sans CJK KR',
                             'Noto Sans KR', 'AppleGothic', 'UnDotum', 'Baekmuk Gothic') if n in available]
        if found:
            plt.rcParams['font.family'] = ['DejaVu Sans'] + found
        return bool(found)
    except Exception:
        return False


_HAS_KOREAN_FONT = _setup_korean_font_fallback()

# ── Timeframe constants ────────────────────────────────────────────────

VALID_INTERVALS = {'15m', '1h', '4h', '12h', '1d', '1w', '1y'}

_TIMEFRAME_LABEL = {
    '15m': '15M', '1h': '1H', '4h': '4H', '12h': '12H',
    '1d': '1D', '1w': '1W', '1y': '1Y',
}

_TIMEFRAME_DATE_FMT = {
    '15m': '%m/%d %H:%M',
    '1h':  '%m/%d %H:%M',
    '4h':  '%m/%d %H:%M',
    '12h': '%m/%d %H:%M',
    '1d':  '%m/%d',
    '1w':  '%y/%m/%d',
    '1y':  '%Y/%m',
}

# Binance kline interval strings (1y uses 1d data, resampled to monthly)
_BINANCE_INTERVAL = {
    '15m': '15m', '1h': '1h', '4h': '4h', '12h': '12h',
    '1d': '1d', '1w': '1w', '1y': '1d',
}

# Bybit kline interval strings (max 200 candles per request)
_BYBIT_INTERVAL = {
    '15m': '15', '1h': '60', '4h': '240', '12h': '720',
    '1d': 'D', '1w': 'W', '1y': 'D',
}

# API fetch limits
# 1d fetches 365 so _draw_chart(tail 60) has data AND 52w stats are accurate
_FETCH_LIMIT = {
    '15m': 60,
    '1h':  60,
    '4h':  60,
    '12h': 60,
    '1d':  365,
    '1w':  60,
    '1y':  1000,  # daily data fetched, then resampled to monthly
}

# Pandas month-end resample alias changed in 2.2
_PD_VER = tuple(int(x) for x in pd.__version__.split('.')[:2])
_MONTH_RULE = 'ME' if _PD_VER >= (2, 2) else 'M'


# ── Formatters ─────────────────────────────────────────────────────────

def normalize_symbol(ticker: str) -> str:
    ticker = ticker.upper().strip()
    if '/' in ticker:
        return ticker
    if ticker.endswith('USDT'):
        return f"{ticker[:-4]}/USDT"
    return f"{ticker}/USDT"


def format_price(price: float) -> str:
    """가격 포맷.
    >= 1 : 천단위 콤마 + 소수 2자리  (예: 12,345.68 / 12.34)
    < 1  : 소수 4자리, 불필요한 후행 0 제거 (예: 0.9234 / 0.04 / 0.0031)
    절대 int 변환 금지."""
    if price >= 1:
        return f"{price:,.2f}"
    s = f"{price:.4f}".rstrip('0').rstrip('.')
    return s if s else '0'


def _fmt_kr(price: float) -> str:
    """KRW 가격 포맷 (국내주식 정수 원화 / 1원 미만 코인 공용)."""
    if price >= 1:
        return f"{round(price):,}"
    s = f"{price:.4f}".rstrip('0').rstrip('.')
    return s if s else '0'


def _fmt_us(price: float) -> str:
    """USD 가격 포맷 — format_price와 동일 로직."""
    return format_price(price)


def _change_line(current: float, prev: float, fmt_fn=None) -> str:
    if fmt_fn is None:
        fmt_fn = format_price
    change = current - prev
    pct    = change / prev * 100 if prev else 0.0
    abs_ch = fmt_fn(abs(change))
    if change >= 0:
        return f"+{abs_ch} (+{pct:.2f}%)"
    return f"-{abs_ch} (-{abs(pct):.2f}%)"


# ── Symbol / timeframe parsing ─────────────────────────────────────────

def _parse_symbol(user_input: str) -> str:
    """BTC / btc / BTC/USDT / BTCUSDT → 'BTCUSDT'"""
    s = user_input.upper().strip()
    if '/' in s:
        s = s.split('/')[0]
    if s.endswith('USDT'):
        return s
    return s + 'USDT'


def parse_timeframe(raw: str) -> 'str | None':
    """'1H' / '4h' / '1W' → normalized lowercase key. None if invalid."""
    normalized = raw.strip().lower()
    return normalized if normalized in VALID_INTERVALS else None


# ── OHLCV resampling ───────────────────────────────────────────────────

def _resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample OHLCV DataFrame to a coarser frequency."""
    agg = {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}
    if 'volume' in df.columns:
        agg['volume'] = 'sum'
    return df.resample(rule).agg(agg).dropna(subset=['open', 'close'])


def _to_monthly(df: pd.DataFrame) -> pd.DataFrame:
    """Resample daily data to monthly, keep last 60 months."""
    return _resample_ohlcv(df, _MONTH_RULE).tail(60)


# ── Binance REST API ───────────────────────────────────────────────────

def fetch_binance_spot(symbol: str, timeframe: str, limit: int = 60) -> 'pd.DataFrame | None':
    """Binance spot kline. Returns ascending DataFrame or None."""
    interval = _BINANCE_INTERVAL.get(timeframe, '1d')
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    try:
        r = requests.get("https://api.binance.com/api/v3/klines",
                         params=params, timeout=15)
        if r.status_code != 200:
            print(f"[BINANCE SPOT FAIL] {symbol} HTTP {r.status_code}: {r.text[:200]}")
            return None
        rows = r.json()
        if not rows or not isinstance(rows, list):
            print(f"[BINANCE SPOT FAIL] {symbol} empty response")
            return None
        df = pd.DataFrame(
            [[row[0], row[1], row[2], row[3], row[4], row[5]] for row in rows],
            columns=['ts', 'open', 'high', 'low', 'close', 'volume'],
        )
        df.index = pd.to_datetime(df['ts'].astype(int), unit='ms', utc=True)
        df.index.name = 'timestamp'
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df = df[['open', 'high', 'low', 'close', 'volume']].dropna()
        print(f"[BINANCE SPOT OK] {symbol} {timeframe} rows={len(df)}")
        print(f"[BINANCE SPOT DF] index.dtype={df.index.dtype} tz={df.index.tz}")
        print(df.head(3).to_string())
        return df if not df.empty else None
    except Exception:
        print(f"[BINANCE SPOT FAIL] {symbol} {timeframe}")
        tb.print_exc()
        logger.exception("[BINANCE SPOT FAIL] %s %s", symbol, timeframe)
        return None


def fetch_binance_futures(symbol: str, timeframe: str, limit: int = 60) -> 'pd.DataFrame | None':
    """Binance USDT-M futures kline."""
    interval = _BINANCE_INTERVAL.get(timeframe, '1d')
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/klines",
                         params=params, timeout=15)
        if r.status_code != 200:
            print(f"[BINANCE FUTURES FAIL] {symbol} HTTP {r.status_code}: {r.text[:200]}")
            return None
        rows = r.json()
        if not rows or not isinstance(rows, list):
            print(f"[BINANCE FUTURES FAIL] {symbol} empty response")
            return None
        df = pd.DataFrame(
            [[row[0], row[1], row[2], row[3], row[4], row[5]] for row in rows],
            columns=['ts', 'open', 'high', 'low', 'close', 'volume'],
        )
        df.index = pd.to_datetime(df['ts'].astype(int), unit='ms', utc=True)
        df.index.name = 'timestamp'
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df = df[['open', 'high', 'low', 'close', 'volume']].dropna()
        print(f"[BINANCE FUTURES OK] {symbol} {timeframe} rows={len(df)}")
        print(f"[BINANCE FUTURES DF] index.dtype={df.index.dtype} tz={df.index.tz}")
        print(df.head(3).to_string())
        return df if not df.empty else None
    except Exception:
        print(f"[BINANCE FUTURES FAIL] {symbol} {timeframe}")
        tb.print_exc()
        logger.exception("[BINANCE FUTURES FAIL] %s %s", symbol, timeframe)
        return None


def get_funding_rate(symbol: str) -> 'float | None':
    """Binance USDT-M latest funding rate. Returns None on failure."""
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/fundingRate",
            params={"symbol": symbol, "limit": 1},
            timeout=10,
        )
        data = r.json()
        if data and isinstance(data, list):
            return float(data[-1]['fundingRate'])
    except Exception:
        pass
    return None


# ── Bybit v5 REST API ──────────────────────────────────────────────────

def fetch_bybit_spot(symbol: str, timeframe: str, limit: int = 60) -> 'pd.DataFrame | None':
    """Bybit spot kline. Returns ascending DataFrame or None."""
    limit    = min(limit, 200)  # Bybit max is 200
    interval = _BYBIT_INTERVAL.get(timeframe, 'D')
    params = {"category": "spot", "symbol": symbol,
              "interval": interval, "limit": limit}
    try:
        r = requests.get("https://api.bybit.com/v5/market/kline",
                         params=params, timeout=15)
        if r.status_code != 200:
            print(f"[BYBIT SPOT FAIL] {symbol} HTTP {r.status_code}")
            return None
        data = r.json()
        if data.get('retCode') != 0:
            print(f"[BYBIT SPOT FAIL] {symbol} retCode={data.get('retCode')} {data.get('retMsg')}")
            return None
        rows = data.get('result', {}).get('list', [])
        if not rows:
            print(f"[BYBIT SPOT FAIL] {symbol} empty list")
            return None
        rows = sorted(rows, key=lambda x: int(x[0]))  # newest-first → ascending
        df = pd.DataFrame(rows,
                          columns=['ts', 'open', 'high', 'low', 'close', 'volume', 'turnover'])
        df.index = pd.to_datetime(df['ts'].astype(int), unit='ms', utc=True)
        df.index.name = 'timestamp'
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df = df[['open', 'high', 'low', 'close', 'volume']].dropna()
        print(f"[BYBIT SPOT OK] {symbol} {timeframe} rows={len(df)}")
        print(f"[BYBIT SPOT DF] index.dtype={df.index.dtype} tz={df.index.tz}")
        print(df.head(3).to_string())
        return df if not df.empty else None
    except Exception:
        print(f"[BYBIT SPOT FAIL] {symbol} {timeframe}")
        tb.print_exc()
        logger.exception("[BYBIT SPOT FAIL] %s %s", symbol, timeframe)
        return None


def fetch_bybit_perps(symbol: str, timeframe: str, limit: int = 60) -> 'pd.DataFrame | None':
    """Bybit USDT linear perps kline."""
    limit    = min(limit, 200)
    interval = _BYBIT_INTERVAL.get(timeframe, 'D')
    params = {"category": "linear", "symbol": symbol,
              "interval": interval, "limit": limit}
    try:
        r = requests.get("https://api.bybit.com/v5/market/kline",
                         params=params, timeout=15)
        if r.status_code != 200:
            print(f"[BYBIT PERPS FAIL] {symbol} HTTP {r.status_code}")
            return None
        data = r.json()
        if data.get('retCode') != 0:
            print(f"[BYBIT PERPS FAIL] {symbol} retCode={data.get('retCode')} {data.get('retMsg')}")
            return None
        rows = data.get('result', {}).get('list', [])
        if not rows:
            print(f"[BYBIT PERPS FAIL] {symbol} empty list")
            return None
        rows = sorted(rows, key=lambda x: int(x[0]))
        df = pd.DataFrame(rows,
                          columns=['ts', 'open', 'high', 'low', 'close', 'volume', 'turnover'])
        df.index = pd.to_datetime(df['ts'].astype(int), unit='ms', utc=True)
        df.index.name = 'timestamp'
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df = df[['open', 'high', 'low', 'close', 'volume']].dropna()
        print(f"[BYBIT PERPS OK] {symbol} {timeframe} rows={len(df)}")
        print(f"[BYBIT PERPS DF] index.dtype={df.index.dtype} tz={df.index.tz}")
        print(df.head(3).to_string())
        return df if not df.empty else None
    except Exception:
        print(f"[BYBIT PERPS FAIL] {symbol} {timeframe}")
        tb.print_exc()
        logger.exception("[BYBIT PERPS FAIL] %s %s", symbol, timeframe)
        return None


# ── MEXC public REST API (마지막 fallback 거래소, API 키 불필요) ────────

_MEXC_SPOT_BASE    = 'https://api.mexc.com'
_MEXC_FUTURES_BASE = 'https://contract.mexc.com'
_MEXC_TIMEOUT      = 10
_MEXC_SPOT_MAX_LIMIT = 500   # 현물 klines 1회 최대 개수

# MEXC 지원 봉만 매핑. 12h 는 MEXC 에 없어서 4h 를 UTC 12h 경계로 집계한다
# (1y 는 기존 거래소와 동일하게 일봉을 받아 월봉으로 리샘플).
_MEXC_SPOT_INTERVAL = {
    '15m': '15m', '1h': '60m', '4h': '4h', '12h': '4h',
    '1d': '1d', '1w': '1W', '1y': '1d',
}
_MEXC_FUTURES_INTERVAL = {
    '15m': 'Min15', '1h': 'Min60', '4h': 'Hour4', '12h': 'Hour4',
    '1d': 'Day1', '1w': 'Week1', '1y': 'Day1',
}
_MEXC_INTERVAL_SEC = {
    '15m': 900, '1h': 3600, '4h': 14400, '12h': 14400,
    '1d': 86400, '1w': 604800, '1y': 86400,
}


def _mexc_get(url: str, params: dict, tag: str) -> 'dict | list | None':
    """MEXC public GET 1회 (재시도 없음). HTTP/네트워크/JSON 오류는 None."""
    try:
        r = requests.get(url, params=params, timeout=_MEXC_TIMEOUT)
        if r.status_code == 429:
            print(f"[{tag} FAIL] rate limited (HTTP 429)")
            return None
        if r.status_code != 200:
            print(f"[{tag} FAIL] HTTP {r.status_code}: {r.text[:200]}")
            return None
        return r.json()
    except Exception as e:
        print(f"[{tag} FAIL] {type(e).__name__}: {e}")
        logger.warning("[%s FAIL] %s", tag, e)
        return None


def _mexc_finish_df(df: pd.DataFrame, timeframe: str) -> 'pd.DataFrame | None':
    """공통 후처리: 숫자 변환 → 12h 집계. 기존 fetch_* 와 동일한 형식 반환."""
    df.index.name = 'timestamp'
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df = df[['open', 'high', 'low', 'close', 'volume']].dropna()
    if timeframe == '12h':
        df = _resample_ohlcv(df, '12h')
    return df if not df.empty else None


def mexc_spot_exists(symbol: str) -> bool:
    """MEXC 현물에서 거래 가능한 페어인지 확인. 없는 심볼은 exchangeInfo 가 빈 목록을 준다."""
    data = _mexc_get(f"{_MEXC_SPOT_BASE}/api/v3/exchangeInfo", {"symbol": symbol}, "MEXC SPOT")
    symbols = data.get('symbols') if isinstance(data, dict) else None
    if not symbols:
        return False
    info = symbols[0]
    return str(info.get('status')) == '1' and bool(info.get('isSpotTradingAllowed'))


def fetch_mexc_spot(symbol: str, timeframe: str, limit: int = 60) -> 'pd.DataFrame | None':
    """MEXC spot kline. 종목 존재 확인 후에만 캔들 요청. Returns ascending DataFrame or None."""
    interval = _MEXC_SPOT_INTERVAL.get(timeframe)
    if interval is None:
        print(f"[MEXC SPOT FAIL] 지원하지 않는 인터벌: {timeframe}")
        return None
    try:
        if not mexc_spot_exists(symbol):
            print(f"[MEXC SPOT] {symbol} 종목 없음")
            return None
        if timeframe == '12h':
            limit = limit * 3
        limit = min(limit, _MEXC_SPOT_MAX_LIMIT)
        rows = _mexc_get(f"{_MEXC_SPOT_BASE}/api/v3/klines",
                         {"symbol": symbol, "interval": interval, "limit": limit}, "MEXC SPOT")
        if not rows or not isinstance(rows, list):
            print(f"[MEXC SPOT FAIL] {symbol} empty/invalid klines")
            return None
        df = pd.DataFrame(
            [[r[0], r[1], r[2], r[3], r[4], r[5]] for r in rows],
            columns=['ts', 'open', 'high', 'low', 'close', 'volume'],
        )
        df.index = pd.to_datetime(df['ts'].astype(int), unit='ms', utc=True)
        df = _mexc_finish_df(df, timeframe)
        if df is not None:
            print(f"[MEXC SPOT OK] {symbol} {timeframe} rows={len(df)}")
        return df
    except Exception:
        print(f"[MEXC SPOT FAIL] {symbol} {timeframe}")
        tb.print_exc()
        logger.exception("[MEXC SPOT FAIL] %s %s", symbol, timeframe)
        return None


def _mexc_contract_symbol(symbol: str) -> str:
    """'ABCUSDT' → 'ABC_USDT'"""
    return f"{symbol[:-4]}_USDT" if symbol.endswith('USDT') else symbol


def _mexc_futures_detail(symbol: str) -> 'dict | None':
    """MEXC USDT 무기한 계약 정보. 없거나 거래 불가(state != 0)면 None."""
    data = _mexc_get(f"{_MEXC_FUTURES_BASE}/api/v1/contract/detail",
                     {"symbol": _mexc_contract_symbol(symbol)}, "MEXC FUTURES")
    if not isinstance(data, dict) or not data.get('success'):
        return None
    detail = data.get('data')
    if not isinstance(detail, dict) or detail.get('state') != 0:
        return None
    return detail


def fetch_mexc_perps(symbol: str, timeframe: str, limit: int = 60) -> 'pd.DataFrame | None':
    """MEXC USDT futures kline. 계약 존재 확인 후에만 캔들 요청. volume 은 코인 수량으로 환산."""
    interval = _MEXC_FUTURES_INTERVAL.get(timeframe)
    if interval is None:
        print(f"[MEXC FUTURES FAIL] 지원하지 않는 인터벌: {timeframe}")
        return None
    try:
        detail = _mexc_futures_detail(symbol)
        if detail is None:
            print(f"[MEXC FUTURES] {symbol} 계약 없음")
            return None
        if timeframe == '12h':
            limit = limit * 3
        end = int(_time.time())
        start = end - (limit + 2) * _MEXC_INTERVAL_SEC[timeframe]
        data = _mexc_get(
            f"{_MEXC_FUTURES_BASE}/api/v1/contract/kline/{_mexc_contract_symbol(symbol)}",
            {"interval": interval, "start": start, "end": end}, "MEXC FUTURES")
        k = data.get('data') if isinstance(data, dict) and data.get('success') else None
        if not isinstance(k, dict) or not k.get('time'):
            print(f"[MEXC FUTURES FAIL] {symbol} empty/invalid klines")
            return None
        try:
            contract_size = float(detail.get('contractSize') or 1)
        except (TypeError, ValueError):
            contract_size = 1.0
        df = pd.DataFrame({
            'open': k['open'], 'high': k['high'], 'low': k['low'],
            'close': k['close'], 'volume': k['vol'],
        })
        df.index = pd.to_datetime(pd.Series(k['time']).astype(int), unit='s', utc=True)
        df = _mexc_finish_df(df, timeframe)
        if df is not None:
            df['volume'] = df['volume'] * contract_size
            print(f"[MEXC FUTURES OK] {symbol} {timeframe} rows={len(df)}")
        return df
    except Exception:
        print(f"[MEXC FUTURES FAIL] {symbol} {timeframe}")
        tb.print_exc()
        logger.exception("[MEXC FUTURES FAIL] %s %s", symbol, timeframe)
        return None


def get_mexc_funding_rate(symbol: str) -> 'float | None':
    """MEXC USDT futures 최근 펀딩비. 실패 시 None."""
    data = _mexc_get(
        f"{_MEXC_FUTURES_BASE}/api/v1/contract/funding_rate/{_mexc_contract_symbol(symbol)}",
        {}, "MEXC FUNDING")
    try:
        return float(data['data']['fundingRate'])
    except (TypeError, KeyError, ValueError):
        return None


# ── 코인 거래소 탐색 (ac/ap 공통): Binance → Bybit → MEXC ───────────────

_SPOT_SOURCES = (
    ('Binance spot', fetch_binance_spot),
    ('Bybit spot',   fetch_bybit_spot),
    ('MEXC spot',    fetch_mexc_spot),
)
_PERPS_SOURCES = (
    ('Binance futures', fetch_binance_futures),
    ('Bybit perps',     fetch_bybit_perps),
    ('MEXC futures',    fetch_mexc_perps),
)


def _fetch_first_available(sources, tag: str, sym: str, timeframe: str,
                           limit: int) -> 'tuple[pd.DataFrame | None, str | None]':
    """우선순위대로 조회해 처음 성공한 (df, 거래소명) 반환. 한 곳이 실패해도 다음으로 진행."""
    for name, fetch in sources:
        try:
            df = fetch(sym, timeframe, limit)
        except Exception as e:
            print(f"[{tag}] {name} 예외: {e}")
            df = None
        if df is not None:
            return df, name
        print(f"[{tag}] {name} None → next")
    return None, None


# ── Korean stocks (Kiwoom REST API + KRX KIND) ────────────────────────

# Market → suffix  (bare 6-digit code + suffix for logging)
_MARKET_SUFFIX: dict = {
    'KOSPI':  '.KS',
    'KOSDAQ': '.KQ',
    'KONEX':  '',
    'ETF':    '.KS',
}


def _normalize_query(text: str) -> str:
    """NFC → remove spaces/special chars → lowercase."""
    text = unicodedata.normalize('NFC', text)
    text = re.sub(r'[\s\-\_\./\(\)\[\]·&,+*%@#$!^~|]', '', text)
    return text.lower()


def normalize_stock_code(code) -> str:
    """종목코드를 6자리 문자열로 정규화. int 변환 절대 금지."""
    raw = str(code).strip()
    digits = re.sub(r'\D', '', raw)   # 숫자만 추출 (비숫자 제거)
    result = digits.zfill(6)
    if raw != result:
        print(f'[NORMALIZE CODE] raw={raw!r} → {result}')
    return result


# ── Kiwoom REST API + KRX KIND 종목 검색 ──────────────────────────────

_KIWOOM_API_BASE = 'https://api.kiwoom.com'
_KRX_STOCK_CACHE: dict = {'items': [], 'by_code': {}, 'by_norm': {}, 'loaded_at': 0.0}
_KR_SEARCH_CACHE: dict = {}
_KR_SEARCH_TTL  = 600   # 검색 결과 캐시 10분
_KRX_CACHE_TTL  = 600   # 종목 리스트 캐시 10분
_KRX_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'kr_stock_cache.json')


def get_kiwoom_access_token() -> 'str | None':
    """키 로딩(env → .env → 키 파일)과 토큰 캐시·재발급은 kiwoom_api 로 일원화."""
    try:
        return kiwoom_api.get_access_token()
    except kiwoom_api.KiwoomError:
        return None


def _kiwoom_headers() -> dict:
    token   = get_kiwoom_access_token()
    app_key, secret = kiwoom_api.get_credentials()
    h = {'Content-Type': 'application/json'}
    if token:
        h['Authorization'] = f'Bearer {token}'
    if app_key:
        h['appkey'] = app_key
    if secret:
        h['secretkey'] = secret
    return h


def _naver_crawl_market(sosok: int, market_name: str) -> list:
    """Naver Finance sise 페이지에서 KOSPI(sosok=0) / KOSDAQ(sosok=1) 전체 크롤링.
    KRX data.krx.co.kr 은 LOGOUT 차단 — Naver Finance 가 유일하게 외부 IP에서 동작."""
    from bs4 import BeautifulSoup
    base = f'https://finance.naver.com/sise/sise_market_sum.naver?sosok={sosok}'
    h   = {'User-Agent': 'Mozilla/5.0'}

    # 첫 페이지 → 마지막 페이지 번호 파악
    try:
        r = requests.get(f'{base}&page=1', headers=h, timeout=15)
        r.encoding = 'euc-kr'
        soup = BeautifulSoup(r.text, 'html.parser')
        pgRR = soup.select_one('td.pgRR a')
        last_page = 1
        if pgRR:
            m = re.search(r'page=(\d+)', pgRR.get('href', ''))
            if m:
                last_page = int(m.group(1))
    except Exception as e:
        print(f'[KRX LOAD] {market_name} 첫 페이지 실패: {e}')
        return []

    print(f'[KRX LOAD] {market_name} last_page={last_page}')
    items = []
    for page in range(1, last_page + 1):
        try:
            r = requests.get(f'{base}&page={page}', headers=h, timeout=15)
            r.encoding = 'euc-kr'
            soup = BeautifulSoup(r.text, 'html.parser')
            for a in soup.select('a[href*="/item/main.naver?code="]'):
                name = a.get_text(strip=True)
                m = re.search(r'code=(\d{6})', a.get('href', ''))
                if m and name:
                    items.append((normalize_stock_code(m.group(1)), name, market_name))
        except Exception as e:
            print(f'[KRX LOAD] {market_name} page={page} 오류: {e}')
    print(f'[KRX LOAD] {market_name}={len(items)}')
    return items


def _load_pykrx_stock_list() -> list:
    """Naver Finance sise 페이지 기반 전체 종목 로딩. Returns [(code, name, market), ...]
    (pykrx OHLCV 는 Naver fchart 사용으로 정상 동작; ticker list 전용 KRX 엔드포인트는 LOGOUT 차단)"""
    kospi  = _naver_crawl_market(0, 'KOSPI')
    kosdaq = _naver_crawl_market(1, 'KOSDAQ')
    items  = kospi + kosdaq
    print(f'[KRX LOAD] kospi={len(kospi)} kosdaq={len(kosdaq)} total={len(items)}')

    if not items:
        print('[KRX LOAD] 전체 종목 로딩 실패')
        return []

    # 첫 5개 샘플
    for code, name, market in items[:5]:
        print(f'[KR SEARCH RESULT] raw_code={code!r} normalized_code={normalize_stock_code(code)} name={name!r} market={market}')
    return items


def _get_krx_stock_cache() -> dict:
    """pykrx 기반 종목 캐시. 10분 TTL, kr_stock_cache.json 영속화."""
    now   = _time.time()
    cache = _KRX_STOCK_CACHE

    # 메모리 캐시가 유효하면 그대로 반환
    if cache['items'] and now - cache['loaded_at'] < _KRX_CACHE_TTL:
        return cache

    # JSON 파일 캐시 확인
    if os.path.exists(_KRX_CACHE_FILE):
        try:
            with open(_KRX_CACHE_FILE, encoding='utf-8') as f:
                saved = _json.load(f)
            if now - saved.get('loaded_at', 0) < _KRX_CACHE_TTL and saved.get('items'):
                items = [tuple(row) for row in saved['items']]
                print(f'[KRX LOAD] JSON 캐시 로드: {len(items)}종목')
                _rebuild_cache(items, now)
                return cache
        except Exception as e:
            print(f'[KRX LOAD] JSON 캐시 읽기 실패: {e}')

    # 새로 로딩
    items = _load_pykrx_stock_list()
    if not items:
        print('[KRX LOAD] 종목 로딩 실패 - 기존 캐시 유지')
        return cache

    # JSON 저장
    try:
        with open(_KRX_CACHE_FILE, 'w', encoding='utf-8') as f:
            _json.dump({'loaded_at': now, 'items': items}, f, ensure_ascii=False)
    except Exception as e:
        print(f'[KRX LOAD] JSON 캐시 저장 실패: {e}')

    _rebuild_cache(items, now)
    return cache


def _rebuild_cache(items: list, loaded_at: float) -> None:
    """items 리스트로 by_code / by_norm 인덱스 재구성."""
    by_code: dict = {}
    by_norm: dict = {}
    for code, name, market in items:
        entry = {'code': code, 'name': name, 'market': market}
        by_code[code] = entry
        nk = _normalize_query(name)
        if nk not in by_norm:
            by_norm[nk] = entry
    _KRX_STOCK_CACHE.update({
        'items': items, 'by_code': by_code,
        'by_norm': by_norm, 'loaded_at': loaded_at,
    })


def kiwoom_search_stock(query: str) -> 'dict | None':
    """
    pykrx 기반 종목 검색 (KOSPI/KOSDAQ/ETF).
    Returns {'code': '328130', 'name': '루닛', 'market': 'KOSDAQ'} or None.
    Cache: 10분.
    """
    norm = _normalize_query(query)
    now  = _time.time()

    if norm in _KR_SEARCH_CACHE:
        cached, ts = _KR_SEARCH_CACHE[norm]
        if now - ts < _KR_SEARCH_TTL:
            print(f'[KIWOOM SEARCH] cache hit: query={query!r} → {cached}')
            return cached

    print(f'[KIWOOM SEARCH] query={query!r} norm={norm!r}')
    cache = _get_krx_stock_cache()

    # 1. 6자리 코드 직접 조회
    if re.fullmatch(r'\d{1,6}', query.strip()):
        code_key = normalize_stock_code(query.strip())
        if code_key in cache['by_code']:
            result = dict(cache['by_code'][code_key])
            result['code'] = normalize_stock_code(result['code'])
            print(f'[KR SEARCH RESULT] raw_code={query.strip()!r} normalized_code={result["code"]}')
            print(f'[KIWOOM SEARCH] code exact: {result}')
            _KR_SEARCH_CACHE[norm] = (result, now)
            return result

    # 2. 정규화 정확 매칭
    if norm in cache['by_norm']:
        result = dict(cache['by_norm'][norm])
        result['code'] = normalize_stock_code(result['code'])
        print(f'[KR SEARCH RESULT] raw_code={result["code"]!r} normalized_code={result["code"]}')
        print(f'[KIWOOM SEARCH] norm exact: {result}')
        _KR_SEARCH_CACHE[norm] = (result, now)
        return result

    # 3. 부분 일치 (norm 포함, 이름 짧은 것 우선)
    matches = [
        entry for entry in cache['by_code'].values()
        if norm in _normalize_query(entry['name'])
    ]
    if matches:
        matches.sort(key=lambda x: len(x['name']))
        result = dict(matches[0])
        result['code'] = normalize_stock_code(result['code'])
        print(f'[KR SEARCH RESULT] raw_code={matches[0]["code"]!r} normalized_code={result["code"]}')
        print(f'[KIWOOM SEARCH] contains match ({len(matches)} hits): {result}')
        _KR_SEARCH_CACHE[norm] = (result, now)
        return result

    print(f'[KIWOOM SEARCH] not found: query={query!r}')
    _KR_SEARCH_CACHE[norm] = (None, now)
    return None


def kiwoom_get_price(code: str) -> 'dict | None':
    """Kiwoom REST API 현재가 조회. Returns dict or None."""
    code = normalize_stock_code(code)
    headers = _kiwoom_headers()
    url = f'{_KIWOOM_API_BASE}/v1/stock/price'
    payload = {'stk_cd': code}
    print(f'[KIWOOM REQUEST] POST {url} payload={payload}')
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        print(f'[KIWOOM RESPONSE] status={r.status_code} body={r.text[:300]}')
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception as e:
        print(f'[KIWOOM REQUEST] 오류: {e}')
        return None

    if isinstance(data, list):
        data = data[0] if data else {}
    if isinstance(data, dict) and 'data' in data:
        inner = data['data']
        if isinstance(inner, list):
            data = inner[0] if inner else {}
        elif isinstance(inner, dict):
            data = inner

    def _to_int(*keys) -> int:
        for k in keys:
            v = data.get(k)
            if v is None:
                continue
            s = str(v).replace(',', '').replace('+', '').strip()
            try:
                return int(float(s))
            except (ValueError, TypeError):
                pass
        return 0

    def _to_float(*keys) -> float:
        for k in keys:
            v = data.get(k)
            if v is None:
                continue
            s = str(v).replace(',', '').replace('%', '').replace('+', '').strip()
            try:
                return float(s)
            except (ValueError, TypeError):
                pass
        return 0.0

    price      = _to_int('cur_pric', 'cur_prc', 'price', 'close', 'stck_prpr', 'current_price', 'clos_pric')
    change     = _to_int('bfdy_vrss', 'prdy_vrss', 'prv_diff', 'change', 'diff', 'price_change')
    change_pct = _to_float('bfdy_ctrt', 'prdy_ctrt', 'diff_rt', 'change_pct', 'rate', 'change_rate')
    volume     = _to_int('acml_vol', 'trde_vol', 'volume', 'acc_volume')
    open_p     = _to_int('open_pric', 'stck_oprc', 'open')
    high_p     = _to_int('hgst_pric', 'stck_hgpr', 'high_price', 'high')
    low_p      = _to_int('lwst_pric', 'stck_lwpr', 'low_price', 'low')

    sign = str(data.get('prdy_vrss_sign', data.get('bfdy_vrss_sign', data.get('sign', ''))) or '')
    if sign in ('2', '하락', 'FALL') or change_pct < 0:
        change     = -abs(change)
        change_pct = -abs(change_pct)

    print(f'[KIWOOM RESPONSE] code={code} price={price:,} change_pct={change_pct:+.2f}% volume={volume:,}')
    return {
        'price':      price,
        'change':     change,
        'change_pct': change_pct,
        'volume':     volume,
        'open':       open_p,
        'high':       high_p,
        'low':        low_p,
    }


def _fetch_naver_ohlcv(ticker: str, count: int = 365) -> pd.DataFrame:
    """Naver fchart XML 파싱 → OHLCV DataFrame (UTC index)."""
    import lxml.etree as ET
    ticker = normalize_stock_code(ticker)
    url = (f'https://fchart.stock.naver.com/sise.nhn'
           f'?symbol={ticker}&timeframe=day&count={count}&requestType=0')
    try:
        r = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        # BOM 및 XML 선언 앞 공백 제거 → "XML declaration allowed only at the start" 방지
        raw = r.content
        if raw.startswith(b'\xef\xbb\xbf'):   # UTF-8 BOM
            raw = raw[3:]
        raw = raw.strip()
        root = ET.fromstring(raw)
        rows = []
        for item in root.iter('item'):
            d = item.get('data', '')
            parts = d.split('|')
            if len(parts) < 5:
                continue
            try:
                rows.append({
                    'date':   parts[0].strip(),
                    'open':   float(parts[1]),
                    'high':   float(parts[2]),
                    'low':    float(parts[3]),
                    'close':  float(parts[4]),
                    'volume': float(parts[5]) if len(parts) > 5 else 0.0,
                })
            except (ValueError, IndexError):
                pass
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df['dt'] = pd.to_datetime(df['date'], format='%Y%m%d', errors='coerce')
        df = df.dropna(subset=['dt']).sort_values('dt')
        df.index = df['dt'].dt.tz_localize('Asia/Seoul').dt.tz_convert('UTC')
        return df[['open', 'high', 'low', 'close', 'volume']].copy()
    except Exception as e:
        print(f'[NAVER OHLCV] 오류: {e}')
        return pd.DataFrame()


def kiwoom_get_chart_ohlcv(code: str, timeframe: str = '1d') -> pd.DataFrame:
    """
    pykrx (primary) + Naver fchart (fallback) OHLCV DataFrame.
    timeframe: 1h/4h/12h → 일봉 대체, 1d / 1w / 1y 지원.
    Returns DataFrame [open, high, low, close, volume], UTC DatetimeIndex.
    """
    from pykrx import stock as pykrx_stock
    code = normalize_stock_code(code)   # int 변환 방지 + 항상 6자리 문자열

    # 분봉은 pykrx 미지원 → 일봉으로 대체
    fetch_tf = '1d' if timeframe in ('1h', '4h', '12h') else timeframe

    today_str = datetime.now().strftime('%Y%m%d')
    if fetch_tf == '1d':
        from_date = (datetime.now() - timedelta(days=500)).strftime('%Y%m%d')
    elif fetch_tf == '1w':
        from_date = (datetime.now() - timedelta(days=365 * 3)).strftime('%Y%m%d')
    else:  # 1y → 월봉 대체
        from_date = (datetime.now() - timedelta(days=365 * 10)).strftime('%Y%m%d')

    print(f'[KIWOOM CHART] pykrx code={code} fetch_tf={fetch_tf} from={from_date}')
    df = pd.DataFrame()
    try:
        print(f'[PYKRX CALL] ticker={code}')
        df = pykrx_stock.get_market_ohlcv_by_date(from_date, today_str, code)
    except Exception as e:
        print(f'[KIWOOM CHART] pykrx 오류: {e}')

    if df is None or df.empty:
        print(f'[KIWOOM CHART] pykrx 실패 → Naver fchart fallback code={code}')
        df = _fetch_naver_ohlcv(code, count=500)

    if df is None or df.empty:
        raise ValueError(f'차트 데이터 없음: code={code}')

    # pykrx 컬럼 한글 → 영문 정규화
    col_map = {'시가': 'open', '고가': 'high', '저가': 'low', '종가': 'close', '거래량': 'volume'}
    df = df.rename(columns=col_map)
    for col in ('open', 'high', 'low', 'close', 'volume'):
        if col not in df.columns:
            df[col] = 0.0

    # pykrx 인덱스(DatetimeIndex 또는 date)를 UTC 변환
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    if df.index.tz is None:
        try:
            df.index = df.index.tz_localize('Asia/Seoul').tz_convert('UTC')
        except Exception:
            df.index = df.index.tz_convert('UTC')

    df = df.sort_index()
    df = df[['open', 'high', 'low', 'close', 'volume']].copy()
    df = df.dropna(subset=['open', 'close'])
    df = df[df['close'] > 0]

    print(f'[KIWOOM CHART OK] code={code} tf={timeframe} rows={len(df)}')
    return df



def find_kr_stock(query: str):
    """
    KRX KIND 기반 한국 주식/ETF 검색.
    Returns (ticker_with_suffix, name) on unique match.
    Returns (None, []) on failure.
    """
    query_raw  = query.strip()
    query_norm = _normalize_query(query_raw)
    print(f"[KR SEARCH] query={query_raw!r} normalized={query_norm!r}")

    result = kiwoom_search_stock(query_raw)
    if result and result.get('code') and result.get('name'):
        code   = result['code']
        name   = result['name']
        market = result.get('market', '')
        suffix = _MARKET_SUFFIX.get(market, '')
        ticker_out = f"{code}{suffix}"
        print(f"[KR SEARCH] matched={name!r} code={code} market={market} ticker={ticker_out}")
        return ticker_out, name

    print(f"[KR SEARCH] query={query_raw!r} matched=None")
    return None, []


def search_kr_stock(query: str) -> dict:
    """
    Public API for Korean stock/ETF search.
    Success  → {'name': str, 'code': str, 'market': str, 'ticker': str}
    Not found→ {}
    """
    ticker, result = find_kr_stock(query)
    if ticker:
        bare   = _bare_kr_ticker(ticker)
        kw     = kiwoom_search_stock(query)
        market = kw.get('market', '') if kw else ''
        name   = kw.get('name', result) if kw else str(result)
        return {'name': name, 'code': bare, 'market': market, 'ticker': ticker}
    return {}


def is_kr_ticker(ticker: str) -> bool:
    """True when ticker is a KRX stock: bare 6-digit code or 6-digit.KS / .KQ"""
    bare = ticker.split('.')[0]
    return bare.isdigit() and len(bare) == 6


def _bare_kr_ticker(ticker: str) -> str:
    """Strip .KS / .KQ suffix → bare 6-digit pykrx code."""
    return ticker.split('.')[0]



def create_kr_stock_chart(ticker: str, name: str, timeframe: str = '1d'):
    """
    kiwoom-cli 기반 한국 주식 캔들 차트.
    1d/1w/1y : 해당 봉 그대로 사용.
    1h/4h/12h: kiwoom이 분봉 지원 → 분봉 차트 표시 (불가 시 일봉 대체).
    Returns (file_path, caption) or raises.
    """
    bare = normalize_stock_code(_bare_kr_ticker(ticker))
    print(f'[AK CHART] ticker={bare} name={name!r} timeframe={timeframe}')

    # ── OHLCV ──────────────────────────────────────────────────────────
    df_raw = None
    effective_tf = timeframe
    try:
        df_raw = kiwoom_get_chart_ohlcv(bare, timeframe)
    except Exception as e:
        print(f"[KR CHART] {timeframe} 차트 실패: {e}")
        if timeframe in ('1h', '4h', '12h'):
            print(f"[KR CHART] 분봉 실패 → 일봉 대체")
            effective_tf = '1d'
            df_raw = kiwoom_get_chart_ohlcv(bare, '1d')
        else:
            raise

    if df_raw is None or df_raw.empty:
        raise ValueError(f"차트 데이터 없음: {bare}")

    intraday_fallback = (timeframe in ('1h', '4h', '12h') and effective_tf == '1d')

    # 52주 최고/최저는 일봉 기준 (분봉/주봉/월봉이어도 일봉으로 별도 조회)
    try:
        df_1d = kiwoom_get_chart_ohlcv(bare, '1d') if effective_tf != '1d' else df_raw
    except Exception:
        df_1d = df_raw
    high_52w = float(df_1d.tail(252)['high'].max())
    low_52w  = float(df_1d.tail(252)['low'].min())

    # 봉 선택: 최근 60봉
    if effective_tf == '1w':
        df = _resample_ohlcv(df_raw, 'W').tail(60)
    elif effective_tf == '1y':
        df = _to_monthly(df_raw)
    else:
        df = df_raw.tail(60)

    if df.empty:
        raise ValueError(f"차트 데이터 없음: {bare}")

    # ── 현재가 ─────────────────────────────────────────────────────────
    kp = kiwoom_get_price(bare)
    if kp and kp['price'] > 0:
        current_price = float(kp['price'])
        change_pct    = kp['change_pct']
        change_amt    = kp['change']
        volume        = kp['volume']
        market        = kp.get('market', '')
        print(f"[KR CHART] 키움 현재가: ₩{current_price:,.0f} ({change_pct:+.2f}%)")
    else:
        current_price = float(df['close'].iloc[-1])
        prev_price    = float(df['close'].iloc[-2]) if len(df) >= 2 else current_price
        change_amt    = int(round(current_price - prev_price))
        change_pct    = (current_price / prev_price - 1) * 100 if prev_price else 0.0
        volume        = int(df_raw['volume'].iloc[-1]) if 'volume' in df_raw.columns else 0
        market        = ''
        print(f"[KR CHART] 차트 마지막 종가 fallback: ₩{current_price:,.0f}")

    # 시장 정보 kiwoom 검색 결과에서도 보완
    if not market:
        kw = kiwoom_search_stock(name) or kiwoom_search_stock(bare)
        if kw:
            market = kw.get('market', '')

    tf_label = _TIMEFRAME_LABEL.get(timeframe, timeframe.upper())
    if intraday_fallback:
        title_tf   = f"1D (요청: {tf_label})"
        caption_tf = f"1D ⚠️ (한국주식 {tf_label} → 일봉 대체)"
    else:
        title_tf   = tf_label
        caption_tf = tf_label

    sign_emoji = '📈' if change_pct >= 0 else '📉'
    pct_str    = f"{change_pct:+.2f}%"
    amt_sign   = '+' if change_amt >= 0 else ''
    amt_str    = f"{amt_sign}{change_amt:,}"

    title    = f"{name} ({bare}) - {title_tf} - KRX"
    tmp_path = _make_tmp_path()
    _draw_chart(df, title, effective_tf, tmp_path)

    lines = [
        f"{sign_emoji} {name} ({bare}) 차트",
        f"🕒 Timeframe: {caption_tf}\n",
        f"💰 현재가: ₩{_fmt_kr(current_price)}",
        f"📊 등락률: {pct_str} ({amt_str}원)",
    ]
    if volume > 0:
        lines.append(f"📦 거래량: {volume:,}")
    lines.append(f"\n52주 최고: ₩{_fmt_kr(high_52w)}")
    lines.append(f"52주 최저: ₩{_fmt_kr(low_52w)}")
    if market:
        lines.append(f"\n🏢 시장: {market}")

    caption = '\n'.join(lines)
    return tmp_path, caption


# ── Chart drawing ──────────────────────────────────────────────────────

def _make_tmp_path() -> str:
    tmp = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
    tmp.close()
    return tmp.name


def _draw_chart(df: pd.DataFrame, title: str, timeframe: str, tmp_path: str) -> None:
    """TradingView-dark candlestick chart. Shows last 60 candles."""
    print(f"[DRAW] enter title='{title}' timeframe={timeframe}")
    print(f"[DRAW] input shape={df.shape} index.dtype={df.index.dtype} tz={df.index.tz}")
    print(f"[DRAW] df.head:\n{df.head(3).to_string()}")
    df = df.tail(60).copy()
    timestamps = df.index.tolist()
    df = df.reset_index(drop=True)
    n  = len(df)

    UP_COLOR   = '#26a69a'
    DOWN_COLOR = '#ef5350'
    BG_COLOR   = '#111722'
    GRID_COLOR = '#1e2535'
    TEXT_COLOR = '#c7c7c7'
    PRICE_LINE = '#f0c040'
    HIGH_COLOR = '#ef5350'
    LOW_COLOR  = '#5090f0'

    fig, ax = plt.subplots(figsize=(14, 9), dpi=100)
    fig.patch.set_facecolor(BG_COLOR)
    ax.set_facecolor(BG_COLOR)

    candle_width = 0.6
    for i in range(n):
        o, h, l, c = df.at[i, 'open'], df.at[i, 'high'], df.at[i, 'low'], df.at[i, 'close']
        color = UP_COLOR if c >= o else DOWN_COLOR
        ax.plot([i, i], [l, h], color=color, linewidth=0.8, zorder=1)
        body_bottom = min(o, c)
        body_height = max(abs(c - o), (h - l) * 0.001)
        ax.add_patch(mpatches.Rectangle(
            (i - candle_width / 2, body_bottom),
            candle_width, body_height,
            linewidth=0, facecolor=color, zorder=2,
        ))

    price_low    = df['low'].min()
    price_high   = df['high'].max()
    price_range  = max(price_high - price_low, price_high * 1e-6)
    arrow_offset = price_range * 0.08

    high_idx = int(df['high'].idxmax())
    low_idx  = int(df['low'].idxmin())
    high_val = df.at[high_idx, 'high']
    low_val  = df.at[low_idx,  'low']

    if high_idx < n * 0.25:
        h_xytext = (min(high_idx + max(int(n * 0.10), 3), n - 1), high_val + arrow_offset)
        h_ha = 'left'
    else:
        h_xytext = (high_idx, high_val + arrow_offset)
        h_ha = 'center'

    ax.annotate(
        format_price(high_val),
        xy=(high_idx, high_val), xytext=h_xytext,
        arrowprops=dict(arrowstyle='->', color=HIGH_COLOR, lw=1.0, shrinkA=2, shrinkB=2),
        fontsize=16, color=HIGH_COLOR, fontweight='bold',
        va='bottom', ha=h_ha, zorder=6, clip_on=False,
    )
    ax.annotate(
        format_price(low_val),
        xy=(low_idx, low_val), xytext=(low_idx, low_val - arrow_offset),
        arrowprops=dict(arrowstyle='->', color=LOW_COLOR, lw=1.0, shrinkA=2, shrinkB=2),
        fontsize=16, color=LOW_COLOR, fontweight='bold',
        va='top', ha='center', zorder=6, clip_on=False,
    )

    pad = price_range * 0.10
    ax.set_ylim(price_low - pad, price_high + pad)
    ax.set_xlim(-1, n)

    ax.yaxis.tick_right()
    ax.yaxis.set_label_position('right')
    ax.tick_params(axis='y', labelright=True, labelleft=False,
                   colors=TEXT_COLOR, labelsize=15)
    ax.yaxis.set_major_formatter(
        mticker.FuncFormatter(lambda v, _: format_price(v))
    )

    date_fmt = _TIMEFRAME_DATE_FMT.get(timeframe, '%m/%d')
    step = max(1, n // 8)
    tick_positions = list(range(0, n, step))
    if (n - 1) not in tick_positions:
        tick_positions.append(n - 1)

    tick_labels = []
    for pos in tick_positions:
        if pos < len(timestamps):
            ts = timestamps[pos]
            try:
                ts_local = ts.tz_convert('Asia/Seoul')
            except Exception:
                ts_local = ts
            tick_labels.append(ts_local.strftime(date_fmt))
        else:
            tick_labels.append('')

    ax.set_xticks(tick_positions)
    ax.set_xticklabels(tick_labels, rotation=0, ha='center',
                       fontsize=8, color=TEXT_COLOR)

    ax.grid(True, color=GRID_COLOR, linewidth=0.5, linestyle='--', zorder=0)
    ax.set_axisbelow(True)

    current_price = df['close'].iloc[-1]
    ax.axhline(current_price, linestyle='--', linewidth=0.9,
               color=PRICE_LINE, alpha=0.9, zorder=3)
    ax.text(
        1.002, current_price,
        f" {format_price(current_price)}",
        transform=ax.get_yaxis_transform(),
        va='center', ha='left', fontsize=15, color=PRICE_LINE,
        bbox=dict(boxstyle='round,pad=0.25', facecolor='#1c2333', edgecolor='none'),
        clip_on=False,
    )
    ax.text(
        0.01, 0.97, title,
        transform=ax.transAxes, va='top', ha='left',
        fontsize=24, color='white', fontweight='bold', zorder=7,
        bbox=dict(facecolor='#111722', alpha=0.85, edgecolor='none', pad=5),
    )
    for spine in ax.spines.values():
        spine.set_edgecolor(GRID_COLOR)

    plt.tight_layout(pad=0.5)
    fig.savefig(tmp_path, dpi=100, bbox_inches='tight', facecolor=BG_COLOR)
    plt.close(fig)



# ── Korean exchange price fetchers ────────────────────────────────────

def fetch_upbit_ticker(symbol: str) -> 'float | None':
    """Upbit KRW 현재가. symbol='BTC' → market 'KRW-BTC'. Returns float or None."""
    try:
        market = f"KRW-{symbol.upper()}"
        r = requests.get(
            'https://api.upbit.com/v1/ticker',
            params={'markets': market},
            headers={'Accept': 'application/json'},
            timeout=8,
        )
        if r.status_code != 200:
            logger.debug("[UPBIT] %s HTTP %d", market, r.status_code)
            return None
        data = r.json()
        if not data:
            return None
        price = float(data[0]['trade_price'])
        logger.info("[UPBIT] %s=₩%s", symbol, f"{int(price):,}")
        return price
    except Exception as e:
        logger.debug("[UPBIT] %s error: %s", symbol, e)
        return None


def fetch_bithumb_ticker(symbol: str) -> 'float | None':
    """Bithumb KRW 현재가. symbol='BTC' → BTC_KRW ticker. Returns float or None."""
    try:
        r = requests.get(
            f'https://api.bithumb.com/public/ticker/{symbol.upper()}_KRW',
            headers={'Accept': 'application/json'},
            timeout=8,
        )
        if r.status_code != 200:
            logger.debug("[BITHUMB] %s HTTP %d", symbol, r.status_code)
            return None
        data = r.json()
        if data.get('status') != '0000':
            logger.debug("[BITHUMB] %s status=%s", symbol, data.get('status'))
            return None
        price = float(data['data']['closing_price'])
        logger.info("[BITHUMB] %s=₩%s", symbol, f"{int(price):,}")
        return price
    except Exception as e:
        logger.debug("[BITHUMB] %s error: %s", symbol, e)
        return None


# ── Public chart functions ─────────────────────────────────────────────

def create_clean_candlestick_chart(symbol: str, timeframe: str = '1d') -> dict:
    """/ac: Binance spot → Bybit spot → MEXC spot fallback"""
    sym = _parse_symbol(symbol)
    result = {
        'success': False, 'file_path': None, 'current_price': None,
        'symbol': sym, 'timeframe': timeframe, 'exchange': None,
        'error': None, 'currency': '$', 'caption': '',
    }
    try:
        limit = _FETCH_LIMIT.get(timeframe, 60)
        print(f"[AC] sym={sym} timeframe={timeframe} limit={limit}")

        df, source = _fetch_first_available(_SPOT_SOURCES, 'AC', sym, timeframe, limit)

        if df is None:
            print(f"[AC] All sources returned None for {sym} {timeframe}")
            result['error'] = (
                f"코인 데이터를 가져올 수 없습니다: {sym}\n"
                "Binance / Bybit / MEXC 모두 실패 — 서버 로그를 확인하세요."
            )
            return result

        print(f"[AC] fetch OK source={source} shape={df.shape}")
        print(f"[AC] index.dtype={df.index.dtype}  tz={df.index.tz}")
        print(f"[AC] df.head:\n{df.head(3).to_string()}")

        df_raw = df.copy()

        if timeframe == '1y':
            print(f"[AC] resampling to monthly ...")
            df = _to_monthly(df)
            print(f"[AC] after resample shape={df.shape}")

        if df is None or df.empty:
            result['error'] = f"데이터 리샘플링 실패: {sym}"
            return result

        result['exchange'] = source
        current_price = float(df['close'].iloc[-1])
        prev_price    = float(df['close'].iloc[-2]) if len(df) >= 2 else current_price
        result['current_price'] = current_price

        # 52-week high/low
        if timeframe in ('1d', '1y'):
            df_52w   = df_raw.tail(365)
            high_52w = float(df_52w['high'].max())
            low_52w  = float(df_52w['low'].min())
        elif timeframe == '1w':
            high_52w = float(df_raw['high'].max())
            low_52w  = float(df_raw['low'].min())
        else:
            df_1d, _ = _fetch_first_available(_SPOT_SOURCES, 'AC', sym, '1d', 365)
            high_52w = float(df_1d['high'].max()) if df_1d is not None else None
            low_52w  = float(df_1d['low'].min())  if df_1d is not None else None

        label    = _TIMEFRAME_LABEL.get(timeframe, timeframe.upper())
        title    = f"{sym} - {label} - {source}"
        tmp_path = _make_tmp_path()
        print(f"[AC] calling _draw_chart title='{title}' tmp={tmp_path}")
        _draw_chart(df, title, timeframe, tmp_path)
        print(f"[AC] _draw_chart done")

        lines = [
            f"📊 {sym} 차트",
            f"🕒 Timeframe: {label}\n",
            f"현재가: {format_price(current_price)} USDT",
            f"전일대비: {_change_line(current_price, prev_price)}",
        ]
        if high_52w is not None:
            lines.append(f"52주 최고가: {format_price(high_52w)} USDT")
        if low_52w is not None:
            lines.append(f"52주 최저가: {format_price(low_52w)} USDT")

        result['success']   = True
        result['file_path'] = tmp_path
        result['caption']   = '\n'.join(lines)

    except Exception:
        tb.print_exc()
        logger.exception("create_clean_candlestick_chart 오류 %s %s", sym, timeframe)
        result['error'] = "차트 생성 중 오류가 발생했습니다. 서버 로그를 확인하세요."
        plt.close('all')

    return result


def create_perps_chart(symbol: str, timeframe: str = '1d') -> dict:
    """/ap: Binance futures → Bybit perps → MEXC futures fallback"""
    sym = _parse_symbol(symbol)
    result = {
        'success': False, 'file_path': None, 'current_price': None,
        'symbol': sym, 'timeframe': timeframe, 'exchange': None,
        'error': None, 'currency': '$', 'caption': '',
    }
    try:
        limit = _FETCH_LIMIT.get(timeframe, 60)

        df, source = _fetch_first_available(_PERPS_SOURCES, 'AP', sym, timeframe, limit)

        if df is None:
            result['error'] = (
                f"선물 데이터를 가져올 수 없습니다: {sym}\n"
                "Binance futures / Bybit perps / MEXC futures 모두 실패 — 서버 로그를 확인하세요."
            )
            return result

        df_raw = df.copy()

        if timeframe == '1y':
            df = _to_monthly(df)

        if df is None or df.empty:
            result['error'] = f"데이터 리샘플링 실패: {sym}"
            return result

        result['exchange'] = source
        current_price = float(df['close'].iloc[-1])
        prev_price    = float(df['close'].iloc[-2]) if len(df) >= 2 else current_price
        result['current_price'] = current_price

        if timeframe in ('1d', '1y'):
            df_52w   = df_raw.tail(365)
            high_52w = float(df_52w['high'].max())
            low_52w  = float(df_52w['low'].min())
        elif timeframe == '1w':
            high_52w = float(df_raw['high'].max())
            low_52w  = float(df_raw['low'].min())
        else:
            df_1d, _ = _fetch_first_available(_PERPS_SOURCES, 'AP', sym, '1d', 365)
            high_52w = float(df_1d['high'].max()) if df_1d is not None else None
            low_52w  = float(df_1d['low'].min())  if df_1d is not None else None

        funding = get_mexc_funding_rate(sym) if source == 'MEXC futures' else get_funding_rate(sym)

        label    = _TIMEFRAME_LABEL.get(timeframe, timeframe.upper())
        title    = f"{sym} PERPS - {label} - {source}"
        tmp_path = _make_tmp_path()
        print(f"[AP] calling _draw_chart title='{title}'")
        _draw_chart(df, title, timeframe, tmp_path)
        print(f"[AP] _draw_chart done")

        lines = [
            f"📊 {sym} 선물 차트",
            f"🕒 Timeframe: {label}\n",
            f"현재가: {format_price(current_price)} USDT",
            f"전일대비: {_change_line(current_price, prev_price)}",
        ]
        if high_52w is not None:
            lines.append(f"52주 최고가: {format_price(high_52w)} USDT")
        if low_52w is not None:
            lines.append(f"52주 최저가: {format_price(low_52w)} USDT")
        if funding is not None:
            lines.append(f"펀딩비: {funding * 100:.4f}%")

        result['success']   = True
        result['file_path'] = tmp_path
        result['caption']   = '\n'.join(lines)

    except Exception:
        tb.print_exc()
        logger.exception("create_perps_chart 오류 %s %s", sym, timeframe)
        result['error'] = "선물 차트 생성 중 오류가 발생했습니다. 서버 로그를 확인하세요."
        plt.close('all')

    return result


# ── /au, /ak 국내주식: 키움증권 REST API ────────────────────────────────

# 봉 종류: 1d/1w/1y 는 API 그대로, 15m/1h 는 분봉(tic_scope) 그대로, 4h/12h 는 60분봉을 KST 경계로 집계
_KIWOOM_KIND         = {'1d': 'day', '1w': 'week', '1y': 'month'}
_KIWOOM_MINUTE_SCOPE = {'15m': '15', '1h': '60', '4h': '60', '12h': '60'}
_KIWOOM_MIN_ROWS     = {'1d': 300, '1w': 60, '1y': 60, '15m': 60, '1h': 60, '4h': 240, '12h': 240}

_KIWOOM_MSG_NOT_FOUND    = "국내주식 종목을 찾을 수 없습니다: {query}"
_KIWOOM_MSG_US_NOT_FOUND = "해외주식 종목을 찾을 수 없습니다: {query}"
_KIWOOM_MSG_AUTH         = "키움증권 API 인증에 실패했습니다. 서버 로그를 확인해주세요."
_KIWOOM_MSG_DATA         = "키움증권에서 차트 데이터를 가져오지 못했습니다."


def _fetch_kiwoom_kr_df(code: str, timeframe: str) -> pd.DataFrame:
    if timeframe in _KIWOOM_KIND:
        df = kiwoom_api.fetch_ohlcv(code, _KIWOOM_KIND[timeframe], min_rows=_KIWOOM_MIN_ROWS[timeframe])
    else:
        df = kiwoom_api.fetch_ohlcv(code, 'minute', tic_scope=_KIWOOM_MINUTE_SCOPE[timeframe],
                                    min_rows=_KIWOOM_MIN_ROWS[timeframe])
        if timeframe in ('4h', '12h'):
            df = _resample_ohlcv(df, timeframe)
    return df


def create_kiwoom_kr_stock_chart(query: str, timeframe: str = '1d', log_tag: str = 'AU') -> dict:
    """/au, /ak 국내주식: 종목명/6자리 코드 → 키움 종목 검색 → 키움 REST OHLCV → 기존 _draw_chart.
    yfinance 는 호출하지 않는다."""
    query = query.strip()
    result = {
        'success': False, 'file_path': None, 'current_price': None,
        'symbol': query, 'timeframe': timeframe, 'exchange': 'KIWOOM',
        'error': None, 'currency': '₩', 'caption': '',
    }
    logger.info("[%s] input=%s", log_tag, query)

    if timeframe not in VALID_INTERVALS:
        result['error'] = (
            f"지원하지 않는 인터벌: {timeframe}\n"
            f"지원 인터벌: 15m / 1h / 4h / 12h / 1d / 1w / 1y"
        )
        return result

    try:
        stock = kiwoom_api.search_stock(query)
        if stock is None:
            logger.info("[%s] Korean stock not found: %s", log_tag, query)
            result['error'] = _KIWOOM_MSG_NOT_FOUND.format(query=query)
            return result
        code, name = stock['code'], stock['name']
        result['symbol'] = code
        logger.info("[%s] Korean stock found: %s (%s)", log_tag, name, code)
        logger.info("[%s] source=KIWOOM", log_tag)

        df_full = _fetch_kiwoom_kr_df(code, timeframe)
        df = df_full.tail(60)

        current_price = float(df['close'].iloc[-1])
        prev_price    = float(df['close'].iloc[-2]) if len(df) >= 2 else current_price
        result['current_price'] = current_price

        # 52주 최고/최저는 일봉 기준
        high_52w = low_52w = None
        try:
            df_1d = df_full if timeframe == '1d' else _fetch_kiwoom_kr_df(code, '1d')
            year  = df_1d.tail(252)
            high_52w, low_52w = float(year['high'].max()), float(year['low'].min())
        except kiwoom_api.KiwoomError as e:
            logger.warning("[%s] 52주 일봉 조회 실패(생략): %s", log_tag, e)

        label    = _TIMEFRAME_LABEL.get(timeframe, timeframe.upper())
        title    = (f"{name} ({code}) - {label} - Kiwoom" if _HAS_KOREAN_FONT
                    else f"{code} - {label} - Kiwoom")     # 한글 폰트 없는 서버에서 글자 깨짐 방지
        tmp_path = _make_tmp_path()
        _draw_chart(df, title, timeframe, tmp_path)

        lines = [
            f"📊 {name} ({code}) 차트",
            f"🕒 Timeframe: {label}\n",
            f"현재가: {_fmt_kr(current_price)}원",
            f"전일대비: {_change_line(current_price, prev_price, _fmt_kr)}",
        ]
        if high_52w is not None:
            lines.append(f"52주 최고가: {_fmt_kr(high_52w)}원")
        if low_52w is not None:
            lines.append(f"52주 최저가: {_fmt_kr(low_52w)}원")
        if stock.get('market_name'):
            lines.append(f"\n🏢 시장: {stock['market_name']}")

        result['success']   = True
        result['file_path'] = tmp_path
        result['caption']   = '\n'.join(lines)
        logger.info("[%s] chart generated successfully", log_tag)

    except kiwoom_api.KiwoomAuthError as e:
        logger.error("[%s] Kiwoom auth error: %s", log_tag, e)
        result['error'] = _KIWOOM_MSG_AUTH
    except kiwoom_api.KiwoomError as e:
        logger.error("[%s] Kiwoom data error: %s", log_tag, e)
        result['error'] = _KIWOOM_MSG_DATA
    except Exception:
        logger.error("create_kiwoom_kr_stock_chart 오류:\n%s", tb.format_exc())
        result['error'] = _KIWOOM_MSG_DATA
        plt.close('all')

    return result


# ── /au 해외(미국)주식: 키움증권 REST API (yfinance 미사용) ─────────────

_KIWOOM_US_KIND         = {'1d': 'day', '1w': 'week', '1y': 'month'}
_KIWOOM_US_MINUTE_SCOPE = {'15m': '15', '1h': '60', '4h': '60', '12h': '60'}
_KIWOOM_US_MIN_ROWS     = {'1d': 300, '1w': 60, '1y': 60, '15m': 60, '1h': 60, '4h': 240, '12h': 240}


def _fetch_kiwoom_us_df(symbol: str, exchange: str, timeframe: str) -> pd.DataFrame:
    if timeframe in _KIWOOM_US_KIND:
        df = kiwoom_api.fetch_us_ohlcv(symbol, exchange, _KIWOOM_US_KIND[timeframe],
                                       min_rows=_KIWOOM_US_MIN_ROWS[timeframe])
    else:
        df = kiwoom_api.fetch_us_ohlcv(symbol, exchange, 'minute',
                                       tic_scope=_KIWOOM_US_MINUTE_SCOPE[timeframe],
                                       min_rows=_KIWOOM_US_MIN_ROWS[timeframe])
        if timeframe in ('4h', '12h'):
            df = _resample_ohlcv(df, timeframe)
    return df


def create_us_stock_chart(ticker: str, timeframe: str = '1d') -> dict:
    """/au 해외주식: 키움증권 REST API (usa20100 현재가/52주 + usa06011/12/13/14 차트).
    yfinance 는 호출하지 않는다."""
    ticker = ticker.upper().strip()
    result = {
        'success': False, 'file_path': None, 'current_price': None,
        'symbol': ticker, 'timeframe': timeframe, 'exchange': 'KIWOOM',
        'error': None, 'currency': '$', 'caption': '',
    }
    logger.info("[AU] input=%s", ticker)

    if timeframe not in VALID_INTERVALS:
        result['error'] = (
            f"지원하지 않는 인터벌: {timeframe}\n"
            f"지원 인터벌: 15m / 1h / 4h / 12h / 1d / 1w / 1y"
        )
        return result

    try:
        exchange, quote = kiwoom_api.us_quote(ticker)
    except kiwoom_api.KiwoomAuthError as e:
        logger.error("[AU] Kiwoom auth error: %s", e)
        result['error'] = _KIWOOM_MSG_AUTH
        return result
    except kiwoom_api.KiwoomError as e:
        logger.info("[AU] Overseas stock not found: %s (%s)", ticker, e)
        result['error'] = _KIWOOM_MSG_US_NOT_FOUND.format(query=ticker)
        return result

    logger.info("[AU] Overseas stock found: %s exchange=%s", ticker, exchange)
    logger.info("[AU] source=KIWOOM")

    try:
        df_full = _fetch_kiwoom_us_df(ticker, exchange, timeframe)
        df = df_full.tail(60)

        current_price = float(df['close'].iloc[-1])
        prev_price    = float(df['close'].iloc[-2]) if len(df) >= 2 else current_price
        result['current_price'] = current_price

        high_52w, low_52w = kiwoom_api.us_52w_range(quote)
        if high_52w is None or low_52w is None:
            try:
                df_1d = df_full if timeframe == '1d' else _fetch_kiwoom_us_df(ticker, exchange, '1d')
                year  = df_1d.tail(252)
                high_52w = high_52w if high_52w is not None else float(year['high'].max())
                low_52w  = low_52w  if low_52w  is not None else float(year['low'].min())
            except kiwoom_api.KiwoomError as e:
                logger.warning("[AU] 52주 일봉 조회 실패(생략): %s", e)

        label    = _TIMEFRAME_LABEL.get(timeframe, timeframe.upper())
        title    = f"{ticker} - {label} - Kiwoom"
        tmp_path = _make_tmp_path()
        _draw_chart(df, title, timeframe, tmp_path)

        lines = [
            f"📊 {ticker} 차트",
            f"🕒 Timeframe: {label}\n",
            f"현재가: {_fmt_us(current_price)} USD",
            f"전일대비: {_change_line(current_price, prev_price, _fmt_us)}",
        ]
        if high_52w is not None:
            lines.append(f"52주 최고가: {_fmt_us(high_52w)} USD")
        if low_52w is not None:
            lines.append(f"52주 최저가: {_fmt_us(low_52w)} USD")

        result['success']   = True
        result['file_path'] = tmp_path
        result['caption']   = '\n'.join(lines)
        logger.info("[AU] chart generated successfully")

    except kiwoom_api.KiwoomAuthError as e:
        logger.error("[AU] Kiwoom auth error: %s", e)
        result['error'] = _KIWOOM_MSG_AUTH
    except kiwoom_api.KiwoomError as e:
        logger.error("[AU] Kiwoom data error: %s", e)
        result['error'] = _KIWOOM_MSG_DATA
    except Exception:
        logger.error("create_us_stock_chart 오류:\n%s", tb.format_exc())
        result['error'] = _KIWOOM_MSG_DATA
        plt.close('all')

    return result
