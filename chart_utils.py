"""
chart_utils.py
Binance REST API (primary) + Bybit v5 REST API (fallback)
+ yfinance (US stocks) + pykrx (Korean stocks)
"""

import difflib
import logging
import os
import re
import tempfile
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


# ── Korean stocks ──────────────────────────────────────────────────────
# Primary source : 상장법인목록.xls  (KRX official listing, placed in project root)
# ETF fallback   : _STATIC_ETF_RAW  (Excel doesn't include ETFs)
# Stock fallback : _STATIC_STOCK_RAW (used when Excel is absent)
# -----------------------------------------------------------------------

try:
    from rapidfuzz import process as _rf_process, fuzz as _rf_fuzz
    _USE_RAPIDFUZZ = True
except ImportError:
    _USE_RAPIDFUZZ = False

# Path to KRX listing Excel (same directory as this file)
_KRX_EXCEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '상장법인목록.xls')

# KRX 시장구분 → canonical market name
_MARKET_MAP: dict = {
    '유가':   'KOSPI',   # 유가증권시장 = KOSPI
    '코스피': 'KOSPI',
    '코스닥': 'KOSDAQ',
    '코넥스': 'KONEX',
}

# Market → yfinance/log suffix  (pykrx always uses bare 6-digit codes)
_MARKET_SUFFIX: dict = {
    'KOSPI':  '.KS',
    'KOSDAQ': '.KQ',
    'KONEX':  '',
    'ETF':    '.KS',
}


def _normalize_query(text: str) -> str:
    """NFC → remove spaces/special chars → lowercase.
    'TIGER 미국S&P500' → 'tiger미국sp500'
    '두산 에너빌리티'  → '두산에너빌리티'
    '(주)LG&H'        → 'lgh'
    """
    text = unicodedata.normalize('NFC', text)
    text = re.sub(r'[\s\-\_\./\(\)\[\]·&,+*%@#$!^~|]', '', text)
    return text.lower()


# ── Static ETFs (KRX 상장법인목록.xls does NOT include ETFs) ─────────────
_STATIC_ETF_RAW = [
    # KODEX 시리즈
    ('069500', 'KODEX 200',                  'ETF'),
    ('122630', 'KODEX 레버리지',              'ETF'),
    ('114800', 'KODEX 인버스',               'ETF'),
    ('229200', 'KODEX 코스닥150',            'ETF'),
    ('251340', 'KODEX 코스닥150레버리지',    'ETF'),
    ('233740', 'KODEX 코스닥150인버스',      'ETF'),
    ('091160', 'KODEX 반도체',               'ETF'),
    ('091170', 'KODEX 은행',                 'ETF'),
    ('091180', 'KODEX 자동차',               'ETF'),
    ('379800', 'KODEX 미국S&P500TR',         'ETF'),
    ('379810', 'KODEX 미국나스닥100TR',      'ETF'),
    ('117460', 'KODEX 건설',                 'ETF'),
    ('102780', 'KODEX 삼성그룹',             'ETF'),
    # TIGER 시리즈
    ('102110', 'TIGER 200',                  'ETF'),
    ('360750', 'TIGER 미국S&P500',           'ETF'),
    ('133690', 'TIGER 나스닥100',            'ETF'),
    ('305720', 'TIGER 2차전지테마',          'ETF'),
    ('195930', 'TIGER 해외상장리츠(H)',      'ETF'),
    ('441800', 'TIGER 미국테크TOP10INDXX',   'ETF'),
    ('139220', 'TIGER 200 IT',               'ETF'),
    ('364980', 'TIGER 차이나전기차SOLACTIVE','ETF'),
    ('458730', 'TIGER 인도니프티50',         'ETF'),
    # ARIRANG / KBSTAR / HANARO
    ('270810', 'ARIRANG 고배당주',           'ETF'),
    ('292150', 'KBSTAR 200',                 'ETF'),
]

# ── Static stock fallback (used only when Excel file is absent) ───────────
_STATIC_STOCK_RAW = [
    ('005930', '삼성전자',           'KOSPI'),
    ('000660', 'SK하이닉스',         'KOSPI'),
    ('005380', '현대차',             'KOSPI'),
    ('000270', '기아',               'KOSPI'),
    ('373220', 'LG에너지솔루션',     'KOSPI'),
    ('207940', '삼성바이오로직스',   'KOSPI'),
    ('006400', '삼성SDI',            'KOSPI'),
    ('051910', 'LG화학',             'KOSPI'),
    ('068270', '셀트리온',           'KOSPI'),
    ('005490', 'POSCO홀딩스',        'KOSPI'),
    ('003670', '포스코퓨처엠',       'KOSPI'),
    ('012330', '현대모비스',         'KOSPI'),
    ('105560', 'KB금융',             'KOSPI'),
    ('055550', '신한지주',           'KOSPI'),
    ('028260', '삼성물산',           'KOSPI'),
    ('086790', '하나금융지주',       'KOSPI'),
    ('066570', 'LG전자',             'KOSPI'),
    ('015760', '한국전력',           'KOSPI'),
    ('323410', '카카오뱅크',         'KOSPI'),
    ('377300', '카카오페이',         'KOSPI'),
    ('034020', '두산에너빌리티',     'KOSPI'),
    ('241560', '두산밥캣',           'KOSPI'),
    ('009150', '삼성전기',           'KOSPI'),
    ('011070', 'LG이노텍',           'KOSPI'),
    ('012450', '한화에어로스페이스', 'KOSPI'),
    ('010130', '고려아연',           'KOSPI'),
    ('032830', '삼성생명',           'KOSPI'),
    ('000810', '삼성화재',           'KOSPI'),
    ('035420', 'NAVER',              'KOSPI'),
    ('035720', '카카오',             'KOSPI'),
    ('030200', 'KT',                 'KOSPI'),
    ('017670', 'SK텔레콤',           'KOSPI'),
    ('003550', 'LG',                 'KOSPI'),
    ('096770', 'SK이노베이션',       'KOSPI'),
    ('033780', 'KT&G',               'KOSPI'),
    ('003490', '대한항공',           'KOSPI'),
    ('086520', '에코프로',           'KOSDAQ'),
    ('247540', '에코프로비엠',       'KOSDAQ'),
    ('259960', '크래프톤',           'KOSDAQ'),
    ('293490', '카카오게임즈',       'KOSDAQ'),
    ('036570', '엔씨소프트',         'KOSDAQ'),
    ('352820', 'HYBE',               'KOSDAQ'),
    ('263750', '펄어비스',           'KOSDAQ'),
    ('035900', 'JYP Ent.',           'KOSDAQ'),
    ('122870', '와이지엔터테인먼트', 'KOSDAQ'),
]


def _load_krx_excel() -> list:
    """
    Load 상장법인목록.xls → [(ticker_6digit, name, market), ...]
    KRX .xls files are HTML-disguised tables with EUC-KR encoding.
    Returns empty list if file is absent or unreadable.
    """
    if not os.path.isfile(_KRX_EXCEL_PATH):
        logger.warning("[KRX EXCEL] file not found: %s", _KRX_EXCEL_PATH)
        return []
    df = None
    try:
        tables = pd.read_html(_KRX_EXCEL_PATH, encoding='euc-kr')
        df = tables[0] if tables else None
    except Exception:
        try:
            df = pd.read_excel(_KRX_EXCEL_PATH, dtype=str)
        except Exception as e:
            logger.warning("[KRX EXCEL] read failed: %s", e)
            return []

    if df is None or df.empty:
        return []

    cols = {str(c).strip(): c for c in df.columns}
    name_col = next((cols[c] for c in cols if '회사명' in c or '종목명' in c), None)
    code_col  = next((cols[c] for c in cols if '종목코드' in c or '코드' in c), None)
    mkt_col   = next((cols[c] for c in cols if '시장구분' in c or '시장' in c), None)

    if not name_col or not code_col:
        logger.warning("[KRX EXCEL] required columns not found: %s", list(df.columns))
        return []

    results = []
    for _, row in df.iterrows():
        raw_ticker = str(row[code_col]).strip().split('.')[0].zfill(6)
        if not raw_ticker.isdigit() or len(raw_ticker) != 6:
            continue
        name = str(row[name_col]).strip()
        if not name or name == 'nan':
            continue
        market = 'KOSPI'
        if mkt_col:
            mkt_raw = str(row[mkt_col]).strip()
            market = _MARKET_MAP.get(mkt_raw, _MARKET_MAP.get(mkt_raw[:2], 'KOSPI'))
        results.append((raw_ticker, name, market))

    return results


def _load_kind_listing() -> list:
    """
    KRX KIND 공시시스템에서 전체 상장종목 수집 (단일 HTTP 요청, 로그인 불필요).
    http://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13
    Naver 스크래핑이 실패/부족할 때 사용하는 fallback.
    Returns [(ticker_6digit, name, market), ...]
    """
    try:
        from io import StringIO
        r = requests.get(
            'http://kind.krx.co.kr/corpgeneral/corpList.do',
            params={'method': 'download', 'searchType': '13'},
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Referer': 'http://kind.krx.co.kr/',
            },
            timeout=30,
        )
        r.raise_for_status()
        r.encoding = 'euc-kr'
        tables = pd.read_html(StringIO(r.text))
        if not tables:
            logger.warning("[KIND] no tables found")
            return []
        df = tables[0]
        cols = {str(c).strip(): c for c in df.columns}
        name_col = next((cols[c] for c in cols if '회사명' in c or '종목명' in c), None)
        code_col = next((cols[c] for c in cols if '종목코드' in c or '코드' in c), None)
        mkt_col  = next((cols[c] for c in cols if '시장구분' in c or '시장' in c), None)
        if not name_col or not code_col:
            logger.warning("[KIND] required columns not found: %s", list(df.columns))
            return []
        results = []
        for _, row in df.iterrows():
            raw_ticker = str(row[code_col]).strip().split('.')[0].zfill(6)
            if not raw_ticker.isdigit() or len(raw_ticker) != 6:
                continue
            name = str(row[name_col]).strip()
            if not name or name in ('nan', 'NaN'):
                continue
            market = 'KOSPI'
            if mkt_col:
                mkt_raw = str(row[mkt_col]).strip()
                market = _MARKET_MAP.get(mkt_raw, _MARKET_MAP.get(mkt_raw[:2], 'KOSPI'))
            results.append((raw_ticker, name, market))
        k = sum(1 for _, _, m in results if m == 'KOSPI')
        q = sum(1 for _, _, m in results if m == 'KOSDAQ')
        print(f"[KIND LISTING] KOSPI={k} KOSDAQ={q} total={len(results)}")
        return results
    except Exception as e:
        logger.warning("[KIND LISTING] failed: %s", e)
        return []


_NAVER_HEADERS = {
    'User-Agent':      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept-Language': 'ko-KR,ko;q=0.9',
    'Referer':         'https://finance.naver.com/',
}


def _fetch_naver_etf_list() -> list:
    """
    Naver Finance ETF 페이지에서 ETF 전체 목록 수집.
    https://finance.naver.com/sise/etf.naver
    Returns [(ticker, name, 'ETF'), ...]
    """
    try:
        from bs4 import BeautifulSoup
        r = requests.get('https://finance.naver.com/sise/etf.naver',
                         headers=_NAVER_HEADERS, timeout=15)
        r.encoding = 'euc-kr'
        soup = BeautifulSoup(r.text, 'lxml')

        results = []
        seen: set = set()
        # ETF 페이지의 메인 테이블 내 종목 링크
        for a in soup.select('a[href*="item/main.naver?code="]'):
            href = a.get('href', '')
            m = re.search(r'code=(\d{6})', href)
            if not m:
                continue
            ticker = m.group(1)
            if ticker in seen:
                continue
            name = a.get_text(strip=True)
            if name and len(ticker) == 6:
                results.append((ticker, name, 'ETF'))
                seen.add(ticker)

        print(f"[NAVER ETF LOAD] count={len(results)}")
        return results
    except Exception as e:
        logger.warning("[NAVER ETF] scrape failed: %s", e)
        return []


def _parse_market_soup(soup, market: str, results: list, seen: set) -> int:
    """
    Naver sise_market_sum 페이지 soup에서 종목코드+이름 추출.
    반환: 이번 페이지에서 신규 추가된 종목 수.

    전략 1: table.type_2 a[href*="code="]  (테이블 범위 내 종목 링크)
    전략 2: a[href*="item/main.naver?code="]  (전체 페이지 fallback)
    """
    added = 0

    links = soup.select('table.type_2 a[href*="code="]')
    if not links:
        links = soup.select('a[href*="item/main.naver?code="]')

    for a in links:
        href = a.get('href', '')
        m = re.search(r'code=(\d{6})', href)
        if not m:
            continue
        ticker = m.group(1)
        name   = a.get_text(strip=True)
        if ticker and name and ticker not in seen:
            results.append((ticker, name, market))
            seen.add(ticker)
            added += 1
    return added


def _detect_last_page(soup) -> int:
    """
    Naver Finance 페이지네이션에서 마지막 페이지 번호 탐지.
    .pgRR → 맨뒤 링크의 page=N 파라미터 추출.
    """
    # 여러 selector 시도 (Naver 레이아웃 버전 차이 대응)
    for sel in ['td.pgRR a', '.pgRR a', 'a.pgRR']:
        tag = soup.select_one(sel)
        if tag:
            href = tag.get('href', '')
            m = re.search(r'page=(\d+)', href)
            if m:
                return int(m.group(1))

    # fallback: 페이지 링크 전체에서 최댓값
    max_p = 1
    for a in soup.select('div.paging a, .paging a, td[class*="pg"] a'):
        href = a.get('href', '')
        m = re.search(r'page=(\d+)', href)
        if m:
            max_p = max(max_p, int(m.group(1)))
    return max_p if max_p > 1 else 50


def _load_naver_market(sosok: int) -> list:
    """
    Naver Finance sise_market_sum에서 KOSPI(sosok=0)/KOSDAQ(sosok=1) 전체 종목 수집.

    알고리즘:
      1) 첫 페이지 로드 → pgRR로 마지막 페이지 번호 탐지
      2) 2 ~ last_page 전 페이지 순회 (중간 오류가 있어도 건너뜀)
      3) 각 페이지에서 종목코드+이름 추출

    Returns [(ticker_6digit, name, market), ...]
    """
    import time as _time
    from bs4 import BeautifulSoup

    market    = 'KOSPI' if sosok == 0 else 'KOSDAQ'
    base_url  = (
        'https://finance.naver.com/sise/sise_market_sum.naver'
        f'?sosok={sosok}'
    )
    results:  list = []
    seen:     set  = set()
    last_page = 50  # default safety limit

    # ── 1. 첫 페이지 + 마지막 페이지 탐지 ────────────────────────────
    try:
        r = requests.get(f'{base_url}&page=1', headers=_NAVER_HEADERS, timeout=15)
        r.encoding = 'euc-kr'
        soup = BeautifulSoup(r.text, 'lxml')
        last_page = _detect_last_page(soup)
        cnt = _parse_market_soup(soup, market, results, seen)
        print(f"[{market} LOAD] 마지막 페이지={last_page}  page=1: {cnt}개")
    except Exception as e:
        logger.error("[%s] page=1 failed: %s", market, e)
        return results

    # ── 2. 2 ~ last_page 전 페이지 순회 ──────────────────────────────
    consecutive_empty = 0
    for page in range(2, last_page + 1):
        _time.sleep(0.15)   # Naver 서버 부하 방지
        try:
            r = requests.get(
                f'{base_url}&page={page}', headers=_NAVER_HEADERS, timeout=15)
            r.encoding = 'euc-kr'
            soup = BeautifulSoup(r.text, 'lxml')
            cnt = _parse_market_soup(soup, market, results, seen)

            if cnt == 0:
                consecutive_empty += 1
                logger.debug("[%s] page=%d: 0개 (연속 빈 페이지=%d)",
                             market, page, consecutive_empty)
                # 3페이지 연속 빈 경우만 중단 (일시적 파싱 실패 대비)
                if consecutive_empty >= 3:
                    print(f"[{market} LOAD] 3페이지 연속 빈 페이지 → 종료 (page={page})")
                    break
            else:
                consecutive_empty = 0

            if page % 10 == 0:
                print(f"[{market} LOAD] page={page}/{last_page} 누적={len(results)}")

        except Exception as e:
            logger.warning("[%s] page=%d error (skip): %s", market, page, e)

    print(f"[NAVER STOCK LOAD] {market} count={len(results)}")
    if results:
        print(f"[{market} LOAD] 샘플 5개: {[(t,n) for t,n,_ in results[:5]]}")
    return results


def _fetch_naver_ohlcv(ticker: str, count: int = 365) -> pd.DataFrame:
    """
    Naver Finance fchart에서 일봉 OHLCV 가져오기 (pykrx 실패 시 fallback).
    https://fchart.stock.naver.com/sise.nhn?symbol={ticker}&timeframe=day&count={count}&requestType=0
    Returns DataFrame with columns [open, high, low, close, volume], UTC index.
    """
    from lxml import etree as _et
    bare = _bare_kr_ticker(ticker)
    url  = (
        'https://fchart.stock.naver.com/sise.nhn'
        f'?symbol={bare}&timeframe=day&count={count}&requestType=0'
    )
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()

    try:
        root = _et.fromstring(r.content)
    except Exception as e:
        raise ValueError(f"[NAVER FCHART] XML parse error for {bare}: {e}")

    rows = []
    for item in root.iter('item'):
        raw = item.get('data', '')
        sep = '|' if '|' in raw else '^'
        parts = raw.split(sep)
        if len(parts) < 6:
            continue
        try:
            dt_s       = parts[0].strip()
            o, h, l, c, v = (float(p) for p in parts[1:6])
            rows.append({'date': dt_s, 'open': o, 'high': h, 'low': l, 'close': c, 'volume': v})
        except (ValueError, IndexError):
            continue

    if not rows:
        raise ValueError(f"[NAVER FCHART] no data for {bare}")

    df = pd.DataFrame(rows)
    df['dt'] = pd.to_datetime(df['date'].str.strip(), format='%Y%m%d', errors='coerce')
    df = df.dropna(subset=['dt'])
    df.index = df['dt'].dt.tz_localize('Asia/Seoul').dt.tz_convert('UTC')
    df = df[['open', 'high', 'low', 'close', 'volume']].sort_index()
    df = df.dropna(subset=['open', 'close'])

    logger.info("[NAVER FCHART OK] %s rows=%d", bare, len(df))
    return df


# Aliases: user-typed shorthand → canonical KRX display name (post-normalization).
# Excel uses Korean "포스코퓨처엠" for 003670, English "POSCO홀딩스" for 005490.
_KR_ALIASES_RAW: dict = {
    '삼전':         '삼성전자',
    '삼바':         '삼성바이오로직스',
    '한전':         '한국전력공사',
    '한국전력':     '한국전력공사',
    '엔솔':         'LG에너지솔루션',
    '엘지엔솔':     'LG에너지솔루션',
    'lg엔솔':       'LG에너지솔루션',
    '에코비엠':     '에코프로비엠',
    '두산':         '두산에너빌리티',
    '네이버':       'NAVER',
    '하이브':       'HYBE',
    '포스코퓨처엠': '포스코퓨처엠',   # explicit exact-match alias (Korean name in Excel)
    '포스코퓨처':   '포스코퓨처엠',
    '포퓨엠':       '포스코퓨처엠',
    '포스코홀딩스': 'POSCO홀딩스',    # Excel uses English prefix for 005490
    '포스코':       'POSCO홀딩스',
}
_KR_ALIASES: dict = {
    _normalize_query(k): _normalize_query(v)
    for k, v in _KR_ALIASES_RAW.items()
}

# KRX full-listing cache — rebuilt once per calendar day.
_krx_cache: dict = {
    'by_norm': {}, 'by_ticker': {}, 'entries': [], 'norm_names': [], 'date': '',
}


def _build_krx_cache() -> dict:
    """
    Build cache priority:
      1. Naver Finance KOSPI/KOSDAQ (live, paginated)  ← primary
      1.5 KIND API (KRX 공시시스템, single request)    ← fallback when Naver<400/500
      2. Naver Finance ETF page      (live)            ← primary
      3. Static ETF list                               ← safety net
      4. KRX Excel 상장법인목록.xls                    ← fallback (<500 stocks)
      5. Static stock list                             ← last resort
    """
    by_norm: dict   = {}
    by_ticker: dict = {}
    entries: list   = []

    def _add(ticker: str, name: str, market: str) -> None:
        if not name or ticker in by_ticker:
            return
        nn = _normalize_query(name)
        by_ticker[ticker] = (name, market)
        entries.append((ticker, name, nn, market))
        if nn not in by_norm:
            by_norm[nn] = ticker

    # Debug targets — always check these in output
    _DEBUG_STOCKS = {
        '328130': '루닛',
        '277810': '레인보우로보틱스',
        '247540': '에코프로비엠',
        '196170': '알테오젠',
        '087010': '펩트론',
        '403870': 'HPSP',
    }

    def _debug_check(stage: str) -> None:
        for code, name in _DEBUG_STOCKS.items():
            if code in by_ticker:
                _, mkt = by_ticker[code]
                print(f"[KRX DEBUG/{stage}] {name}({code}) ✓ market={mkt}")
            else:
                print(f"[KRX DEBUG/{stage}] {name}({code}) ✗ NOT loaded")

    # ── 1. Naver KOSPI + KOSDAQ ───────────────────────────────────────
    kospi_count = kosdaq_count = 0
    for ticker, name, market in _load_naver_market(0):
        _add(ticker, name, market)
        kospi_count += 1
    for ticker, name, market in _load_naver_market(1):
        _add(ticker, name, market)
        kosdaq_count += 1

    print(f"[KRX CACHE] Naver 로드: KOSPI={kospi_count} KOSDAQ={kosdaq_count}")
    _debug_check('after_naver')

    # ── 1.5. KIND fallback (Naver가 부족하면 KIND API로 보완) ──────────
    konex_count = 0
    if kospi_count < 400 or kosdaq_count < 500:
        logger.warning(
            "[KRX CACHE] Naver 부족 KOSPI=%d KOSDAQ=%d → KIND API fallback",
            kospi_count, kosdaq_count,
        )
        kind_added_k = kind_added_q = 0
        for ticker, name, market in _load_kind_listing():
            before = len(entries)
            _add(ticker, name, market)
            if len(entries) > before:
                if market == 'KOSPI':    kind_added_k += 1; kospi_count  += 1
                elif market == 'KOSDAQ': kind_added_q += 1; kosdaq_count += 1
                elif market == 'KONEX':  konex_count  += 1
        print(f"[KIND] 추가: KOSPI+={kind_added_k} KOSDAQ+={kind_added_q}")
        _debug_check('after_kind')

    # ── 2. Naver ETF page ─────────────────────────────────────────────
    etf_count = 0
    for ticker, name, market in _fetch_naver_etf_list():
        before = len(entries)
        _add(ticker, name, market)
        if len(entries) > before:
            etf_count += 1

    # ── 3. Static ETFs (always include for reliability) ───────────────
    for ticker, name, market in _STATIC_ETF_RAW:
        before = len(entries)
        _add(ticker, name, market)
        if len(entries) > before:
            etf_count += 1

    # ── 4. KRX Excel fallback (KIND도 실패 시) ────────────────────────
    if kospi_count + kosdaq_count < 500:
        logger.warning(
            "[KRX CACHE] KIND도 부족 %d+%d < 500 → Excel fallback",
            kospi_count, kosdaq_count,
        )
        for ticker, name, market in _load_krx_excel():
            _add(ticker, name, market)
            if market == 'KOSPI':    kospi_count  += 1
            elif market == 'KOSDAQ': kosdaq_count += 1
            elif market == 'KONEX':  konex_count  += 1

    # ── 5. Static stock list (최후 수단) ──────────────────────────────
    if len(entries) < 50:
        logger.warning("[KRX CACHE] only %d entries, using static stock list", len(entries))
        for ticker, name, market in _STATIC_STOCK_RAW:
            _add(ticker, name, market)

    total = len(entries)
    print(
        f"[KRX CACHE] 총 {total}개: "
        f"KOSPI={kospi_count} KOSDAQ={kosdaq_count} "
        f"KONEX={konex_count} ETF={etf_count}"
    )
    _debug_check('final')
    return {
        'by_norm':    by_norm,
        'by_ticker':  by_ticker,
        'entries':    entries,
        'norm_names': [nn for _, _, nn, _ in entries],
        'date':       date.today().strftime('%Y%m%d'),
    }


def _get_krx_cache() -> dict:
    """Return the KRX cache dict, rebuilding when the calendar date changes."""
    global _krx_cache
    today = date.today().strftime('%Y%m%d')
    if _krx_cache['entries'] and _krx_cache['date'] == today:
        return _krx_cache
    _krx_cache = _build_krx_cache()
    return _krx_cache


def find_kr_stock(query: str):
    """
    Search a Korean stock or ETF by name, alias, or 6-digit ticker code.
    Returns (ticker_with_suffix, name) for a unique match.
    Returns (None, [(ticker, name), ...]) for multiple candidates.
    Returns (None, []) when nothing is found.

    Priority:
      1. 6-digit code  → cache lookup
      2. Alias map     → resolve to canonical name → cache exact match
      3. Cache exact normalized match
      4. Cache contains match
      5. rapidfuzz WRatio (cutoff 60) or difflib fallback
    """
    query_raw  = query.strip()
    query_norm = _normalize_query(query_raw)

    def _suffix(market: str) -> str:
        return _MARKET_SUFFIX.get(market, '')

    # ── 1. 6-digit code ───────────────────────────────────────────────
    if query_raw.isdigit() and len(query_raw) == 6:
        cache = _get_krx_cache()
        if query_raw in cache['by_ticker']:
            name, market = cache['by_ticker'][query_raw]
            ticker_out = f"{query_raw}{_suffix(market)}"
            print(f"[KR SEARCH] query={query_raw} matched={name} ticker={ticker_out} market={market}")
            return ticker_out, name
        return None, []

    cache = _get_krx_cache()

    # ── 2. Alias ──────────────────────────────────────────────────────
    resolved_norm = _KR_ALIASES.get(query_norm)
    if resolved_norm:
        ticker = cache['by_norm'].get(resolved_norm)
        if ticker:
            name, market = cache['by_ticker'][ticker]
            ticker_out = f"{ticker}{_suffix(market)}"
            print(f"[KR SEARCH] query={query_raw} matched={name} ticker={ticker_out} market={market}")
            return ticker_out, name

    # ── 3. Exact normalized match ─────────────────────────────────────
    ticker = cache['by_norm'].get(query_norm)
    if ticker:
        name, market = cache['by_ticker'][ticker]
        ticker_out = f"{ticker}{_suffix(market)}"
        print(f"[SEARCH]\nquery={query_raw}\nmatched={name}\nticker={ticker_out}")
        return ticker_out, name

    # ── 4. Contains match ─────────────────────────────────────────────
    contains: list = [
        (t, n) for t, n, nn, _ in cache['entries']
        if query_norm in nn or nn in query_norm
    ]
    if contains:
        if len(contains) == 1:
            t, n = contains[0]
            market = cache['by_ticker'][t][1]
            ticker_out = f"{t}{_suffix(market)}"
            print(f"[SEARCH]\nquery={query_raw}\nmatched={n}\nticker={ticker_out}")
            return ticker_out, n
        return None, contains[:5]

    # ── 5. Fuzzy match ────────────────────────────────────────────────
    norm_names = cache['norm_names']
    if not norm_names:
        print(f"[KR SEARCH] query={query_raw} matched=None")
        return None, []

    if _USE_RAPIDFUZZ:
        results = _rf_process.extract(
            query_norm, norm_names,
            scorer=_rf_fuzz.WRatio, limit=5, score_cutoff=60,
        )
        fuzzy_norms = [r[0] for r in results]
    else:
        fuzzy_norms = difflib.get_close_matches(query_norm, norm_names, n=5, cutoff=0.6)

    if fuzzy_norms:
        fuzzy_set = set(fuzzy_norms)
        seen: set = set()
        fuzzy_results: list = []
        for t, n, nn, _ in cache['entries']:
            if nn in fuzzy_set and t not in seen:
                fuzzy_results.append((t, n))
                seen.add(t)
        fuzzy_results = fuzzy_results[:5]
        if len(fuzzy_results) == 1:
            t, n = fuzzy_results[0]
            market = cache['by_ticker'][t][1]
            ticker_out = f"{t}{_suffix(market)}"
            print(f"[KR SEARCH] query={query_raw} matched={n} ticker={ticker_out} market={market}")
            return ticker_out, n
        return None, fuzzy_results

    print(f"[KR SEARCH] query={query_raw} matched=None")
    return None, []


def search_kr_stock(query: str) -> dict:
    """
    Public API for Korean stock/ETF search. Returns structured result.

    Success  → {'name': str, 'code': str, 'market': str, 'ticker': str}
    Multiple → {'candidates': [{'name': str, 'code': str, 'market': str}, ...]}
    Not found→ {}
    """
    ticker, result = find_kr_stock(query)
    if ticker:
        bare             = _bare_kr_ticker(ticker)
        cache            = _get_krx_cache()
        name_r, market_r = cache['by_ticker'].get(bare, (result, 'KOSPI'))
        return {'name': name_r, 'code': bare, 'market': market_r, 'ticker': ticker}
    if isinstance(result, list) and result:
        cache = _get_krx_cache()
        return {
            'candidates': [
                {'name': n, 'code': t,
                 'market': cache['by_ticker'].get(t, (None, 'KOSPI'))[1]}
                for t, n in result
            ]
        }
    return {}


def is_kr_ticker(ticker: str) -> bool:
    """True when ticker is a KRX stock: bare 6-digit code or 6-digit.KS / .KQ"""
    bare = ticker.split('.')[0]
    return bare.isdigit() and len(bare) == 6


def _bare_kr_ticker(ticker: str) -> str:
    """Strip .KS / .KQ suffix → bare 6-digit pykrx code."""
    return ticker.split('.')[0]


def fetch_naver_daily_ohlcv(code: str, pages: int = 7) -> pd.DataFrame:
    """
    Naver Finance 일별시세 페이지에서 OHLCV 수집 (pykrx 실패 시 fallback).
    https://finance.naver.com/item/sise_day.naver?code={code}&page={page}

    각 페이지 약 10행. pages=7이면 약 70행 (차트용 60봉 충분).
    1w/1y 차트용으로는 pages를 크게 설정(예: 50~120).
    Returns DataFrame [open, high, low, close, volume], UTC DatetimeIndex.
    """
    from bs4 import BeautifulSoup
    bare = _bare_kr_ticker(code)
    all_rows = []

    for page in range(1, pages + 1):
        url = f'https://finance.naver.com/item/sise_day.naver?code={bare}&page={page}'
        try:
            r = requests.get(url, headers=_NAVER_HEADERS, timeout=10)
            r.encoding = 'euc-kr'
            soup = BeautifulSoup(r.text, 'lxml')

            table = soup.select_one('table.type2')
            if not table:
                break

            page_rows = 0
            for row in table.select('tr'):
                tds = row.select('td')
                if len(tds) < 7:
                    continue
                date_text = tds[0].get_text(strip=True)
                if not re.match(r'\d{4}\.\d{2}\.\d{2}', date_text):
                    continue
                try:
                    close_ = float(tds[1].get_text(strip=True).replace(',', ''))
                    open_  = float(tds[3].get_text(strip=True).replace(',', ''))
                    high_  = float(tds[4].get_text(strip=True).replace(',', ''))
                    low_   = float(tds[5].get_text(strip=True).replace(',', ''))
                    vol_   = float(re.sub(r'[^\d]', '', tds[6].get_text()) or '0')
                    all_rows.append({
                        'date': date_text,
                        'open': open_, 'high': high_, 'low': low_,
                        'close': close_, 'volume': vol_,
                    })
                    page_rows += 1
                except (ValueError, AttributeError):
                    continue

            if page_rows == 0:
                break  # 빈 페이지 → 종료

        except Exception as e:
            logger.warning("[NAVER DAILY] page=%d code=%s: %s", page, bare, e)
            if page == 1:
                raise  # 첫 페이지 실패면 즉시 raise

    if not all_rows:
        raise ValueError(f"[NAVER DAILY] no data for {bare}")

    df = pd.DataFrame(all_rows)
    df['dt'] = pd.to_datetime(df['date'], format='%Y.%m.%d', errors='coerce')
    df = df.dropna(subset=['dt'])
    df = df.sort_values('dt')  # ascending (oldest first)
    df.index = df['dt'].dt.tz_localize('Asia/Seoul').dt.tz_convert('UTC')
    df = df[['open', 'high', 'low', 'close', 'volume']].copy()
    df = df.dropna(subset=['open', 'close'])

    print(f"[NAVER DAILY] {bare} pages={page} rows={len(df)}")
    return df


def fetch_kr_stock_ohlcv(ticker: str, timeframe: str) -> pd.DataFrame:
    """
    Fetch KRX OHLCV using pykrx — works for KOSPI, KOSDAQ, KONEX, and ETFs.

    Always fetches raw daily data; resampling is done by the caller.
    Days fetched per timeframe:
      1h / 4h / 12h / 1d → 365 days  (intraday not available; caller shows daily)
      1w                  → 730 days  (~100 weekly candles)
      1y                  → 1825 days (~5 years for monthly resampling)

    Returns DataFrame with lowercase columns (open/high/low/close/volume),
    DatetimeIndex localized to UTC.
    """
    try:
        from pykrx import stock as krx
    except ImportError:
        raise ImportError("pykrx 패키지가 필요합니다: pip install pykrx")

    bare = _bare_kr_ticker(ticker)

    _DAYS_BACK = {
        '1h': 365, '4h': 365, '12h': 365, '1d': 365,
        '1w': 730, '1y': 1825,
    }
    days_back = _DAYS_BACK.get(timeframe, 365)
    todate    = date.today().strftime('%Y%m%d')
    fromdate  = (date.today() - timedelta(days=days_back)).strftime('%Y%m%d')

    print(f"[KR FETCH] ticker={bare} tf={timeframe} from={fromdate} to={todate}")

    df = krx.get_market_ohlcv_by_date(fromdate, todate, bare)

    # ETF fallback: try get_etf_ohlcv_by_date when market API returns empty
    if df is None or df.empty:
        print(f"[KR FETCH] market API empty for {bare}, trying ETF API")
        try:
            df = krx.get_etf_ohlcv_by_date(fromdate, todate, bare)
        except Exception as e:
            logger.warning("[KR FETCH] ETF API failed for %s: %s", bare, e)

    # Naver fchart fallback (XML API)
    if df is None or df.empty:
        print(f"[KR FETCH] pykrx 모두 실패 → Naver fchart 시도: {bare}")
        try:
            df_fc = _fetch_naver_ohlcv(bare, count=days_back)
            if df_fc is not None and not df_fc.empty:
                print(f"[KR FETCH] Naver fchart OK: {bare} rows={len(df_fc)}")
                return df_fc
        except Exception as e:
            logger.warning("[KR FETCH] Naver fchart failed for %s: %s", bare, e)

    # Naver sise_day fallback (HTML 스크래핑, 가장 신뢰도 높음)
    if df is None or df.empty:
        print(f"[KR FETCH] Naver sise_day 시도: {bare}")
        try:
            daily_pages = {
                '1h': 7, '4h': 7, '12h': 7, '1d': 7,
                '1w': 50, '1y': 120,
            }.get(timeframe, 7)
            df_daily = fetch_naver_daily_ohlcv(bare, pages=daily_pages)
            if df_daily is not None and not df_daily.empty:
                return df_daily
        except Exception as e:
            logger.warning("[KR FETCH] Naver sise_day failed for %s: %s", bare, e)

    if df is None or df.empty:
        raise ValueError(
            f"데이터 없음: {bare}. pykrx/Naver fchart/sise_day 모두 실패."
        )

    df = df.rename(columns={
        '시가': 'open', '고가': 'high', '저가': 'low',
        '종가': 'close', '거래량': 'volume',
    })
    keep = [c for c in ['open', 'high', 'low', 'close', 'volume'] if c in df.columns]
    df = df[keep].copy()
    if 'volume' not in df.columns:
        df['volume'] = 0

    df.index = pd.to_datetime(df.index)
    if df.index.tz is None:
        df.index = df.index.tz_localize('Asia/Seoul').tz_convert('UTC')

    df = df.dropna(subset=['open', 'high', 'low', 'close'])
    for c in ['open', 'high', 'low', 'close', 'volume']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df = df.dropna(subset=['open', 'close'])

    print(f"[KR FETCH OK] {bare} tf={timeframe} rows={len(df)}")
    return df


def create_kr_stock_chart(ticker: str, name: str, timeframe: str = '1d'):
    """
    KRX candlestick chart using pykrx (OHLCV) + KIS API (현재가/등락/거래량).

    1d / 1w / 1y : full support
    1h / 4h / 12h: pykrx has no intraday data → returns daily candles with a note

    Returns (file_path, caption) or raises.
    """
    bare = _bare_kr_ticker(ticker)
    intraday_fallback = timeframe in ('1h', '4h', '12h')
    effective_tf = '1d' if intraday_fallback else timeframe

    df_raw = fetch_kr_stock_ohlcv(bare, effective_tf)

    # 52w stats always derived from the raw (daily) data
    high_52w = float(df_raw.tail(252)['high'].max())
    low_52w  = float(df_raw.tail(252)['low'].min())

    if effective_tf == '1w':
        df = _resample_ohlcv(df_raw, 'W').tail(60)
    elif effective_tf == '1y':
        df = _to_monthly(df_raw)
    else:
        df = df_raw.tail(60)

    if df.empty:
        raise ValueError(f"차트 데이터 없음: {bare}")

    # 시장 정보 캐시에서 조회 (KIS API 호출 전에 결정)
    try:
        cache  = _get_krx_cache()
        _, market = cache['by_ticker'].get(bare, (name, ''))
    except Exception:
        market = ''

    # ── 현재가: KIS API 우선, 실패 시 pykrx 마지막 종가 ──────────────
    kis = fetch_kis_stock_price(bare, market=market)
    if kis and kis['price'] > 0:
        current_price = float(kis['price'])
        change_pct    = kis['change_pct']
        change_amt    = kis['change']
        volume        = kis['volume']
        price_source  = 'KIS'
        print(f"[KR CHART] KIS 현재가: ₩{current_price:,.0f} ({change_pct:+.2f}%)")
    else:
        current_price = float(df['close'].iloc[-1])
        prev_price    = float(df['close'].iloc[-2]) if len(df) >= 2 else current_price
        change_amt    = int(round(current_price - prev_price))
        change_pct    = (current_price / prev_price - 1) * 100 if prev_price else 0.0
        last_row      = df_raw.tail(1)
        volume        = int(last_row['volume'].iloc[0]) if 'volume' in df_raw.columns and not last_row.empty else 0
        price_source  = 'pykrx'
        print(f"[KR CHART] pykrx 마지막 종가: ₩{current_price:,.0f} (KIS API 미설정/실패)")

    tf_label = _TIMEFRAME_LABEL.get(timeframe, timeframe.upper())

    if intraday_fallback:
        title_tf   = f"1D (요청: {tf_label})"
        caption_tf = f"1D ⚠️ (한국주식은 {tf_label} 미지원 → 일봉 표시)"
    else:
        title_tf   = tf_label
        caption_tf = tf_label

    # 등락률 부호 기호
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


# ── 한국투자증권 OpenAPI ───────────────────────────────────────────────

_KIS_TOKEN_CACHE: dict = {'token': None, 'expires_at': 0.0}
_KIS_BASE = 'https://openapi.koreainvestment.com:9443'


def get_kis_access_token() -> 'str | None':
    """
    한국투자증권 OpenAPI OAuth2 access token 발급 (24h 캐시).
    환경변수 KIS_APP_KEY / KIS_APP_SECRET 필요.
    없으면 None 반환 (graceful fallback).
    """
    import time
    global _KIS_TOKEN_CACHE

    app_key    = os.getenv('KIS_APP_KEY', '').strip()
    app_secret = os.getenv('KIS_APP_SECRET', '').strip()
    if not app_key or not app_secret:
        logger.debug("[KIS TOKEN] KIS_APP_KEY/KIS_APP_SECRET 미설정 → pykrx fallback")
        return None

    # 유효한 토큰 재사용
    if _KIS_TOKEN_CACHE['token'] and time.time() < _KIS_TOKEN_CACHE['expires_at']:
        return _KIS_TOKEN_CACHE['token']

    try:
        r = requests.post(
            f'{_KIS_BASE}/oauth2/tokenP',
            json={
                'grant_type': 'client_credentials',
                'appkey':     app_key,
                'appsecret':  app_secret,
            },
            headers={'Content-Type': 'application/json'},
            timeout=10,
        )
        if r.status_code != 200:
            logger.error("[KIS TOKEN] HTTP %d: %s", r.status_code, r.text[:200])
            return None
        data = r.json()
        token = data.get('access_token')
        if not token:
            logger.error("[KIS TOKEN] access_token 없음: %s", data)
            return None
        expires_in = int(data.get('expires_in', 86400))
        _KIS_TOKEN_CACHE['token']      = token
        _KIS_TOKEN_CACHE['expires_at'] = time.time() + expires_in - 600  # 10분 여유
        logger.info("[KIS TOKEN] 발급 성공 (expires_in=%ds)", expires_in)
        print(f"[KIS TOKEN] 발급 성공")
        return token
    except Exception as e:
        logger.error("[KIS TOKEN] 발급 실패: %s", e)
        return None


def fetch_kis_stock_price(code: str, market: str = '') -> 'dict | None':
    """
    한국투자증권 OpenAPI 현재가 조회 (TR_ID: FHKST01010100).

    code  : 6자리 종목코드 (예: '328130')
    market: 'KOSPI' | 'KOSDAQ' | 'ETF' | '' (빈 값이면 캐시에서 자동 조회)

    시장 코드:
      KOSPI  → fid_cond_mrkt_div_code = 'J'
      KOSDAQ → fid_cond_mrkt_div_code = 'Q'
      ETF    → 'J' 시도 후 실패 시 'Q' fallback

    Returns dict or None.
    """
    token = get_kis_access_token()
    if not token:
        return None

    app_key    = os.getenv('KIS_APP_KEY', '').strip()
    app_secret = os.getenv('KIS_APP_SECRET', '').strip()

    # ── 시장 코드 결정 ────────────────────────────────────────────────
    if not market:
        try:
            cache = _get_krx_cache()
            _, market = cache['by_ticker'].get(code, (code, 'KOSPI'))
        except Exception:
            market = 'KOSPI'

    _MKT_CODE = {'KOSPI': 'J', 'KOSDAQ': 'Q', 'ETF': 'J', 'KONEX': 'J'}
    primary   = _MKT_CODE.get(market, 'J')
    # fallback 순서: 주 시장 코드 → 반대 코드 (J↔Q) → ETF 특수 처리
    if primary == 'J':
        candidates = ['J', 'Q']
    else:
        candidates = ['Q', 'J']

    def _int(d: dict, key: str) -> int:
        try:
            return int(d.get(key) or 0)
        except (ValueError, TypeError):
            return 0

    def _float(d: dict, key: str) -> float:
        try:
            return float(d.get(key) or 0.0)
        except (ValueError, TypeError):
            return 0.0

    headers = {
        'authorization': f'Bearer {token}',
        'appkey':        app_key,
        'appsecret':     app_secret,
        'tr_id':         'FHKST01010100',
        'custtype':      'P',
        'Content-Type':  'application/json; charset=utf-8',
    }

    for mrkt_code in candidates:
        print(f"[KIS REQUEST] code={code} market={market} fid_cond_mrkt_div_code={mrkt_code}")
        try:
            r = requests.get(
                f'{_KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-price',
                headers=headers,
                params={
                    'fid_cond_mrkt_div_code': mrkt_code,  # 소문자 필수
                    'fid_input_iscd':         code,
                },
                timeout=10,
            )
            print(f"[KIS RESPONSE] HTTP {r.status_code}")
            if r.status_code != 200:
                logger.error("[KIS PRICE] %s HTTP %d: %s", code, r.status_code, r.text[:300])
                continue

            j      = r.json()
            rt_cd  = j.get('rt_cd',  '?')
            msg_cd = j.get('msg_cd', '')
            msg1   = j.get('msg1',   '')
            print(f"[KIS RESPONSE] rt_cd={rt_cd} msg_cd={msg_cd} msg1={msg1}")

            if rt_cd != '0':
                logger.error(
                    "[KIS PRICE] %s mrkt=%s rt_cd=%s msg_cd=%s msg1=%s",
                    code, mrkt_code, rt_cd, msg_cd, msg1,
                )
                # 종목코드 오류류면 fallback 불필요
                if msg_cd in ('OPSP0003U', 'EGW00123'):
                    break
                continue  # 다른 시장 코드로 재시도

            out   = j.get('output', {})
            price = _int(out, 'stck_prpr')

            if price == 0:
                # 시장 닫힘 등으로 0 반환 → 다른 코드 시도
                print(f"[KIS RESPONSE] price=0 for {code} mrkt={mrkt_code} → next candidate")
                continue

            change     = _int(out,   'prdy_vrss')
            change_pct = _float(out, 'prdy_ctrt')
            volume     = _int(out,   'acml_vol')
            open_p     = _int(out,   'stck_oprc')
            high_p     = _int(out,   'stck_hgpr')
            low_p      = _int(out,   'stck_lwpr')

            # prdy_vrss_sign: 1=상한 2=상승 3=보합 4=하한 5=하락
            sign_code = out.get('prdy_vrss_sign', '3')
            if sign_code in ('4', '5'):
                change     = -abs(change)
                change_pct = -abs(change_pct)
            else:
                change     = abs(change)
                change_pct = abs(change_pct) if sign_code in ('1', '2') else change_pct

            logger.info("[KIS PRICE] code=%s price=%s change_pct=%s%%", code, price, change_pct)
            print(f"[KIS PRICE] code={code} mrkt={mrkt_code} price={price:,} change_pct={change_pct:+.2f}%")
            return {
                'price':      price,
                'change':     change,
                'change_pct': change_pct,
                'volume':     volume,
                'open':       open_p,
                'high':       high_p,
                'low':        low_p,
                'mrkt_code':  mrkt_code,
            }

        except Exception as e:
            logger.error("[KIS PRICE] %s mrkt=%s 오류: %s", code, mrkt_code, e)

    logger.warning("[KIS PRICE] %s: 모든 시장 코드 실패", code)
    return None


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
