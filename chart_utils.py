"""
chart_utils.py
ccxt + yfinance + pykrx 캔들스틱 차트 생성 유틸리티.
단일 axes, 캔들만 표시 — 볼륨 / MA / 52W 라인 / legend 없음.
"""

import logging
import tempfile
from datetime import date, datetime, timedelta

import ccxt
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

_YF_PARAMS = {
    '1d':  ('1d',  '1y'),
    '1h':  ('1h',  '30d'),
    '4h':  ('1h',  '60d'),
    '15m': ('15m', '7d'),
    '5m':  ('5m',  '5d'),
}


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
    """한국 주식 가격: 정수 + 쉼표 구분."""
    return f"{int(round(price)):,}"


def _fmt_us(price: float) -> str:
    """미국 주식 가격: 소수점 2자리."""
    if price >= 1000:
        return f"{price:,.2f}"
    return f"{price:.2f}"


def _change_line(current: float, prev: float, fmt_fn=None) -> str:
    """전일대비 등락 문자열 (예: +1,230.10 (+1.60%))."""
    if fmt_fn is None:
        fmt_fn = format_price
    change = current - prev
    pct    = change / prev * 100 if prev else 0.0
    abs_ch = fmt_fn(abs(change))
    if change >= 0:
        return f"+{abs_ch} (+{pct:.2f}%)"
    return f"-{abs_ch} (-{abs(pct):.2f}%)"


# ═══════════════════════════════════════════════════
# 데이터 조회
# ═══════════════════════════════════════════════════

def _base_from_symbol(symbol: str) -> str:
    """'BTC/USDT' → 'BTC'"""
    return symbol.split('/')[0].upper()


def _make_exchange(cls, extra_opts: dict) -> ccxt.Exchange:
    return cls({
        'enableRateLimit': True,
        'timeout': 30000,
        **extra_opts,
    })


def _try_fetch_on(
    exchange: ccxt.Exchange,
    candidates: list,
    timeframe: str,
    limit: int,
    label: str,
):
    """
    단일 거래소에서 심볼 후보를 순서대로 시도.
    성공 시 (ohlcv, label) 반환, 실패 시 (None, None).
    """
    try:
        markets = exchange.load_markets()
    except Exception as e:
        print(f"[FETCH FAIL] {label} load_markets: {e}")
        logger.warning("[FETCH FAIL] %s load_markets: %s", label, e)
        return None, None

    for symbol in candidates:
        if symbol not in markets:
            continue
        print(f"[FETCH TRY] {label} {symbol} {timeframe}")
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            if ohlcv:
                print(f"[FETCH OK] {label} {symbol}")
                logger.info("[FETCH OK] %s %s", label, symbol)
                return ohlcv, label
        except Exception as e:
            print(f"[FETCH FAIL] {label} {symbol} {e}")
            logger.warning("[FETCH FAIL] %s %s: %s", label, symbol, e)

    return None, None


_AC_ATTEMPTS = [
    # (거래소 클래스, 옵션, 심볼 후보 템플릿, 레이블)
    (ccxt.bybit,   {'options': {'defaultType': 'spot'}},   ['spot'],   'Bybit spot'),
    (ccxt.bybit,   {'options': {'defaultType': 'linear'}}, ['linear'], 'Bybit perps'),
    (ccxt.binance, {},                                      ['spot'],   'Binance spot'),
    (ccxt.binance, {'options': {'defaultType': 'future'}}, ['linear'], 'Binance futures'),
]

_AP_ATTEMPTS = [
    (ccxt.bybit,   {'options': {'defaultType': 'linear'}}, ['linear'], 'Bybit perps'),
    (ccxt.binance, {'options': {'defaultType': 'future'}}, ['linear'], 'Binance futures'),
]


def _build_candidates(base: str, market_types: list) -> list:
    """spot → ['BASE/USDT'], linear → ['BASE/USDT:USDT', 'BASE/USDT']"""
    result = []
    for t in market_types:
        if t == 'spot':
            result += [f"{base}/USDT"]
        elif t == 'linear':
            result += [f"{base}/USDT:USDT", f"{base}/USDT"]
    return result


def _fetch_spot(symbol: str, timeframe: str, limit: int):
    """
    /ac 조회 순서: Bybit spot → Bybit perps → Binance spot → Binance futures
    Returns (ohlcv, label) or (None, None).
    """
    base = _base_from_symbol(symbol)
    for cls, opts, mtype, label in _AC_ATTEMPTS:
        candidates = _build_candidates(base, mtype)
        ex = _make_exchange(cls, opts)
        ohlcv, name = _try_fetch_on(ex, candidates, timeframe, limit, label)
        if ohlcv:
            return ohlcv, name
    return None, None


def _fetch_perps(symbol: str, timeframe: str, limit: int):
    """
    /ap 조회 순서: Bybit perps → Binance futures
    Returns (ohlcv, label) or (None, None).
    """
    base = _base_from_symbol(symbol)
    for cls, opts, mtype, label in _AP_ATTEMPTS:
        candidates = _build_candidates(base, mtype)
        ex = _make_exchange(cls, opts)
        ohlcv, name = _try_fetch_on(ex, candidates, timeframe, limit, label)
        if ohlcv:
            return ohlcv, name
    return None, None


def _to_dataframe(raw) -> pd.DataFrame:
    df = pd.DataFrame(raw, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
    return df.set_index('timestamp')


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
    """최근 유효 KRX 거래일(YYYYMMDD)을 반환한다."""
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
            continue
    return datetime.now().strftime("%Y%m%d")


def find_kr_stock(query: str):
    """
    종목명 또는 코드로 KRX 종목을 검색한다.
    검색 순서: KR_STOCK_MAP → 6자리 코드 → pykrx 동적 검색

    Returns:
        (ticker, name)            — 완전 일치
        (None, [(t, n), ...])     — 부분 일치 후보
        (None, [])                — 미발견
    """
    query_raw  = query.strip()
    query_norm = query_raw.replace(" ", "").upper()

    print("[KRX SEARCH] raw=%s norm=%s" % (query_raw, query_norm))

    # ── 1. 하드코딩 매핑 우선 검색 ──────────────────────
    if query_norm in KR_STOCK_MAP:
        ticker, name = KR_STOCK_MAP[query_norm]
        print("[KRX MAP FOUND]", ticker, name)
        return ticker, name

    # ── 2. 6자리 종목코드 직접 조회 ──────────────────────
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

    # ── 3. pykrx 동적 검색 ───────────────────────────────
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


def _fetch_krx_data(query: str):
    """
    pykrx로 한국 주식 데이터 조회.
    Returns (df, ticker, name) or raises ValueError.
    """
    try:
        from pykrx import stock as krx
    except ImportError:
        raise ImportError("pykrx 패키지가 설치되지 않았습니다: pip install pykrx")

    query = query.strip()

    if query.isdigit() and len(query) == 6:
        ticker = query
        name = krx.get_market_ticker_name(ticker)
        if not name:
            raise ValueError(f"종목 코드를 찾을 수 없습니다: {ticker}")
    else:
        ticker = None
        name = query
        for market in ("KOSPI", "KOSDAQ"):
            try:
                for t in krx.get_market_ticker_list(market=market):
                    if krx.get_market_ticker_name(t) == query:
                        ticker = t
                        break
            except Exception:
                continue
            if ticker:
                break
        if not ticker:
            raise ValueError(f"종목을 찾을 수 없습니다: {query}")

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

    return df, ticker, name


def _fetch_yf_ohlcv(yf_symbol: str, timeframe: str) -> pd.DataFrame:
    """
    yfinance OHLCV 조회. MultiIndex 컬럼 / 소문자 변환 / tz 처리 통합.
    성공 시 open/high/low/close 컬럼의 DataFrame 반환.
    """
    try:
        import yfinance as yf
    except ImportError:
        raise ImportError("yfinance 패키지가 설치되지 않았습니다.")

    interval, period = _YF_PARAMS.get(timeframe, ('1d', '1y'))
    print(f"[YF TRY] {yf_symbol} interval={interval} period={period}")

    data = yf.download(
        yf_symbol,
        period=period,
        interval=interval,
        progress=False,
        auto_adjust=False,
    )

    if data is None or data.empty:
        print(f"[YF FAIL] {yf_symbol} empty dataframe")
        raise ValueError(f"yfinance 데이터 없음: {yf_symbol}")

    # MultiIndex 처리 (yfinance 버전에 따라 발생)
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = [col[0] for col in data.columns]

    # 컬럼명 소문자 통일
    data.columns = [c.lower() for c in data.columns]

    # tz 처리
    if data.index.tz is None:
        data.index = data.index.tz_localize('UTC')

    required = ['open', 'high', 'low', 'close']
    missing = [c for c in required if c not in data.columns]
    if missing:
        print(f"[YF FAIL] {yf_symbol} missing columns: {missing}")
        raise ValueError(f"yfinance 컬럼 누락: {missing}")

    data = data[required].dropna()

    if data.empty:
        print(f"[YF FAIL] {yf_symbol} empty after dropna")
        raise ValueError(f"yfinance 유효 데이터 없음: {yf_symbol}")

    print(f"[YF OK] {yf_symbol} rows={len(data)}")
    return data


def _fetch_crypto_yf(symbol: str, timeframe: str):
    """
    코인 ccxt 실패 시 yfinance fallback.
    'BTC/USDT' → 'BTC-USD' 로 조회.
    Returns (df, 'yfinance') or raises Exception.
    """
    base = _base_from_symbol(symbol)
    yf_symbol = f"{base}-USD"
    df = _fetch_yf_ohlcv(yf_symbol, timeframe)
    return df, 'yfinance'


# ═══════════════════════════════════════════════════
# 차트 그리기
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
    n = len(df)

    UP_COLOR   = '#26a69a'
    DOWN_COLOR = '#ef5350'
    BG_COLOR   = '#111722'
    GRID_COLOR = '#1e2535'
    TEXT_COLOR = '#c7c7c7'
    PRICE_LINE = '#f0c040'
    HIGH_COLOR = '#ef5350'   # 고점 빨간색
    LOW_COLOR  = '#5090f0'   # 저점 파란색

    fig, ax = plt.subplots(figsize=(14, 9), dpi=100)
    fig.patch.set_facecolor(BG_COLOR)
    ax.set_facecolor(BG_COLOR)

    # ── 캔들 ──────────────────────────────────────────
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

    # ── 최고가 / 최저가 화살표 + 가격 ────────────────
    price_low  = df['low'].min()
    price_high = df['high'].max()
    price_range = max(price_high - price_low, price_high * 1e-6)
    arrow_offset = price_range * 0.08

    high_idx = int(df['high'].idxmax())
    low_idx  = int(df['low'].idxmin())
    high_val = df.at[high_idx, 'high']
    low_val  = df.at[low_idx,  'low']

    # 고점 라벨: 왼쪽 1/4 구간이면 오른쪽으로 이동 (티커명 겹침 방지)
    if high_idx < n * 0.25:
        h_xytext = (min(high_idx + max(int(n * 0.10), 3), n - 1), high_val + arrow_offset)
        h_ha = 'left'
    else:
        h_xytext = (high_idx, high_val + arrow_offset)
        h_ha = 'center'

    ax.annotate(
        format_price(high_val),
        xy=(high_idx, high_val),
        xytext=h_xytext,
        arrowprops=dict(arrowstyle='->', color=HIGH_COLOR, lw=1.0, shrinkA=2, shrinkB=2),
        fontsize=16, color=HIGH_COLOR, fontweight='bold',
        va='bottom', ha=h_ha, zorder=6, clip_on=False,
    )
    ax.annotate(
        format_price(low_val),
        xy=(low_idx, low_val),
        xytext=(low_idx, low_val - arrow_offset),
        arrowprops=dict(arrowstyle='->', color=LOW_COLOR, lw=1.0, shrinkA=2, shrinkB=2),
        fontsize=16, color=LOW_COLOR, fontweight='bold',
        va='top', ha='center', zorder=6, clip_on=False,
    )

    # ── 축 범위 (상하 10%) ─────────────────────────────
    pad = price_range * 0.10
    ax.set_ylim(price_low - pad, price_high + pad)
    ax.set_xlim(-1, n)

    # ── y축 오른쪽 (fontsize=15) ──────────────────────
    ax.yaxis.tick_right()
    ax.yaxis.set_label_position('right')
    ax.tick_params(axis='y', labelright=True, labelleft=False,
                   colors=TEXT_COLOR, labelsize=15)
    ax.yaxis.set_major_formatter(
        mticker.FuncFormatter(lambda v, _: format_price(v))
    )

    # ── x축 날짜 라벨 (최대 8개 눈금) ────────────────
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

    # ── 그리드 ────────────────────────────────────────
    ax.grid(True, color=GRID_COLOR, linewidth=0.5, linestyle='--', zorder=0)
    ax.set_axisbelow(True)

    # ── 현재가 점선 + 오른쪽 라벨 (fontsize=15) ───────
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

    # ── 차트 내부 왼쪽 위 제목 (fontsize=24, 반투명 배경) ─
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
    /ac 조회 순서: Bybit spot → Bybit perps → Binance spot → Binance futures → yfinance fallback
    """
    result = {
        'success': False, 'file_path': None, 'current_price': None,
        'symbol': symbol, 'timeframe': timeframe, 'exchange': None,
        'error': None, 'currency': '$', 'caption': '',
    }
    try:
        # ── 1. ccxt 시도 ──────────────────────────────
        raw, exchange_name = _fetch_spot(symbol, timeframe, 100)
        df = None

        if raw:
            df = _to_dataframe(raw)
        else:
            # ── 2. yfinance fallback ──────────────────
            print(f"[AC FALLBACK] ccxt 전부 실패 → yfinance 시도: {symbol}")
            try:
                df, exchange_name = _fetch_crypto_yf(symbol, timeframe)
            except Exception as yf_err:
                print(f"[AC FALLBACK FAIL] yfinance: {yf_err}")
                tried = ', '.join(label for *_, label in _AC_ATTEMPTS)
                result['error'] = (
                    f"데이터를 가져올 수 없습니다: {symbol.split('/')[0]}\n"
                    f"시도: {tried}, yfinance"
                )
                return result

        result['exchange'] = exchange_name
        current_price = float(df['close'].iloc[-1])
        prev_price    = float(df['close'].iloc[-2]) if len(df) >= 2 else current_price
        result['current_price'] = current_price

        # ── 3. 52주 고/저 ─────────────────────────────
        high_52w = low_52w = None
        if exchange_name == 'yfinance':
            # 이미 1y 데이터 → 그대로 사용
            high_52w = float(df['high'].max())
            low_52w  = float(df['low'].min())
        else:
            raw_52w, _ = _fetch_spot(symbol, '1d', 365)
            if raw_52w:
                df_52w   = _to_dataframe(raw_52w)
                high_52w = float(df_52w['high'].max())
                low_52w  = float(df_52w['low'].min())
            else:
                try:
                    df_52w, _ = _fetch_crypto_yf(symbol, '1d')
                    high_52w  = float(df_52w['high'].max())
                    low_52w   = float(df_52w['low'].min())
                except Exception:
                    pass

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
    """Bybit perps → Binance futures 순서로 조회."""
    result = {
        'success': False, 'file_path': None, 'current_price': None,
        'symbol': symbol, 'timeframe': timeframe, 'exchange': None,
        'error': None, 'currency': '$', 'caption': '',
    }
    try:
        raw, exchange_name = _fetch_perps(symbol, timeframe, 100)
        if not raw:
            tried = ', '.join(label for *_, label in _AP_ATTEMPTS)
            result['error'] = (
                f"선물 데이터를 가져올 수 없습니다: {symbol.split('/')[0]}\n"
                f"시도한 거래소: {tried}"
            )
            return result

        result['exchange'] = exchange_name
        df = _to_dataframe(raw)
        current_price = float(df['close'].iloc[-1])
        prev_price    = float(df['close'].iloc[-2]) if len(df) >= 2 else current_price
        result['current_price'] = current_price

        # 52주 고/저
        raw_52w, _ = _fetch_perps(symbol, '1d', 365)
        high_52w = low_52w = None
        if raw_52w:
            df_52w   = _to_dataframe(raw_52w)
            high_52w = float(df_52w['high'].max())
            low_52w  = float(df_52w['low'].min())

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
        logger.error("create_perps_chart 오류: %s", e)
        result['error'] = str(e)
        plt.close('all')

    return result


def create_korean_stock_chart(query: str, timeframe: str = '1d') -> dict:
    """한국 주식 차트 (pykrx, 일봉만 지원)."""
    result = {
        'success': False, 'file_path': None, 'current_price': None,
        'symbol': query, 'timeframe': timeframe, 'exchange': 'KRX',
        'error': None, 'currency': '₩',
    }

    if timeframe != '1d':
        result['error'] = "한국 주식은 일봉(1d)만 지원합니다."
        return result

    try:
        df, ticker, name = _fetch_krx_data(query)
        result['current_price'] = float(df['close'].iloc[-1])
        result['symbol'] = f"{ticker} {name}"

        title = f"{ticker} {name} - 1D - KRX"
        tmp_path = _make_tmp_path()
        _draw_chart(df, title, timeframe, tmp_path)

        result['success'] = True
        result['file_path'] = tmp_path

    except Exception as e:
        logger.error("create_korean_stock_chart 오류: %s", e)
        result['error'] = str(e)
        plt.close('all')

    return result


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

    # 52주 고/저 — 캡션용 (차트에 선 없음)
    df_52w   = df.tail(252)
    high_52w = float(df_52w['high'].max())
    low_52w  = float(df_52w['low'].min())

    title = f"{name} ({ticker}) - 1D - KRX"
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


def create_us_stock_chart(ticker: str, timeframe: str = '1d') -> dict:
    """미국 주식 차트 (yfinance). 1d / 1h 지원."""
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
        print(f"[AU] {ticker} {timeframe}")
        df = _fetch_yf_ohlcv(ticker, timeframe)
        current_price = float(df['close'].iloc[-1])
        prev_price    = float(df['close'].iloc[-2]) if len(df) >= 2 else current_price
        result['current_price'] = current_price

        # 52주 고/저
        high_52w = low_52w = None
        if timeframe == '1d':
            # 1d 1y 데이터를 이미 가져왔으므로 그대로 사용
            high_52w = float(df['high'].max())
            low_52w  = float(df['low'].min())
        else:
            # 1h 조회 시 1d 데이터를 별도로 가져와 52주 계산
            try:
                df_1d    = _fetch_yf_ohlcv(ticker, '1d')
                high_52w = float(df_1d['high'].max())
                low_52w  = float(df_1d['low'].min())
            except Exception:
                pass

        title    = f"{ticker} - {timeframe.upper()} - US"
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

    except Exception as e:
        logger.error("create_us_stock_chart 오류: %s", e)
        result['error'] = str(e)
        plt.close('all')

    return result
