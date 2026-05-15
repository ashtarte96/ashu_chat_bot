"""
chart_utils.py
Binance REST API (primary) + Bybit v5 REST API (fallback)
+ yfinance (US stocks) + pykrx (Korean stocks)
"""

import json as _json
import logging
import os
import re
import subprocess
import tempfile
import time as _time
import traceback as tb
import unicodedata
from datetime import date, datetime, timedelta

import pandas as pd
import requests
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.ticker as mticker

logger = logging.getLogger(__name__)

# ── Timeframe constants ────────────────────────────────────────────────

VALID_INTERVALS = {'1h', '4h', '12h', '1d', '1w', '1y'}

_TIMEFRAME_LABEL = {
    '1h': '1H', '4h': '4H', '12h': '12H',
    '1d': '1D', '1w': '1W', '1y': '1Y',
}

_TIMEFRAME_DATE_FMT = {
    '1h':  '%m/%d %H:%M',
    '4h':  '%m/%d %H:%M',
    '12h': '%m/%d %H:%M',
    '1d':  '%m/%d',
    '1w':  '%y/%m/%d',
    '1y':  '%Y/%m',
}

# Binance kline interval strings (1y uses 1d data, resampled to monthly)
_BINANCE_INTERVAL = {
    '1h': '1h', '4h': '4h', '12h': '12h',
    '1d': '1d', '1w': '1w', '1y': '1d',
}

# Bybit kline interval strings (max 200 candles per request)
_BYBIT_INTERVAL = {
    '1h': '60', '4h': '240', '12h': '720',
    '1d': 'D', '1w': 'W', '1y': 'D',
}

# API fetch limits
# 1d fetches 365 so _draw_chart(tail 60) has data AND 52w stats are accurate
_FETCH_LIMIT = {
    '1h':  60,
    '4h':  60,
    '12h': 60,
    '1d':  365,
    '1w':  60,
    '1y':  1000,  # daily data fetched, then resampled to monthly
}

# yfinance (interval, period) — 4h/12h fetched as 1h then resampled
_YF_PARAMS = {
    '1h':  ('1h',  '60d'),
    '4h':  ('1h',  '60d'),
    '12h': ('1h',  '60d'),
    '1d':  ('1d',  '1y'),
    '1w':  ('1wk', '5y'),
    '1y':  ('1mo', '10y'),
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
    if price >= 1000:
        return f"{price:,.2f}"
    elif price >= 1:
        return f"{price:.4f}"
    else:
        return f"{price:.8f}"


def _fmt_kr(price: float) -> str:
    return f"{int(round(price)):,}"


def _fmt_us(price: float) -> str:
    if price >= 1000:
        return f"{price:,.2f}"
    return f"{price:.2f}"


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


# ── US stocks (yfinance) ───────────────────────────────────────────────

def _normalize_yf_df(data) -> 'pd.DataFrame | None':
    if data is None or data.empty:
        return None
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = [col[0] for col in data.columns]
    data.columns = [c.lower() for c in data.columns]
    if data.index.tz is None:
        data.index = data.index.tz_localize('UTC')
    req = ['open', 'high', 'low', 'close']
    if not all(c in data.columns for c in req):
        return None
    if 'volume' not in data.columns:
        data = data[req].copy()
        data['volume'] = 0.0
    else:
        data = data[req + ['volume']].copy()
    data = data.dropna(subset=req)
    return data if not data.empty else None


def _fetch_us_yf(ticker: str, timeframe: str) -> pd.DataFrame:
    """Fetch yfinance data, resample if needed, return last 60 rows."""
    import yfinance as yf
    yf_interval, yf_period = _YF_PARAMS.get(timeframe, ('1d', '1y'))
    print(f"[AU TRY] {ticker} {timeframe} interval={yf_interval} period={yf_period}")
    try:
        raw = yf.Ticker(ticker).history(period=yf_period, interval=yf_interval)
        df  = _normalize_yf_df(raw)
        if df is None:
            raise ValueError(f"yfinance 빈 데이터: {ticker}")
        # resample for 4h/12h (base data is 1h)
        if timeframe == '4h':
            df = _resample_ohlcv(df, '4h')
        elif timeframe == '12h':
            df = _resample_ohlcv(df, '12h')
        df = df.tail(60)
        if df.empty:
            raise ValueError(f"yfinance 빈 데이터: {ticker}")
        print(f"[AU OK] {ticker} {timeframe} rows={len(df)}")
        return df
    except ValueError:
        raise
    except Exception as e:
        print(f"[AU FAIL] {e}")
        raise ValueError(f"yfinance 조회 실패: {ticker}") from e


# ── Korean stocks (kiwoom-cli based) ──────────────────────────────────

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


# ── kiwoom-cli wrapper ─────────────────────────────────────────────────

_KIWOOM_SEARCH_CACHE: dict = {}  # norm_query → (result_dict, timestamp)
_KIWOOM_CACHE_TTL = 600          # 10 minutes


def _kiwoom_run(*args, timeout: int = 10):
    """Run `kiwoom -f json <args>` and return parsed JSON. None on failure."""
    cmd = ['kiwoom', '-f', 'json'] + [str(a) for a in args]
    print(f"[KIWOOM CMD] {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            print(f"[KIWOOM STDERR] returncode={proc.returncode} stderr={proc.stderr[:500]}")
        stdout = proc.stdout.strip()
        if stdout:
            try:
                return _json.loads(stdout)
            except _json.JSONDecodeError:
                print(f"[KIWOOM RAW] {stdout[:500]}")
        return None
    except subprocess.TimeoutExpired:
        print(f"[KIWOOM TIMEOUT] timed out after {timeout}s: {' '.join(cmd)}")
        return None
    except FileNotFoundError:
        print("[KIWOOM ERROR] kiwoom 명령어를 찾을 수 없습니다. pip install kiwoom-cli 확인")
        return None
    except Exception as e:
        print(f"[KIWOOM ERROR] {e}")
        return None


def _kiwoom_parse_stock_item(item: dict) -> dict:
    """Extract code/name/market from a kiwoom search result item."""
    code = (item.get('stk_cd') or item.get('code') or
            item.get('ticker') or item.get('item_code') or '')
    name = (item.get('stk_nm') or item.get('name') or
            item.get('stk_name') or item.get('item_name') or '')
    market = (item.get('mrkt_nm') or item.get('market') or
              item.get('mrkt_cd') or item.get('market_name') or '')
    return {
        'code':   str(code).strip().zfill(6),
        'name':   str(name).strip(),
        'market': str(market).strip(),
    }


def kiwoom_search_stock(query: str) -> 'dict | None':
    """
    kiwoom-cli로 종목 검색.
    Returns {'code': '328130', 'name': '루닛', 'market': 'KOSDAQ'} or None.
    Cache: 10분.
    """
    norm = _normalize_query(query)
    now  = _time.time()

    if norm in _KIWOOM_SEARCH_CACHE:
        cached, ts = _KIWOOM_SEARCH_CACHE[norm]
        if now - ts < _KIWOOM_CACHE_TTL:
            print(f"[KIWOOM SEARCH] cache hit: query={query!r} → {cached}")
            return cached

    print(f"[KIWOOM SEARCH] query={query!r}")
    data = _kiwoom_run('stock', 'search', query, timeout=10)

    if data is None:
        print(f"[KIWOOM SEARCH] no data for query={query!r}")
        return None

    items = data if isinstance(data, list) else (
        data.get('results') or data.get('items') or
        data.get('data')    or data.get('list')  or []
    )
    if not items:
        print(f"[KIWOOM SEARCH RESULT] query={query!r} → 0 results (raw={str(data)[:200]})")
        return None

    print(f"[KIWOOM SEARCH RESULT] query={query!r} → {len(items)} results")

    # 1. 정규화된 이름 정확 매칭 우선
    for item in items:
        parsed = _kiwoom_parse_stock_item(item)
        if _normalize_query(parsed['name']) == norm:
            print(f"[KIWOOM SEARCH RESULT] exact match: code={parsed['code']} name={parsed['name']!r}")
            _KIWOOM_SEARCH_CACHE[norm] = (parsed, now)
            return parsed

    # 2. 첫 번째 결과
    parsed = _kiwoom_parse_stock_item(items[0])
    print(f"[KIWOOM SEARCH RESULT] first result: code={parsed['code']} name={parsed['name']!r}")
    _KIWOOM_SEARCH_CACHE[norm] = (parsed, now)
    return parsed


def kiwoom_get_price(code: str) -> 'dict | None':
    """kiwoom-cli로 현재가 조회. Returns dict or None."""
    code = str(code).zfill(6)
    print(f"[KIWOOM PRICE] fetching code={code}")
    data = _kiwoom_run('stock', 'price', code, timeout=10)
    if data is None:
        return None

    # 응답이 리스트인 경우 첫 번째 항목 사용
    if isinstance(data, list):
        data = data[0] if data else {}

    print(f"[KIWOOM PRICE] raw={str(data)[:300]}")

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

    price      = _to_int('cur_prc', 'price', 'close', 'clos_pric', 'stck_prpr', 'current_price')
    change     = _to_int('prv_diff', 'change', 'diff', 'prdy_vrss', 'price_change')
    change_pct = _to_float('diff_rt', 'change_pct', 'change_rate', 'prdy_ctrt', 'rate')
    volume     = _to_int('trde_vol', 'volume', 'acml_vol', 'acc_volume')
    open_p     = _to_int('open_pric', 'open', 'stck_oprc')
    high_p     = _to_int('hgst_pric', 'high', 'stck_hgpr', 'high_price')
    low_p      = _to_int('lwst_pric', 'low',  'stck_lwpr', 'low_price')

    # 부호 처리 — 필드명에 'sign' 또는 음수값 포함 여부로 판단
    sign = str(data.get('prv_diff_sign', data.get('sign', data.get('change_sign', ''))) or '')
    if sign in ('2', '하락', 'FALL') or change_pct < 0:
        change     = -abs(change)
        change_pct = -abs(change_pct)

    print(f"[KIWOOM PRICE] code={code} price={price:,} change_pct={change_pct:+.2f}% volume={volume:,}")
    return {
        'price':      price,
        'change':     change,
        'change_pct': change_pct,
        'volume':     volume,
        'open':       open_p,
        'high':       high_p,
        'low':        low_p,
    }


def kiwoom_get_chart_ohlcv(code: str, timeframe: str = '1d') -> pd.DataFrame:
    """
    kiwoom-cli stock chart 명령으로 OHLCV DataFrame 취득.
    timeframe: 1h / 4h / 12h / 1d / 1w / 1y
    Returns DataFrame [open, high, low, close, volume], UTC DatetimeIndex.
    """
    code = str(code).zfill(6)

    _TF_ARGS: dict = {
        '1h':  ('chart', 'minute', code, '--interval', '60'),
        '4h':  ('chart', 'minute', code, '--interval', '240'),
        '12h': ('chart', 'minute', code, '--interval', '720'),
        '1d':  ('chart', 'day',    code),
        '1w':  ('chart', 'week',   code),
        '1y':  ('chart', 'year',   code),
    }
    tf_args = _TF_ARGS.get(timeframe, ('chart', 'day', code))
    cmd_args = ('stock',) + tf_args
    print(f"[KIWOOM CHART] code={code} tf={timeframe} → kiwoom -f json {' '.join(cmd_args)}")

    data = _kiwoom_run(*cmd_args, timeout=15)
    if data is None:
        raise ValueError(f"[KIWOOM CHART] no data for code={code} timeframe={timeframe}")

    candles = data if isinstance(data, list) else (
        data.get('candles') or data.get('data') or data.get('ohlcv') or
        data.get('chart')   or data.get('list') or []
    )
    if not candles:
        raise ValueError(f"[KIWOOM CHART] empty candles for code={code} (raw={str(data)[:200]})")

    rows = []
    for c in candles:
        date_val = (c.get('date') or c.get('dt') or c.get('stck_bsop_date') or
                    c.get('candle_date_time_kst') or c.get('time') or
                    c.get('bas_dt') or '')
        def _fv(k_list):
            for k in k_list:
                v = c.get(k)
                if v is not None:
                    try:
                        return float(str(v).replace(',', ''))
                    except (ValueError, TypeError):
                        pass
            return 0.0
        open_v  = _fv(['open',  'open_pric',  'stck_oprc'])
        high_v  = _fv(['high',  'hgst_pric',  'stck_hgpr', 'high_price'])
        low_v   = _fv(['low',   'lwst_pric',  'stck_lwpr', 'low_price'])
        close_v = _fv(['close', 'clos_pric',  'stck_clpr', 'close_price'])
        vol_v   = _fv(['volume','trde_vol',   'acml_vol',  'acc_volume'])
        rows.append({
            'date': str(date_val).strip(),
            'open': open_v, 'high': high_v, 'low': low_v,
            'close': close_v, 'volume': vol_v,
        })

    if not rows:
        raise ValueError(f"[KIWOOM CHART] failed to parse candles for code={code}")

    df = pd.DataFrame(rows)
    parsed = None
    for fmt in ('%Y%m%d%H%M%S', '%Y%m%d%H%M', '%Y%m%d',
                '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
        try:
            parsed = pd.to_datetime(df['date'], format=fmt, errors='raise')
            break
        except (ValueError, TypeError):
            pass
    if parsed is None:
        parsed = pd.to_datetime(df['date'], infer_datetime_format=True, errors='coerce')

    df['dt'] = parsed
    df = df.dropna(subset=['dt']).sort_values('dt')
    try:
        df.index = df['dt'].dt.tz_localize('Asia/Seoul').dt.tz_convert('UTC')
    except Exception:
        df.index = df['dt'].dt.tz_convert('UTC')
    df = df[['open', 'high', 'low', 'close', 'volume']].copy()
    df = df.dropna(subset=['open', 'close'])

    print(f"[KIWOOM CHART OK] code={code} tf={timeframe} rows={len(df)}")
    return df



def find_kr_stock(query: str):
    """
    kiwoom-cli 기반 한국 주식/ETF 검색.
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
    bare = _bare_kr_ticker(ticker)

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
        f"💰 현재가: ₩{int(current_price):,}",
        f"📊 등락률: {pct_str} ({amt_str}원)",
    ]
    if volume > 0:
        lines.append(f"📦 거래량: {volume:,}")
    lines.append(f"\n52주 최고: ₩{int(high_52w):,}")
    lines.append(f"52주 최저: ₩{int(low_52w):,}")
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
    """/ac: Binance spot → Bybit spot fallback"""
    sym = _parse_symbol(symbol)
    result = {
        'success': False, 'file_path': None, 'current_price': None,
        'symbol': sym, 'timeframe': timeframe, 'exchange': None,
        'error': None, 'currency': '$', 'caption': '',
    }
    try:
        limit = _FETCH_LIMIT.get(timeframe, 60)
        print(f"[AC] sym={sym} timeframe={timeframe} limit={limit}")

        df = fetch_binance_spot(sym, timeframe, limit)
        source = 'Binance spot'
        if df is None:
            print(f"[AC] Binance spot None → try Bybit spot")
            df     = fetch_bybit_spot(sym, timeframe, limit)
            source = 'Bybit spot'

        if df is None:
            print(f"[AC] Both sources returned None for {sym} {timeframe}")
            result['error'] = (
                f"코인 데이터를 가져올 수 없습니다: {sym}\n"
                "Binance / Bybit 모두 실패 — 서버 로그를 확인하세요."
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
            df_1d = fetch_binance_spot(sym, '1d', 365)
            if df_1d is None:
                df_1d = fetch_bybit_spot(sym, '1d', 365)
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
    """/ap: Binance futures → Bybit perps fallback"""
    sym = _parse_symbol(symbol)
    result = {
        'success': False, 'file_path': None, 'current_price': None,
        'symbol': sym, 'timeframe': timeframe, 'exchange': None,
        'error': None, 'currency': '$', 'caption': '',
    }
    try:
        limit = _FETCH_LIMIT.get(timeframe, 60)

        df = fetch_binance_futures(sym, timeframe, limit)
        source = 'Binance futures'
        if df is None:
            df     = fetch_bybit_perps(sym, timeframe, limit)
            source = 'Bybit perps'

        if df is None:
            result['error'] = (
                f"선물 데이터를 가져올 수 없습니다: {sym}\n"
                "Binance futures / Bybit perps 모두 실패 — 서버 로그를 확인하세요."
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
            df_1d = fetch_binance_futures(sym, '1d', 365)
            if df_1d is None:
                df_1d = fetch_bybit_perps(sym, '1d', 365)
            high_52w = float(df_1d['high'].max()) if df_1d is not None else None
            low_52w  = float(df_1d['low'].min())  if df_1d is not None else None

        funding = get_funding_rate(sym)

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


def create_us_stock_chart(ticker: str, timeframe: str = '1d') -> dict:
    """/au: yfinance, all timeframes"""
    ticker = ticker.upper().strip()
    result = {
        'success': False, 'file_path': None, 'current_price': None,
        'symbol': ticker, 'timeframe': timeframe, 'exchange': 'yfinance',
        'error': None, 'currency': '$', 'caption': '',
    }

    if timeframe not in VALID_INTERVALS:
        result['error'] = (
            f"지원하지 않는 인터벌: {timeframe}\n"
            f"지원 인터벌: 1h / 4h / 12h / 1d / 1w / 1y"
        )
        return result

    try:
        df = _fetch_us_yf(ticker, timeframe)

        current_price = float(df['close'].iloc[-1])
        prev_price    = float(df['close'].iloc[-2]) if len(df) >= 2 else current_price
        result['current_price'] = current_price

        # 52w stats from 1d data
        high_52w = low_52w = None
        if timeframe == '1d':
            high_52w = float(df['high'].max())
            low_52w  = float(df['low'].min())
        else:
            try:
                df_1d    = _fetch_us_yf(ticker, '1d')
                high_52w = float(df_1d['high'].max())
                low_52w  = float(df_1d['low'].min())
            except Exception:
                pass

        label    = _TIMEFRAME_LABEL.get(timeframe, timeframe.upper())
        title    = f"{ticker} - {label} - yfinance"
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

    except Exception:
        logger.error("create_us_stock_chart 오류:\n%s", tb.format_exc())
        result['error'] = str(tb.format_exc().strip().split('\n')[-1])
        plt.close('all')

    return result
