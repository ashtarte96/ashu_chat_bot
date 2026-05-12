"""
chart_utils.py
Binance REST API (primary) + Bybit v5 REST API (fallback)
+ yfinance(미국주식) + pykrx(한국주식)
"""

import logging
import tempfile
import traceback as tb
from datetime import date, datetime, timedelta

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

_BINANCE_INTERVAL = {
    '1d': '1d', '1h': '1h', '4h': '4h', '15m': '15m', '5m': '5m',
}

_BYBIT_INTERVAL = {
    '1d': 'D', '1h': '60', '4h': '240', '15m': '15', '5m': '5',
}

_YF_US_PARAMS = {
    '1d': ('1d', '1y'),
    '1h': ('1h', '30d'),
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
# 심볼 변환
# ═══════════════════════════════════════════════════

def _parse_symbol(user_input: str) -> str:
    """BTC / btc / BTC/USDT / BTCUSDT → 'BTCUSDT'"""
    s = user_input.upper().strip()
    if '/' in s:
        s = s.split('/')[0]
    if s.endswith('USDT'):
        return s
    return s + 'USDT'


# ═══════════════════════════════════════════════════
# Binance REST API
# ═══════════════════════════════════════════════════

def fetch_binance_spot(symbol: str, timeframe: str, limit: int = 500) -> 'pd.DataFrame | None':
    """Binance spot kline. symbol='BTCUSDT'"""
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
        print(f"[BINANCE SPOT OK] {symbol} rows={len(df)}")
        return df if not df.empty else None
    except Exception:
        print(f"[BINANCE SPOT FAIL] {symbol} {timeframe}")
        print(tb.format_exc())
        logger.error("[BINANCE SPOT FAIL] %s\n%s", symbol, tb.format_exc())
        return None


def fetch_binance_futures(symbol: str, timeframe: str, limit: int = 500) -> 'pd.DataFrame | None':
    """Binance USDT-M futures kline. symbol='BTCUSDT'"""
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
        print(f"[BINANCE FUTURES OK] {symbol} rows={len(df)}")
        return df if not df.empty else None
    except Exception:
        print(f"[BINANCE FUTURES FAIL] {symbol} {timeframe}")
        print(tb.format_exc())
        logger.error("[BINANCE FUTURES FAIL] %s\n%s", symbol, tb.format_exc())
        return None


def get_funding_rate(symbol: str) -> 'float | None':
    """Binance USDT-M 최신 펀딩비. 실패 시 None 반환."""
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


# ═══════════════════════════════════════════════════
# Bybit v5 REST API
# ═══════════════════════════════════════════════════

def fetch_bybit_spot(symbol: str, timeframe: str, limit: int = 200) -> 'pd.DataFrame | None':
    """Bybit spot kline. symbol='BTCUSDT'"""
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
        rows = sorted(rows, key=lambda x: int(x[0]))  # 최신순 → 오름차순
        df = pd.DataFrame(rows,
                          columns=['ts', 'open', 'high', 'low', 'close', 'volume', 'turnover'])
        df.index = pd.to_datetime(df['ts'].astype(int), unit='ms', utc=True)
        df.index.name = 'timestamp'
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df = df[['open', 'high', 'low', 'close', 'volume']].dropna()
        print(f"[BYBIT SPOT OK] {symbol} rows={len(df)}")
        return df if not df.empty else None
    except Exception:
        print(f"[BYBIT SPOT FAIL] {symbol} {timeframe}")
        print(tb.format_exc())
        logger.error("[BYBIT SPOT FAIL] %s\n%s", symbol, tb.format_exc())
        return None


def fetch_bybit_perps(symbol: str, timeframe: str, limit: int = 200) -> 'pd.DataFrame | None':
    """Bybit USDT linear perps kline. symbol='BTCUSDT'"""
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
        print(f"[BYBIT PERPS OK] {symbol} rows={len(df)}")
        return df if not df.empty else None
    except Exception:
        print(f"[BYBIT PERPS FAIL] {symbol} {timeframe}")
        print(tb.format_exc())
        logger.error("[BYBIT PERPS FAIL] %s\n%s", symbol, tb.format_exc())
        return None


# ═══════════════════════════════════════════════════
# /au: 미국주식 (yfinance only)
# ═══════════════════════════════════════════════════

def _normalize_yf_df(data) -> 'pd.DataFrame | None':
    """yfinance df → open/high/low/close 소문자, UTC tz."""
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
    """yfinance 미국주식 조회. 실패 시 ValueError."""
    import yfinance as yf
    interval, period = _YF_US_PARAMS.get(timeframe, ('1d', '1y'))
    print("[AU TRY]", ticker)
    try:
        raw = yf.Ticker(ticker).history(period=period, interval=interval)
        df  = _normalize_yf_df(raw)
        if df is not None:
            print("[AU OK]", ticker)
            return df
        print("[AU FAIL]", "empty data")
        raise ValueError(f"yfinance 빈 데이터: {ticker}")
    except ValueError:
        raise
    except Exception as e:
        print("[AU FAIL]", e)
        raise ValueError(f"yfinance 조회 실패: {ticker}") from e


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
    """KRX 차트. Returns (file_path, caption) or raises."""
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
    df_52w        = df.tail(252)
    high_52w      = float(df_52w['high'].max())
    low_52w       = float(df_52w['low'].min())

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
# 차트 그리기
# ═══════════════════════════════════════════════════

def _make_tmp_path() -> str:
    tmp = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
    tmp.close()
    return tmp.name


def _draw_chart(df: pd.DataFrame, title: str, timeframe: str, tmp_path: str) -> None:
    """단일 axes 캔들스틱 차트. 최근 80봉. 볼륨/MA/legend 없음."""
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


# ═══════════════════════════════════════════════════
# 공개 함수
# ═══════════════════════════════════════════════════

def create_clean_candlestick_chart(symbol: str, timeframe: str = '1d') -> dict:
    """/ac: Binance spot → Bybit spot fallback"""
    sym = _parse_symbol(symbol)
    result = {
        'success': False, 'file_path': None, 'current_price': None,
        'symbol': sym, 'timeframe': timeframe, 'exchange': None,
        'error': None, 'currency': '$', 'caption': '',
    }
    try:
        # 1. Binance spot
        df     = fetch_binance_spot(sym, timeframe, 500)
        source = 'Binance spot'
        # 2. Bybit spot fallback
        if df is None:
            df     = fetch_bybit_spot(sym, timeframe, 200)
            source = 'Bybit spot'

        if df is None:
            result['error'] = (
                f"코인 데이터를 가져올 수 없습니다: {sym}\n"
                "Binance / Bybit 모두 실패 — 서버 로그를 확인하세요."
            )
            return result

        result['exchange'] = source
        current_price      = float(df['close'].iloc[-1])
        prev_price         = float(df['close'].iloc[-2]) if len(df) >= 2 else current_price
        result['current_price'] = current_price

        # 52주 고/저
        high_52w = low_52w = None
        if timeframe == '1d':
            high_52w = float(df['high'].max())
            low_52w  = float(df['low'].min())
        else:
            df_1d = fetch_binance_spot(sym, '1d', 365) or fetch_bybit_spot(sym, '1d', 365)
            if df_1d is not None:
                high_52w = float(df_1d['high'].max())
                low_52w  = float(df_1d['low'].min())

        title    = f"{sym} - {timeframe.upper()} - {source}"
        tmp_path = _make_tmp_path()
        _draw_chart(df, title, timeframe, tmp_path)

        lines = [
            f"📊 {sym} 차트 ({timeframe})\n",
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
        logger.error("create_clean_candlestick_chart 오류:\n%s", tb.format_exc())
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
        # 1. Binance futures
        df     = fetch_binance_futures(sym, timeframe, 500)
        source = 'Binance futures'
        # 2. Bybit perps fallback
        if df is None:
            df     = fetch_bybit_perps(sym, timeframe, 200)
            source = 'Bybit perps'

        if df is None:
            result['error'] = (
                f"선물 데이터를 가져올 수 없습니다: {sym}\n"
                "Binance futures / Bybit perps 모두 실패 — 서버 로그를 확인하세요."
            )
            return result

        result['exchange'] = source
        current_price      = float(df['close'].iloc[-1])
        prev_price         = float(df['close'].iloc[-2]) if len(df) >= 2 else current_price
        result['current_price'] = current_price

        # 52주 고/저
        high_52w = low_52w = None
        if timeframe == '1d':
            high_52w = float(df['high'].max())
            low_52w  = float(df['low'].min())
        else:
            df_1d = (fetch_binance_futures(sym, '1d', 365) or
                     fetch_bybit_perps(sym, '1d', 365))
            if df_1d is not None:
                high_52w = float(df_1d['high'].max())
                low_52w  = float(df_1d['low'].min())

        # 펀딩비 (Binance, 선택)
        funding = get_funding_rate(sym)

        title    = f"{sym} PERPS - {timeframe.upper()} - {source}"
        tmp_path = _make_tmp_path()
        _draw_chart(df, title, timeframe, tmp_path)

        lines = [
            f"📊 {sym} 선물 차트 ({timeframe})\n",
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
        logger.error("create_perps_chart 오류:\n%s", tb.format_exc())
        result['error'] = "선물 차트 생성 중 오류가 발생했습니다. 서버 로그를 확인하세요."
        plt.close('all')

    return result


def create_us_stock_chart(ticker: str, timeframe: str = '1d') -> dict:
    """/au: yfinance only"""
    ticker = ticker.upper().strip()
    result = {
        'success': False, 'file_path': None, 'current_price': None,
        'symbol': ticker, 'timeframe': timeframe, 'exchange': 'yfinance',
        'error': None, 'currency': '$', 'caption': '',
    }

    if timeframe not in ('1d', '1h'):
        result['error'] = "미국 주식은 1d / 1h 인터벌만 지원합니다."
        return result

    try:
        df = _fetch_us_yf(ticker, timeframe)

        current_price = float(df['close'].iloc[-1])
        prev_price    = float(df['close'].iloc[-2]) if len(df) >= 2 else current_price
        result['current_price'] = current_price

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

        title    = f"{ticker} - {timeframe.upper()} - yfinance"
        tmp_path = _make_tmp_path()
        _draw_chart(df, title, timeframe, tmp_path)

        lines = [
            f"📊 {ticker} 차트 ({timeframe})\n",
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
