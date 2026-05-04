"""
chart_utils.py
Bybit REST API + yfinance + pykrx 캔들스틱 차트 생성 유틸리티.
단일 axes, 캔들만 표시 — 볼륨 / MA / 52W 라인 / legend 없음.
"""

import logging
import tempfile
from datetime import date, datetime, timedelta
from io import StringIO

import requests
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.ticker as mticker

logger = logging.getLogger(__name__)

VALID_INTERVALS = {'1d', '1h', '4h', '15m', '5m'}

_TIMEFRAME_DATE_FMT = {
    '1d':  '%m/%d',
    '1h':  '%m/%d %H:%M',
    '4h':  '%m/%d %H:%M',
    '15m': '%H:%M',
    '5m':  '%H:%M',
}

# Bybit v5 interval 변환표
_BYBIT_INTERVAL = {
    '1d':  'D',
    '1h':  '60',
    '4h':  '240',
    '15m': '15',
    '5m':  '5',
}

# yfinance interval/period 변환표
_YF_PARAMS = {
    '1d':  ('1d',  '1y'),
    '1h':  ('1h',  '30d'),
    '4h':  ('1h',  '60d'),
    '15m': ('15m', '7d'),
    '5m':  ('5m',  '5d'),
}


# ═══════════════════════════════════════════════════
# 포매터
# ═══════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════
# Bybit REST API (코인 /ac /ap)
# ═══════════════════════════════════════════════════

def _base_from_symbol(symbol: str) -> str:
    """'BTC/USDT' → 'BTC'"""
    return symbol.split('/')[0].upper()


def _to_bybit_perps_symbol(user_input: str) -> str:
    """
    사용자 입력을 Bybit perps symbol로 변환.
    BTC / btc / BTC/USDT / BTCUSDT → 모두 'BTCUSDT'
    """
    s = user_input.upper().strip()
    if '/' in s:              # BTC/USDT → BTC
        s = s.split('/')[0]
    if s.endswith('USDT'):    # BTCUSDT → 그대로
        return s
    return s + 'USDT'         # BTC → BTCUSDT


def _bybit_kline(category: str, symbol: str, timeframe: str, limit: int = 365):
    """
    Bybit v5 /market/kline 직접 호출.
    성공 시 DataFrame(open/high/low/close, DatetimeIndex UTC), 실패 시 None.
    """
    interval = _BYBIT_INTERVAL.get(timeframe, 'D')
    url = "https://api.bybit.com/v5/market/kline"
    params = {
        "category": category,
        "symbol":   symbol,
        "interval": interval,
        "limit":    limit,
    }
    label = f"Bybit {category} {symbol}"
    print(f"[AC TRY] {label} {timeframe}")

    try:
        r = requests.get(url, params=params, timeout=20)
        print(f"[BYBIT STATUS] {r.status_code} {r.text[:300]}")

        if r.status_code != 200:
            print(f"[AC FAIL] {label} HTTP {r.status_code}")
            return None

        data = r.json()
        if data.get('retCode') != 0:
            print(f"[AC FAIL] {label} retCode={data.get('retCode')} msg={data.get('retMsg')}")
            return None

        rows = data.get('result', {}).get('list', [])
        if not rows:
            print(f"[AC FAIL] {label} empty list")
            return None

        # list 는 최신순 → timestamp 오름차순 정렬
        rows = sorted(rows, key=lambda x: int(x[0]))

        df = pd.DataFrame(
            rows,
            columns=['ts', 'open', 'high', 'low', 'close', 'volume', 'turnover'],
        )
        df.index = pd.to_datetime(df['ts'].astype(int), unit='ms', utc=True)
        df.index.name = 'timestamp'

        for col in ['open', 'high', 'low', 'close']:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        df = df[['open', 'high', 'low', 'close']].dropna()

        if df.empty:
            print(f"[AC FAIL] {label} empty after conversion")
            return None

        print(f"[AC OK] {label} rows={len(df)}")
        return df

    except Exception as e:
        print(f"[AC FAIL] {label} {e}")
        logger.warning("[AC FAIL] %s: %s", label, e)
        return None


def _fetch_spot(symbol: str, timeframe: str, limit: int = 365):
    """
    /ac: Bybit spot → Bybit perps
    Returns (df, label) or (None, None).
    """
    base         = _base_from_symbol(symbol)
    bybit_symbol = f"{base}USDT"

    df = _bybit_kline('spot', bybit_symbol, timeframe, limit)
    if df is not None:
        return df, 'Bybit spot'

    df = _bybit_kline('linear', bybit_symbol, timeframe, limit)
    if df is not None:
        return df, 'Bybit perps'

    return None, None


def _fetch_perps(symbol: str, timeframe: str, limit: int = 365):
    """
    /ap: Bybit perps only  (내부 호환용, create_perps_chart는 _fetch_perps_ap 사용)
    Returns (df, label) or (None, None).
    """
    base         = _base_from_symbol(symbol)
    bybit_symbol = f"{base}USDT"

    df = _bybit_kline('linear', bybit_symbol, timeframe, limit)
    if df is not None:
        return df, 'Bybit perps'

    return None, None


def _fetch_perps_ap(symbol: str, timeframe: str, limit: int = 365):
    """
    /ap 전용: Bybit v5 linear kline 직접 호출.
    symbol = 'BTC/USDT', 'BTC', 'BTCUSDT', 'btc' 모두 허용.
    Returns (df, None) on success, (None, err_msg) on failure.
    """
    bybit_symbol = _to_bybit_perps_symbol(symbol)
    interval     = _BYBIT_INTERVAL.get(timeframe, 'D')
    url          = "https://api.bybit.com/v5/market/kline"
    params = {
        "category": "linear",
        "symbol":   bybit_symbol,
        "interval": interval,
        "limit":    limit,
    }

    try:
        r = requests.get(url, params=params, timeout=20)
        print(f"[AP BYBIT URL] {r.url}")
        print(f"[AP BYBIT STATUS] {r.status_code}")
        print(f"[AP BYBIT BODY] {r.text[:500]}")

        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"

        data = r.json()
        if data.get('retCode') != 0:
            print(f"[AP BYBIT ERROR] {data}")
            return None, data.get('retMsg', 'unknown error')

        rows = data.get('result', {}).get('list', [])
        if not rows:
            return None, "empty list"

        # Bybit 는 최신순 → timestamp 오름차순 정렬
        rows = sorted(rows, key=lambda x: int(x[0]))

        df = pd.DataFrame(
            rows,
            columns=['ts', 'open', 'high', 'low', 'close', 'volume', 'turnover'],
        )
        df.index = pd.to_datetime(df['ts'].astype(int), unit='ms', utc=True)
        df.index.name = 'timestamp'

        for col in ['open', 'high', 'low', 'close']:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        df = df[['open', 'high', 'low', 'close']].dropna()

        if df.empty:
            return None, "empty after conversion"

        print(f"[AP BYBIT OK] {bybit_symbol} timeframe={timeframe} rows={len(df)}")
        return df, None

    except Exception as e:
        print(f"[AP BYBIT FAIL] {e}")
        logger.warning("[AP BYBIT FAIL] %s %s: %s", symbol, timeframe, e)
        return None, str(e)


# ═══════════════════════════════════════════════════
# yfinance (코인 fallback + 미국 주식)
# ═══════════════════════════════════════════════════

def _fetch_crypto_yf(symbol: str, timeframe: str):
    """
    코인 Bybit API 전체 실패 시 yfinance fallback.
    'BTC/USDT' → 'BTC-USD'
    Returns (df, 'yfinance') or raises Exception.
    """
    import yfinance as yf

    base      = _base_from_symbol(symbol)
    yf_symbol = f"{base}-USD"
    interval, period = _YF_PARAMS.get(timeframe, ('1d', '1y'))

    # 1. Ticker.history
    print(f"[AC TRY] yfinance.Ticker {yf_symbol} {timeframe}")
    try:
        t  = yf.Ticker(yf_symbol)
        df = t.history(period=period, interval=interval)
        df = _normalize_yf_df(df)
        if df is not None:
            print(f"[AC OK] yfinance.Ticker {yf_symbol} rows={len(df)}")
            return df, 'yfinance'
    except Exception as e:
        print(f"[AC FAIL] yfinance.Ticker {yf_symbol} {e}")

    # 2. download
    print(f"[AC TRY] yfinance.download {yf_symbol} {timeframe}")
    try:
        data = yf.download(yf_symbol, period=period, interval=interval,
                           progress=False, auto_adjust=False)
        df = _normalize_yf_df(data)
        if df is not None:
            print(f"[AC OK] yfinance.download {yf_symbol} rows={len(df)}")
            return df, 'yfinance'
    except Exception as e:
        print(f"[AC FAIL] yfinance.download {yf_symbol} {e}")

    raise ValueError(f"yfinance 코인 조회 실패: {yf_symbol}")


def _normalize_yf_df(data) -> pd.DataFrame | None:
    """yfinance DataFrame → open/high/low/close 소문자 컬럼, UTC tz."""
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
    data = data[req].dropna()
    return data if not data.empty else None


def _fetch_us_yf(ticker: str, timeframe: str) -> pd.DataFrame:
    """
    미국 주식 yfinance 조회.
    Ticker.history → download 순서로 시도.
    """
    import yfinance as yf

    interval, period = _YF_PARAMS.get(timeframe, ('1d', '1y'))

    # 1. Ticker.history
    print(f"[AU TRY] yfinance.Ticker {ticker} {timeframe}")
    try:
        t  = yf.Ticker(ticker)
        df = t.history(period=period, interval=interval)
        df = _normalize_yf_df(df)
        if df is not None:
            print(f"[AU OK] yfinance.Ticker {ticker} rows={len(df)}")
            return df
    except Exception as e:
        print(f"[AU FAIL] yfinance.Ticker {ticker} {e}")

    # 2. download
    print(f"[AU TRY] yfinance.download {ticker} {timeframe}")
    try:
        data = yf.download(ticker, period=period, interval=interval,
                           progress=False, auto_adjust=False)
        df = _normalize_yf_df(data)
        if df is not None:
            print(f"[AU OK] yfinance.download {ticker} rows={len(df)}")
            return df
    except Exception as e:
        print(f"[AU FAIL] yfinance.download {ticker} {e}")

    raise ValueError(f"yfinance 미국주식 조회 실패: {ticker}")


def _fetch_us_stooq(ticker: str) -> pd.DataFrame:
    """Stooq daily CSV fallback (일봉만 지원)."""
    stooq_symbol = ticker.lower() + '.us'
    url = f"https://stooq.com/q/d/l/?s={stooq_symbol}&i=d"

    print(f"[AU TRY] Stooq {ticker}")
    try:
        r = requests.get(url, timeout=20)
        print(f"[STOOQ STATUS] {r.status_code} {r.text[:200]}")

        if r.status_code != 200:
            raise ValueError(f"Stooq HTTP {r.status_code}")

        df = pd.read_csv(StringIO(r.text))
        if df.empty or 'Date' not in df.columns:
            raise ValueError("Stooq 응답 파싱 실패")

        df = df.rename(columns={
            'Date': 'timestamp', 'Open': 'open',
            'High': 'high', 'Low': 'low', 'Close': 'close',
        })
        df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
        df = df.set_index('timestamp').sort_index()
        df = df[['open', 'high', 'low', 'close']].dropna()

        if df.empty:
            raise ValueError(f"Stooq 유효 데이터 없음: {ticker}")

        print(f"[AU OK] Stooq {ticker} rows={len(df)}")
        return df

    except Exception as e:
        print(f"[AU FAIL] Stooq {ticker} {e}")
        logger.warning("[AU FAIL] Stooq %s: %s", ticker, e)
        raise


# ═══════════════════════════════════════════════════
# 한국 주식 (pykrx) — 변경 없음
# ═══════════════════════════════════════════════════

KR_STOCK_MAP = {
    "삼성전자":          ("005930", "삼성전자"),
    "삼전":              ("005930", "삼성전자"),
    "SK하이닉스":        ("000660", "SK하이닉스"),
    "하이닉스":          ("000660", "SK하이닉스"),
    "현대차":            ("005380", "현대차"),
    "현대자동차":        ("005380", "현대차"),
    "기아":              ("000270", "기아"),
    "NAVER":             ("035420", "NAVER"),
    "네이버":            ("035420", "NAVER"),
    "카카오":            ("035720", "카카오"),
    "LG에너지솔루션":    ("373220", "LG에너지솔루션"),
    "셀트리온":          ("068270", "셀트리온"),
    "삼성바이오로직스":  ("207940", "삼성바이오로직스"),
    "삼성SDI":           ("006400", "삼성SDI"),
    "LG화학":            ("051910", "LG화학"),
    "포스코":            ("005490", "POSCO홀딩스"),
    "POSCO":             ("005490", "POSCO홀딩스"),
    "KB금융":            ("105560", "KB금융"),
    "신한지주":          ("055550", "신한지주"),
    "하나금융지주":      ("086790", "하나금융지주"),
    "카카오뱅크":        ("323410", "카카오뱅크"),
    "미래에셋증권":      ("006800", "미래에셋증권"),
    "미래에셋":          ("006800", "미래에셋증권"),
    "키움증권":          ("039490", "키움증권"),
    "키움":              ("039490", "키움증권"),
}


def normalize_kr_name(text: str) -> str:
    return text.strip().replace(" ", "").upper()


def get_latest_krx_date() -> str:
    try:
        from pykrx import stock
    except ImportError:
        return datetime.now().strftime("%Y%m%d")

    today = datetime.now()
    for i in range(14):
        d = today - timedelta(days=i)
        date_str = d.strftime("%Y%m%d")
        try:
            tickers = stock.get_market_ticker_list(date=date_str, market="KOSPI")
            if tickers and len(tickers) > 0:
                return date_str
        except Exception as e:
            logger.warning("[KRX DATE ERROR] %s %s", date_str, e)
    return datetime.now().strftime("%Y%m%d")


def find_kr_stock(query: str):
    query_raw  = query.strip()
    query_norm = query_raw.replace(" ", "").upper()

    print("[KRX SEARCH] raw=%s norm=%s" % (query_raw, query_norm))

    if query_norm in KR_STOCK_MAP:
        ticker, name = KR_STOCK_MAP[query_norm]
        print("[KRX MAP FOUND]", ticker, name)
        return ticker, name

    if query_raw.isdigit() and len(query_raw) == 6:
        try:
            from pykrx import stock
            name = stock.get_market_ticker_name(query_raw)
            if name:
                print("[KRX FOUND BY CODE]", query_raw, name)
                return query_raw, name
        except Exception as e:
            logger.warning("[KRX CODE ERROR] %s %s", query_raw, e)
        return None, []

    try:
        from pykrx import stock
    except ImportError:
        logger.error("pykrx 미설치")
        return None, []

    query_norm_upper = normalize_kr_name(query_raw)
    date_str = get_latest_krx_date()
    logger.info("[KRX DATE USED] %s", date_str)

    exact_matches   = []
    partial_matches = []

    for market in ("KOSPI", "KOSDAQ", "KONEX"):
        try:
            tickers = stock.get_market_ticker_list(date=date_str, market=market)
            logger.info("[KRX MARKET] %s count=%d", market, len(tickers))
            for ticker in tickers:
                name      = stock.get_market_ticker_name(ticker)
                name_norm = normalize_kr_name(name)
                if name_norm == query_norm_upper:
                    exact_matches.append((ticker, name))
                elif query_norm_upper in name_norm or name_norm in query_norm_upper:
                    partial_matches.append((ticker, name))
        except Exception as e:
            logger.warning("[KRX MARKET ERROR] %s %s", market, e)

    if exact_matches:
        logger.info("[KRX EXACT] %s", exact_matches[0])
        return exact_matches[0][0], exact_matches[0][1]

    if partial_matches:
        logger.info("[KRX PARTIAL] %s", partial_matches[:5])
        return None, partial_matches[:5]

    logger.info("[KRX NO MATCH] %s", query_raw)
    return None, []


def create_kr_stock_chart(ticker: str, name: str, timeframe: str = '1d'):
    """
    KRX 티커와 종목명을 직접 받아 차트를 생성한다.
    Returns (file_path, caption) or raises Exception.
    """
    try:
        from pykrx import stock as krx
    except ImportError:
        raise ImportError("pykrx 패키지가 설치되지 않았습니다: pip install pykrx")

    todate   = date.today().strftime("%Y%m%d")
    fromdate = (date.today() - timedelta(days=400)).strftime("%Y%m%d")

    df = krx.get_market_ohlcv_by_date(fromdate, todate, ticker)
    if df is None or df.empty:
        raise ValueError(f"데이터를 가져올 수 없습니다: {ticker}")

    df = df.rename(columns={
        '시가': 'open', '고가': 'high', '저가': 'low',
        '종가': 'close', '거래량': 'volume',
    })
    df = df[['open', 'high', 'low', 'close', 'volume']]

    if df.index.tz is None:
        df.index = pd.to_datetime(df.index).tz_localize('Asia/Seoul').tz_convert('UTC')

    current_price = float(df['close'].iloc[-1])
    prev_price    = float(df['close'].iloc[-2]) if len(df) >= 2 else current_price

    df_52w   = df.tail(252)
    high_52w = float(df_52w['high'].max())
    low_52w  = float(df_52w['low'].min())

    title    = f"{name} ({ticker}) - 1D - KRX"
    tmp_path = _make_tmp_path()
    _draw_chart(df, title, timeframe, tmp_path)

    caption = (
        f"📊 {name} ({ticker}) 차트 (1d)\n\n"
        f"현재가: {_fmt_kr(current_price)} KRW\n"
        f"전일대비: {_change_line(current_price, prev_price, _fmt_kr)}\n"
        f"52주 최고가: {_fmt_kr(high_52w)} KRW\n"
        f"52주 최저가: {_fmt_kr(low_52w)} KRW"
    )
    return tmp_path, caption


# ═══════════════════════════════════════════════════
# 차트 그리기 — 변경 없음
# ═══════════════════════════════════════════════════

def _make_tmp_path() -> str:
    tmp = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
    tmp.close()
    return tmp.name


def _draw_chart(df: pd.DataFrame, title: str, timeframe: str, tmp_path: str) -> None:
    """단일 axes 캔들스틱 차트. 1400×900 px. 볼륨/MA/legend 없음."""
    df = df.tail(80).copy()
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

    price_low   = df['low'].min()
    price_high  = df['high'].max()
    price_range = max(price_high - price_low, price_high * 1e-6)
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


# ═══════════════════════════════════════════════════
# 공개 함수
# ═══════════════════════════════════════════════════

def create_clean_candlestick_chart(symbol: str, timeframe: str = '1d') -> dict:
    """
    /ac: Bybit spot → Bybit perps → yfinance fallback
    """
    result = {
        'success': False, 'file_path': None, 'current_price': None,
        'symbol': symbol, 'timeframe': timeframe, 'exchange': None,
        'error': None, 'currency': '$', 'caption': '',
    }
    try:
        # 1. Bybit REST API
        df, exchange_name = _fetch_spot(symbol, timeframe, 365)

        # 2. yfinance fallback
        if df is None:
            print(f"[AC FALLBACK] Bybit 실패 → yfinance: {symbol}")
            try:
                df, exchange_name = _fetch_crypto_yf(symbol, timeframe)
            except Exception as yf_err:
                print(f"[AC FALLBACK FAIL] yfinance: {yf_err}")
                result['error'] = (
                    f"해당 코인 데이터를 가져올 수 없습니다: {symbol.split('/')[0]}\n"
                    f"시도: Bybit spot, Bybit perps, yfinance"
                )
                return result

        result['exchange'] = exchange_name
        current_price = float(df['close'].iloc[-1])
        prev_price    = float(df['close'].iloc[-2]) if len(df) >= 2 else current_price
        result['current_price'] = current_price

        # 52주 고/저
        high_52w = low_52w = None
        if timeframe == '1d':
            # 이미 365일치 있으므로 그대로 사용
            high_52w = float(df['high'].max())
            low_52w  = float(df['low'].min())
        elif exchange_name == 'yfinance':
            # yfinance는 1y 데이터
            high_52w = float(df['high'].max())
            low_52w  = float(df['low'].min())
        else:
            # 비일봉: 1d 365봉 별도 조회
            df_1d, _ = _fetch_spot(symbol, '1d', 365)
            if df_1d is None:
                try:
                    df_1d, _ = _fetch_crypto_yf(symbol, '1d')
                except Exception:
                    df_1d = None
            if df_1d is not None:
                high_52w = float(df_1d['high'].max())
                low_52w  = float(df_1d['low'].min())

        base  = symbol.split('/')[0] + 'USDT'
        title = f"{base} - {timeframe.upper()} - {exchange_name}"
        tmp_path = _make_tmp_path()
        _draw_chart(df, title, timeframe, tmp_path)

        lines = [
            f"📊 {symbol} 차트 ({timeframe})\n",
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

    except Exception as e:
        logger.error("create_clean_candlestick_chart 오류: %s", e)
        result['error'] = str(e)
        plt.close('all')

    return result


def create_perps_chart(symbol: str, timeframe: str = '1d') -> dict:
    """
    /ap: Bybit perps only (category=linear, _fetch_perps_ap 사용)
    """
    bybit_symbol = _to_bybit_perps_symbol(symbol)
    base_name    = bybit_symbol.replace('USDT', '')   # 'BTC'

    result = {
        'success': False, 'file_path': None, 'current_price': None,
        'symbol': symbol, 'timeframe': timeframe, 'exchange': 'Bybit perps',
        'error': None, 'currency': '$', 'caption': '',
    }
    try:
        df, err_msg = _fetch_perps_ap(symbol, timeframe, 365)
        if df is None:
            result['error'] = (
                f"해당 선물 데이터를 가져올 수 없습니다: {base_name}\n"
                f"Bybit 응답: {err_msg}"
            )
            return result

        current_price = float(df['close'].iloc[-1])
        prev_price    = float(df['close'].iloc[-2]) if len(df) >= 2 else current_price
        result['current_price'] = current_price

        # 52주 고/저
        high_52w = low_52w = None
        if timeframe == '1d':
            high_52w = float(df['high'].max())
            low_52w  = float(df['low'].min())
        else:
            df_1d, err_1d = _fetch_perps_ap(symbol, '1d', 365)
            if df_1d is not None:
                high_52w = float(df_1d['high'].max())
                low_52w  = float(df_1d['low'].min())

        title    = f"{bybit_symbol} - {timeframe.upper()} - Bybit perps"
        tmp_path = _make_tmp_path()
        _draw_chart(df, title, timeframe, tmp_path)

        lines = [
            f"📊 {bybit_symbol} 차트 ({timeframe})\n",
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

    except Exception as e:
        logger.error("create_perps_chart 오류: %s", e)
        result['error'] = str(e)
        plt.close('all')

    return result


def create_us_stock_chart(ticker: str, timeframe: str = '1d') -> dict:
    """
    /au: yfinance (Ticker.history → download) → Stooq fallback
    """
    ticker = ticker.upper().strip()
    result = {
        'success': False, 'file_path': None, 'current_price': None,
        'symbol': ticker, 'timeframe': timeframe, 'exchange': 'US',
        'error': None, 'currency': '$', 'caption': '',
    }

    if timeframe not in ('1d', '1h'):
        result['error'] = "미국 주식은 1d / 1h 인터벌만 지원합니다."
        return result

    try:
        df          = None
        used_source = None
        used_tf     = timeframe

        # 1. yfinance
        try:
            df          = _fetch_us_yf(ticker, timeframe)
            used_source = 'yfinance'
        except Exception as e:
            print(f"[AU] yfinance 전체 실패: {e}")

        # 2. Stooq fallback (일봉만)
        if df is None:
            try:
                df          = _fetch_us_stooq(ticker)
                used_source = 'Stooq'
                used_tf     = '1d'
                if timeframe == '1h':
                    logger.info("[AU] 1h 실패, Stooq 일봉으로 대체: %s", ticker)
            except Exception as e:
                print(f"[AU] Stooq 실패: {e}")

        if df is None:
            result['error'] = (
                f"미국 주식 데이터를 가져올 수 없습니다: {ticker}\n"
                f"시도: yfinance, Stooq"
            )
            return result

        current_price = float(df['close'].iloc[-1])
        prev_price    = float(df['close'].iloc[-2]) if len(df) >= 2 else current_price
        result['current_price'] = current_price

        # 52주 고/저
        high_52w = low_52w = None
        if used_tf == '1d':
            # 1y 데이터 → 그대로 사용
            high_52w = float(df['high'].max())
            low_52w  = float(df['low'].min())
        else:
            # 1h → 1d 데이터 별도 조회
            try:
                df_1d    = _fetch_us_yf(ticker, '1d')
                high_52w = float(df_1d['high'].max())
                low_52w  = float(df_1d['low'].min())
            except Exception:
                try:
                    df_1d    = _fetch_us_stooq(ticker)
                    high_52w = float(df_1d['high'].max())
                    low_52w  = float(df_1d['low'].min())
                except Exception:
                    pass

        title    = f"{ticker} - {used_tf.upper()} - {used_source}"
        tmp_path = _make_tmp_path()
        _draw_chart(df, title, used_tf, tmp_path)

        lines = [
            f"📊 {ticker} 차트 ({used_tf})\n",
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

    except Exception as e:
        logger.error("create_us_stock_chart 오류: %s", e)
        result['error'] = str(e)
        plt.close('all')

    return result
