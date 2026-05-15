"""
bot.py
텔레그램 그룹 채팅 메시지 카운트 + 스팸 차단 봇 (python-telegram-bot v21+)

기능:
  - 일반 텍스트 메시지(명령어·봇 제외)를 유저별·날짜별로 카운트
  - /stats          : 오늘(KST) 유저별 메시지 수
  - /stats YYYY-MM-DD : 지정 날짜 유저별 메시지 수
  - /stats_all      : 전체 날짜별 유저별 메시지 수
  - /top            : 전체 기간 누적 TOP 20 순위
  - /reset          : 이 채팅방 데이터 초기화 (관리자 전용)
  - /bw 문구        : 차단 문구 추가 (관리자 전용)
  - /bw             : 광고 메시지에 답글로 입력 → 해당 메시지 텍스트를 차단 문구로 등록 + 삭제 + mute
  - /banwords       : 차단 문구 목록 조회 + 버튼으로 삭제, 5초 후 자동 삭제 (관리자 전용)
  - /dw 문구        : 차단 문구 텍스트로 직접 삭제 (관리자 전용)
  - /mute [시간]    : 답글 대상 mute (기본 24h, 예: /mute 1h /mute 30m /mute 7d, 관리자 전용)
  - /unmute         : mute된 사용자의 메시지에 답글로 입력 → mute 해제 (관리자 전용)
  - /unmute user_id : user_id를 직접 입력해서 mute 해제 (관리자 전용)
  - /help           : 명령어 목록 출력
  - /ac 티커 [인터벌] : 코인 현물 차트 (기본 1d, fallback PERPS)
  - /ap 티커 [인터벌] : 코인 선물(PERPS) 차트 전용
  - /ak 종목 [1d]    : 한국 주식 차트 (일봉만 지원)
  - /au 티커 [인터벌] : 미국 주식 차트 (1d / 1h)
"""

import asyncio
import json
import logging
import os
import random
import time
import traceback
from datetime import datetime, timedelta, timezone

import requests

import pytz
from telegram import (
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import datetime as _dt

import news_utils
from database import Database
from chart_utils import (
    create_clean_candlestick_chart,
    create_perps_chart,
    create_kr_stock_chart,
    create_us_stock_chart,
    find_kr_stock,
    normalize_symbol,
    format_price,
    parse_timeframe,
    VALID_INTERVALS,
    fetch_upbit_ticker,
    fetch_bithumb_ticker,
)

# ═══════════════════════════════════════════════════
# 초기 설정
# ═══════════════════════════════════════════════════

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN 환경변수가 설정되지 않았습니다.\n"
        "실행 전에 다음 중 하나를 설정해주세요:\n"
        "  export TELEGRAM_BOT_TOKEN=your_token   (Linux/Mac)\n"
        "  set TELEGRAM_BOT_TOKEN=your_token      (Windows CMD)\n"
        "  $env:TELEGRAM_BOT_TOKEN='your_token'   (PowerShell)"
    )

MUTE_HOURS     = 24
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID", "")

logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

KST = pytz.timezone('Asia/Seoul')

db = Database()


# ═══════════════════════════════════════════════════
# 광고문구 관리
# ═══════════════════════════════════════════════════

_AD_STATE_FILE  = 'ad_state.json'
_SENT_MSGS_FILE = 'sent_messages.json'


class AdManager:
    """
    광고문구 버전 관리 + 전송된 메시지 추적.
    광고문구 변경 시 이전 메시지를 자동 수정/삭제한다.
    """

    def __init__(self):
        self._load()

    def _load(self):
        try:
            with open(_AD_STATE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.current_ad: str = data.get('current_ad', '')
            self.version: int    = data.get('version', 0)
        except (FileNotFoundError, json.JSONDecodeError):
            self.current_ad = ''
            self.version    = 0

        try:
            with open(_SENT_MSGS_FILE, 'r', encoding='utf-8') as f:
                self._sent: dict = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self._sent = {}

    def _save_state(self):
        try:
            with open(_AD_STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump({'current_ad': self.current_ad, 'version': self.version},
                          f, ensure_ascii=False)
        except Exception as e:
            logger.error("[AD] state save failed: %s", e)

    def _save_messages(self):
        try:
            with open(_SENT_MSGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self._sent, f, ensure_ascii=False)
        except Exception as e:
            logger.error("[AD] messages save failed: %s", e)

    def set_ad(self, text: str) -> int:
        self.current_ad = text.strip()
        self.version   += 1
        self._save_state()
        return self.version

    def clear_ad(self):
        self.current_ad = ''
        self.version   += 1
        self._save_state()

    def get_ad(self) -> str:
        return self.current_ad

    def build_caption(self, base: str) -> str:
        if not self.current_ad:
            return base
        return f"{base}\n\n{self.current_ad}"

    def record(self, chat_id: int, message_id: int, base_caption: str,
               msg_type: str = 'photo'):
        """광고문구가 붙은 메시지를 기록한다."""
        if not self.current_ad:
            return
        key = str(chat_id)
        if key not in self._sent:
            self._sent[key] = []
        self._sent[key].append({
            'message_id':   message_id,
            'ad_version':   self.version,
            'base_caption': base_caption,
            'msg_type':     msg_type,
            'ts':           int(time.time()),
        })
        self._sent[key] = self._sent[key][-200:]   # 최근 200개만 유지
        self._save_messages()

    def outdated_entries(self, chat_id: int) -> list:
        key = str(chat_id)
        return [e for e in self._sent.get(key, [])
                if e.get('ad_version') != self.version]

    def all_chat_ids(self) -> list:
        return [int(k) for k in self._sent]

    def remove_entries(self, chat_id: int, message_ids: list):
        key = str(chat_id)
        if key not in self._sent:
            return
        id_set = set(message_ids)
        self._sent[key] = [e for e in self._sent[key]
                           if e['message_id'] not in id_set]
        self._save_messages()


ad_manager = AdManager()

# ── 이전 광고문구 메시지 일괄 정리 ─────────────────────────────────────────

async def cleanup_old_ads(bot) -> None:
    """
    버전이 다른 광고문구가 붙은 메시지를 수정(edit) 또는 삭제(delete).
    Rate limit을 피하기 위해 메시지 사이 0.1초 대기.
    """
    for chat_id in ad_manager.all_chat_ids():
        outdated = ad_manager.outdated_entries(chat_id)
        if not outdated:
            continue
        edited = deleted = skipped = 0
        processed: list = []
        for entry in outdated:
            msg_id       = entry['message_id']
            base_caption = entry.get('base_caption', '')
            msg_type     = entry.get('msg_type', 'photo')
            new_caption  = ad_manager.build_caption(base_caption)
            try:
                if msg_type == 'photo':
                    await bot.edit_message_caption(
                        chat_id=chat_id, message_id=msg_id, caption=new_caption,
                    )
                else:
                    await bot.edit_message_text(
                        chat_id=chat_id, message_id=msg_id, text=new_caption,
                    )
                edited += 1
            except Exception:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=msg_id)
                    deleted += 1
                except Exception:
                    skipped += 1
            processed.append(msg_id)
            await asyncio.sleep(0.1)
        ad_manager.remove_entries(chat_id, processed)
        logger.info("[AD CLEANUP] chat=%d edited=%d deleted=%d skipped=%d",
                    chat_id, edited, deleted, skipped)


# ═══════════════════════════════════════════════════
# 김치프리미엄 (/kp)
# ═══════════════════════════════════════════════════

_KP_CACHE: dict = {'text': None, 'ts': 0.0}
_KP_TTL = 10   # 캐시 유효시간 (초)


def _fetch_usdkrw() -> float:
    """USD/KRW 환율 (exchangerate-api → open.er-api 순서로 시도)"""
    for url in (
        'https://api.exchangerate-api.com/v4/latest/USD',
        'https://open.er-api.com/v6/latest/USD',
    ):
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                d = r.json()
                rate = (d.get('rates') or d.get('conversion_rates') or {}).get('KRW')
                if rate:
                    return float(rate)
        except Exception:
            continue
    raise ValueError("USD/KRW 환율 조회 실패")


def _fetch_usdtkrw() -> float:
    """USDT/KRW (업비트 → 빗썸 fallback)"""
    try:
        r = requests.get(
            'https://api.upbit.com/v1/ticker',
            params={'markets': 'KRW-USDT'},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            if data and isinstance(data, list):
                return float(data[0]['trade_price'])
    except Exception:
        pass
    try:
        r = requests.get(
            'https://api.bithumb.com/public/ticker/USDT_KRW',
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            price = data.get('data', {}).get('closing_price')
            if price:
                return float(price)
    except Exception:
        pass
    raise ValueError("USDT/KRW 조회 실패")


_NAVER_INDEX_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept-Language': 'ko-KR,ko;q=0.9',
    'Referer': 'https://finance.naver.com/',
}


def _fetch_naver_index(code: str) -> tuple:
    """
    KOSPI 또는 KOSDAQ 현재 지수값 + 전일 대비 등락률(%) 반환.
    (value: float, chg_pct: float | None)

    전략:
      1. Naver sise_index_day 페이지 → pd.read_html (등락률 컬럼 직접 파싱)
      2. Naver mobile JSON API (m.stock.naver.com)
      3. yfinance fallback (^KS11 / ^KQ11)
    """
    import re as _re
    import pandas as _pd

    # ── 1. sise_index_day page (pd.read_html) ─────────────────────────
    try:
        url = f'https://finance.naver.com/sise/sise_index_day.naver?code={code}'
        r = requests.get(url, headers=_NAVER_INDEX_HEADERS, timeout=10)
        r.encoding = 'euc-kr'
        tables = _pd.read_html(r.text)
        for tbl in tables:
            str_cols = [str(c) for c in tbl.columns]
            val_idx  = next((i for i, c in enumerate(str_cols) if '체결가' in c or '종가' in c), None)
            chg_idx  = next((i for i, c in enumerate(str_cols) if '등락률' in c), None)
            if val_idx is None:
                continue
            df_valid = tbl.dropna(subset=[tbl.columns[val_idx]])
            if df_valid.empty:
                continue
            row     = df_valid.iloc[0]
            val_str = str(row.iloc[val_idx]).replace(',', '')
            val     = float(_re.sub(r'[^\d.]', '', val_str))
            if val <= 10:
                continue
            chg_pct = None
            if chg_idx is not None:
                chg_str = str(row.iloc[chg_idx])
                m = _re.search(r'([+\-]?\d+\.?\d*)', chg_str)
                if m:
                    chg_pct = float(m.group(1))
            logger.info("[NAVER %s] read_html: val=%.2f chg=%s", code, val, chg_pct)
            return val, chg_pct
    except Exception as e:
        logger.debug("[NAVER %s] read_html failed: %s", code, e)

    # ── 2. Naver mobile JSON API ───────────────────────────────────────
    try:
        r = requests.get(
            f'https://m.stock.naver.com/api/index/{code}/basic',
            headers={'User-Agent': 'Mozilla/5.0'},
            timeout=8,
        )
        if r.status_code == 200:
            d = r.json()
            val_raw   = str(d.get('closePrice') or d.get('currentValue') or '0').replace(',', '')
            val       = float(_re.sub(r'[^\d.]', '', val_raw) or '0')
            ratio_raw = str(d.get('fluctuationsRatio') or d.get('fluctuationRatio') or '0')
            ratio     = float(_re.sub(r'[^\d.]', '', ratio_raw) or '0')
            ftype     = str(d.get('fluctuationType') or '').upper()
            if 'FALL' in ftype or 'DOWN' in ftype:
                ratio = -abs(ratio)
            if val > 10:
                logger.info("[NAVER %s] JSON: val=%.2f chg=%.2f%%", code, val, ratio)
                return val, ratio
    except Exception as e:
        logger.debug("[NAVER %s] JSON failed: %s", code, e)

    # ── 3. yfinance fallback ───────────────────────────────────────────
    import yfinance as yf
    _map = {'KOSPI': '^KS11', 'KOSDAQ': '^KQ11'}
    fi   = yf.Ticker(_map.get(code, '^KS11')).fast_info
    val  = float(fi['last_price'])
    prev = float(fi.get('previous_close') or val)
    chg  = (val / prev - 1) * 100 if prev else 0.0
    logger.info("[NAVER %s] yfinance: val=%.2f chg=%.2f%%", code, val, chg)
    return val, chg


def _fetch_us_index(ticker: str, label: str) -> tuple:
    """
    미국 지수 (^IXIC 나스닥 / ^GSPC S&P500) 현재가 + 전일 대비 등락률(%) 반환.
    (value: float, chg_pct: float)
    """
    import yfinance as yf
    fi   = yf.Ticker(ticker).fast_info
    val  = float(fi['last_price'])
    prev = float(fi.get('previous_close') or val)
    chg  = (val / prev - 1) * 100 if prev else 0.0
    logger.info("[%s] %s: val=%.2f chg=%.2f%%", ticker, label, val, chg)
    return val, chg


def _fetch_btc_dominance() -> float:
    """BTC 도미넌스 (CoinGecko /api/v3/global)"""
    r = requests.get(
        'https://api.coingecko.com/api/v3/global',
        timeout=15,
    )
    r.raise_for_status()
    return float(r.json()['data']['market_cap_percentage']['btc'])


def _fetch_fear_greed() -> tuple:
    """공포탐욕지수 (alternative.me /fng/). (value_str, classification) 반환."""
    r = requests.get('https://api.alternative.me/fng/', timeout=10)
    r.raise_for_status()
    d = r.json()['data'][0]
    return d['value'], d['value_classification']


# ═══════════════════════════════════════════════════
# 신규 입장자 수학 인증
# ═══════════════════════════════════════════════════

_pending_verification: dict = {}
# user_id → {chat_id, answer, question, task, msg_id}

_FULL_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
)


def _make_captcha() -> tuple:
    """간단한 사칙연산 문제 생성. (question_str, answer) 반환."""
    a   = random.randint(1, 10)
    b   = random.randint(1, 10)
    op  = random.choice(['+', '-'])
    if op == '-' and b > a:
        a, b = b, a   # 음수 방지
    answer = (a + b) if op == '+' else (a - b)
    return f"{a} {op} {b}", answer


def _captcha_keyboard(user_id: int, answer: int) -> InlineKeyboardMarkup:
    """정답 1개 + 오답 3개로 구성된 2×2 버튼 그리드."""
    choices = {answer}
    attempts = 0
    while len(choices) < 4 and attempts < 30:
        decoy = answer + random.randint(-6, 6)
        if decoy != answer and decoy >= 0:
            choices.add(decoy)
        attempts += 1
    while len(choices) < 4:
        choices.add(max(choices) + 1)
    lst = list(choices)
    random.shuffle(lst)
    buttons = [
        InlineKeyboardButton(str(v), callback_data=f"captcha:{user_id}:{v}")
        for v in lst
    ]
    return InlineKeyboardMarkup([buttons[:2], buttons[2:]])


# ═══════════════════════════════════════════════════
# 유틸리티 함수
# ═══════════════════════════════════════════════════

def today_kst() -> str:
    return datetime.now(KST).strftime('%Y-%m-%d')


def make_display_name(user) -> str:
    parts = []
    if user.first_name:
        parts.append(user.first_name)
    if user.last_name:
        parts.append(user.last_name)
    name = ' '.join(parts).strip() or '(이름 없음)'
    if user.username:
        name = f"{name} (@{user.username})"
    return name


def send_in_chunks(lines: list[str], chunk_size: int = 4000) -> list[str]:
    chunks: list[str] = []
    current_lines: list[str] = []
    current_len = 0
    for line in lines:
        if current_len + len(line) + 1 > chunk_size and current_lines:
            chunks.append('\n'.join(current_lines))
            current_lines = [line]
            current_len = len(line)
        else:
            current_lines.append(line)
            current_len += len(line) + 1
    if current_lines:
        chunks.append('\n'.join(current_lines))
    return chunks


def build_banwords_message_and_keyboard(rows: list[tuple]) -> tuple[str, InlineKeyboardMarkup]:
    """
    [(id, word), ...] 를 받아 텍스트와 InlineKeyboardMarkup을 반환한다.
    버튼 레이블은 30자 초과 시 잘라서 '...' 붙임.
    callback_data 형식: "del_banword:{id}"
    """
    lines = ["차단 문구 목록:"]
    keyboard = []
    for i, (word_id, word) in enumerate(rows, start=1):
        lines.append(f"{i}. {word}")
        label = word if len(word) <= 30 else word[:30] + '...'
        keyboard.append([
            InlineKeyboardButton(f"삭제: {label}", callback_data=f"del_banword:{word_id}")
        ])
    return '\n'.join(lines), InlineKeyboardMarkup(keyboard)


async def check_is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    /banwords, /delword, /reset 등에서 사용하는 관리자 확인 헬퍼.
    DM이면 항상 True 반환. API 오류 시 False 반환.
    """
    chat = update.message.chat
    user = update.message.from_user

    if chat.type == 'private':
        return True

    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in ('administrator', 'creator')
    except Exception as exc:
        logger.error("관리자 권한 조회 실패: %s", exc)
        return False


async def send_temp(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    seconds: int = 3,
) -> None:
    """메시지를 전송하고 seconds초 뒤 자동 삭제. 사용자 명령 메시지도 함께 삭제."""
    sent = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text,
    )
    try:
        await update.message.delete()
    except Exception:
        pass
    await asyncio.sleep(seconds)
    try:
        await sent.delete()
    except Exception:
        pass


# ═══════════════════════════════════════════════════
# 일반 메시지 핸들러 (카운트 + 스팸 차단)
# ═══════════════════════════════════════════════════

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    일반 텍스트 메시지 처리 순서:
      1. 봇 메시지면 return
      2. 명령어면 return (filters로 이미 걸러지지만 이중 방어)
      3. 차단 문구 목록과 비교
         - 포함 시: 메시지 삭제 → mute → 알림 → return (카운트 안 함)
      4. 정상 메시지: 카운트 +1
    """
    msg = update.message
    if not msg:
        return

    user = msg.from_user
    if not user or user.is_bot:
        return

    text = msg.text or ''
    if text.startswith('/'):
        return

    print(f"[CHECK MESSAGE] chat={msg.chat_id} user={user.id} text={text[:80]}")

    # ── 차단 문구 검사 ─────────────────────────────
    try:
        blocked_words = db.get_blocked_words()
    except Exception as exc:
        logger.error("get_blocked_words 실패: %s", exc)
        blocked_words = []

    text_lower = text.lower()
    matched = next((w for w in blocked_words if w.lower() in text_lower), None)

    if matched:
        print(f"[BLOCKED] word='{matched}' user={user.id}")

        # 메시지 삭제
        try:
            await update.message.delete()
            print(f"[DELETE OK] message_id={msg.message_id}")
        except Exception as exc:
            print(f"[DELETE FAIL] {exc}")
            logger.warning("메시지 삭제 실패: %s", exc)

        # 사용자 mute (24시간)
        until_date = datetime.now(timezone.utc) + timedelta(hours=MUTE_HOURS)
        try:
            await context.bot.restrict_chat_member(
                chat_id=update.effective_chat.id,
                user_id=update.effective_user.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until_date,
            )
            muted = True
            print(f"[MUTED] user={user.id} until={until_date.isoformat()}")
        except Exception as exc:
            muted = False
            print(f"[MUTE FAIL] {exc}")
            logger.warning("mute 실패 (user=%d): %s", user.id, exc)

        # 처리 결과 알림
        user_display = make_display_name(user)
        if muted:
            notice = (
                f"스팸 메시지 삭제 및 사용자 mute 처리 완료\n"
                f"사용자: {user_display}\n"
                f"차단 문구: {matched}\n"
                f"{MUTE_HOURS}시간 후 자동 해제됩니다."
            )
        else:
            notice = (
                f"스팸 메시지 감지 (mute 실패 — 봇 관리자 권한을 확인해 주세요)\n"
                f"사용자: {user_display}\n"
                f"차단 문구: {matched}"
            )

        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=notice,
            )
        except Exception as exc:
            logger.warning("알림 메시지 전송 실패: %s", exc)

        return  # 스팸 메시지는 카운트하지 않음

    # ── 정상 메시지 카운트 ──────────────────────────
    db.increment_count(
        user_id=user.id,
        display_name=make_display_name(user),
        chat_id=msg.chat_id,
        date=today_kst(),
    )
    logger.info("카운트+1 | user_id=%d | chat_id=%d | date=%s", user.id, msg.chat_id, today_kst())


# ═══════════════════════════════════════════════════
# /bw 명령어 (관리자 전용)
# ═══════════════════════════════════════════════════

async def cmd_banword(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    세 가지 모드:
      [답글 모드]  메시지에 답글로 /bw         → 해당 메시지를 차단 문구로 등록 + 삭제 + mute
      [광고 설정]  /bw <문구>                  → 광고문구 설정 + 이전 메시지 자동 cleanup
      [광고 초기화] /bw clear                  → 광고문구 삭제 + 이전 메시지 cleanup
      [조회]       /bw (인자 없음, 답글 없음)  → 현재 광고문구 출력
    """
    if not update.message:
        return

    try:
        member = await context.bot.get_chat_member(
            update.effective_chat.id,
            update.effective_user.id,
        )
        if member.status not in ('administrator', 'creator'):
            await send_temp(update, context, "관리자만 사용할 수 있습니다.")
            return

        reply_msg = update.message.reply_to_message

        # ── 답글 모드: 스팸 메시지 blacklist + 삭제 + mute ────────────
        if reply_msg:
            raw = (reply_msg.text or reply_msg.caption or '').strip()
            word = raw[:200] if raw else None

            if not word:
                await update.message.reply_text("답글 메시지에 텍스트가 없습니다.")
                return

            print(f"[BANWORD REPLY ADD] word='{word}'")
            added = db.add_blocked_word(word)
            result_msg = (
                f"차단 문구가 추가되었습니다: {word}"
                if added else
                f"이미 등록된 문구입니다: {word}"
            )

            if reply_msg.from_user and not reply_msg.from_user.is_bot:
                try:
                    await reply_msg.delete()
                    print(f"[SPAM MESSAGE DELETED] message_id={reply_msg.message_id}")
                except Exception as e:
                    result_msg += f"\n원본 메시지 삭제 실패: {e}"

                try:
                    target_member = await context.bot.get_chat_member(
                        update.effective_chat.id,
                        reply_msg.from_user.id,
                    )
                    if target_member.status in ('administrator', 'creator'):
                        result_msg += "\n대상 사용자가 관리자라 mute할 수 없습니다."
                    else:
                        until_date = datetime.now(timezone.utc) + timedelta(hours=MUTE_HOURS)
                        await context.bot.restrict_chat_member(
                            chat_id=update.effective_chat.id,
                            user_id=reply_msg.from_user.id,
                            permissions=ChatPermissions(can_send_messages=False),
                            until_date=until_date,
                        )
                        print(f"[SPAM USER MUTED] user={reply_msg.from_user.id}")
                        result_msg += "\n원본 메시지 삭제 및 사용자 mute 완료"
                except Exception as e:
                    result_msg += f"\n사용자 mute 실패: {e}"

            try:
                await update.message.delete()
            except Exception:
                pass
            sent = await context.bot.send_message(chat_id=update.effective_chat.id,
                                                   text=result_msg)
            await asyncio.sleep(3)
            try:
                await sent.delete()
            except Exception:
                pass
            return

        # ── 직접 모드: 광고문구 관리 ──────────────────────────────────
        args_text = ' '.join(context.args).strip() if context.args else ''

        # /bw (인자 없음) → 현재 광고문구 출력
        if not args_text:
            cur = ad_manager.get_ad()
            if cur:
                await update.message.reply_text(
                    f"현재 광고문구 (v{ad_manager.version}):\n\n{cur}\n\n"
                    "/bw 새문구 → 변경   /bw clear → 삭제"
                )
            else:
                await update.message.reply_text(
                    "현재 설정된 광고문구가 없습니다.\n\n"
                    "/bw 문구 → 광고문구 설정"
                )
            return

        # /bw clear → 광고문구 삭제
        if args_text.lower() == 'clear':
            ad_manager.clear_ad()
            print(f"[BW UPDATE] 광고문구 삭제 → new_version={ad_manager.version}")
            logger.info("[BW UPDATE] 광고문구 삭제 new_version=%d", ad_manager.version)
            sent = await update.message.reply_text(
                f"✅ 광고문구가 삭제되었습니다. (v{ad_manager.version})\n"
                "이전 메시지 cleanup을 시작합니다..."
            )
            asyncio.create_task(cleanup_old_ads(context.bot))
            try:
                await update.message.delete()
            except Exception:
                pass
            await asyncio.sleep(3)
            try:
                await sent.delete()
            except Exception:
                pass
            return

        # /bw <문구> → 광고문구 설정
        version = ad_manager.set_ad(args_text)
        print(f"[BW UPDATE] 광고문구 변경 → new_version={version}")
        logger.info("[BW UPDATE] 광고문구 변경 new_version=%d text=%r", version, args_text)
        sent = await update.message.reply_text(
            f"✅ 광고문구가 설정되었습니다. (v{version})\n\n"
            f"{args_text}\n\n"
            "이전 메시지 cleanup을 시작합니다..."
        )
        asyncio.create_task(cleanup_old_ads(context.bot))
        try:
            await update.message.delete()
        except Exception:
            pass
        await asyncio.sleep(3)
        try:
            await sent.delete()
        except Exception:
            pass

    except Exception as e:
        logger.error("cmd_banword 오류: %s", e)
        try:
            await update.message.reply_text(f"오류 발생: {e}")
        except Exception:
            pass


# ═══════════════════════════════════════════════════
# /banwords 명령어 (관리자 전용)
# ═══════════════════════════════════════════════════

async def cmd_banwords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """차단 문구 목록을 InlineKeyboard 삭제 버튼과 함께 출력한다. 5초 후 자동 삭제."""
    if not update.message:
        return

    try:
        if not await check_is_admin(update, context):
            await send_temp(update, context, "관리자만 사용할 수 있습니다.")
            return

        rows = db.get_blocked_words_with_ids()

        if not rows:
            sent = await update.message.reply_text("등록된 차단 문구가 없습니다.")
            try:
                await update.message.delete()
            except Exception:
                pass
            await asyncio.sleep(5)
            try:
                await sent.delete()
            except Exception:
                pass
            return

        text, markup = build_banwords_message_and_keyboard(rows)
        sent = await update.message.reply_text(text, reply_markup=markup)

        try:
            await update.message.delete()
        except Exception:
            pass

        await asyncio.sleep(5)

        try:
            await sent.delete()
        except Exception:
            pass

    except Exception as exc:
        logger.error("cmd_banwords 오류: %s", exc)
        await update.message.reply_text(f"오류 발생: {exc}")


# ═══════════════════════════════════════════════════
# /banwords 삭제 버튼 콜백
# ═══════════════════════════════════════════════════

async def delete_banword_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    InlineKeyboard '삭제: ...' 버튼 콜백.
    callback_data 형식: "del_banword:{word_id}"
    """
    query = update.callback_query

    try:
        # 관리자 체크
        member = await context.bot.get_chat_member(
            update.effective_chat.id,
            update.effective_user.id,
        )
        if member.status not in ('administrator', 'creator'):
            await query.answer("관리자만 사용할 수 있습니다.", show_alert=True)
            return

        await query.answer()  # 로딩 스피너 해제

        word_id = int(query.data.split(':')[1])
        deleted_word = db.remove_blocked_word_by_id(word_id)

        if deleted_word is None:
            # 이미 다른 관리자가 삭제했거나 존재하지 않는 경우
            await query.answer("이미 삭제된 문구입니다.", show_alert=True)
            # 메시지에서 해당 버튼만 사라지도록 목록 갱신
            rows = db.get_blocked_words_with_ids()
            if rows:
                text, markup = build_banwords_message_and_keyboard(rows)
                await query.edit_message_text(text, reply_markup=markup)
            else:
                await query.edit_message_text("등록된 차단 문구가 없습니다.")
            return

        print(f"[BANWORD DELETED BY BUTTON] id={word_id} word='{deleted_word}'")

        # 삭제 성공 — 목록 갱신
        rows = db.get_blocked_words_with_ids()
        if rows:
            header = f"차단 문구가 삭제되었습니다: {deleted_word}\n\n"
            text, markup = build_banwords_message_and_keyboard(rows)
            await query.edit_message_text(header + text, reply_markup=markup)
        else:
            await query.edit_message_text(
                f"차단 문구가 삭제되었습니다: {deleted_word}\n\n등록된 차단 문구가 없습니다."
            )

    except Exception as e:
        logger.error("delete_banword_callback 오류: %s", e)
        try:
            await query.answer(f"오류: {e}", show_alert=True)
        except Exception:
            pass


# ═══════════════════════════════════════════════════
# /dw 명령어 (관리자 전용)
# ═══════════════════════════════════════════════════

async def cmd_delword(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    try:
        if not await check_is_admin(update, context):
            await send_temp(update, context, "관리자만 사용할 수 있습니다.")
            return

        if not context.args:
            await update.message.reply_text("형식: /dw 삭제할문구")
            return

        word = ' '.join(context.args).strip()
        if not word:
            await update.message.reply_text("형식: /dw 삭제할문구")
            return

        if db.remove_blocked_word(word):
            await update.message.reply_text(f"차단 문구가 삭제되었습니다: {word}")
        else:
            await update.message.reply_text(f"등록되지 않은 문구입니다: {word}")

    except Exception as exc:
        logger.error("cmd_delword 오류: %s", exc)
        await update.message.reply_text(f"오류 발생: {exc}")


# ═══════════════════════════════════════════════════
# /mute 명령어 (관리자 전용)
# ═══════════════════════════════════════════════════

def parse_mute_duration(text: str) -> timedelta | None:
    """
    '30m' '1h' '7d' → timedelta 반환.
    파싱 실패 시 None 반환.
    """
    text = text.strip().lower()
    if text.endswith('m'):
        try:
            return timedelta(minutes=int(text[:-1]))
        except ValueError:
            return None
    if text.endswith('h'):
        try:
            return timedelta(hours=int(text[:-1]))
        except ValueError:
            return None
    if text.endswith('d'):
        try:
            return timedelta(days=int(text[:-1]))
        except ValueError:
            return None
    return None


async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    답글로만 사용. 기본 24시간 mute.
      /mute       → 24시간
      /mute 1h    → 1시간
      /mute 30m   → 30분
      /mute 7d    → 7일
    """
    if not update.message:
        return

    try:
        member = await context.bot.get_chat_member(
            update.effective_chat.id,
            update.effective_user.id,
        )
        if member.status not in ('administrator', 'creator'):
            await send_temp(update, context, "관리자만 사용할 수 있습니다.")
            return

        reply_msg = update.message.reply_to_message
        if not reply_msg or not reply_msg.from_user:
            await update.message.reply_text(
                "mute할 사용자의 메시지에 답글로 /mute 또는 /mute 1h를 입력해주세요."
            )
            return

        target_user = reply_msg.from_user

        if target_user.is_bot:
            await update.message.reply_text("봇은 mute할 수 없습니다.")
            return

        target_member = await context.bot.get_chat_member(
            update.effective_chat.id,
            target_user.id,
        )
        if target_member.status in ('administrator', 'creator'):
            await update.message.reply_text("대상 사용자가 관리자라 mute할 수 없습니다.")
            return

        # 시간 파싱
        if context.args:
            duration = parse_mute_duration(context.args[0])
            if duration is None:
                await update.message.reply_text("시간 형식: /mute 30m, /mute 1h, /mute 7d")
                return
        else:
            duration = timedelta(hours=24)

        until_date = datetime.now(timezone.utc) + duration

        await context.bot.restrict_chat_member(
            chat_id=update.effective_chat.id,
            user_id=target_user.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until_date,
        )
        print(f"[MUTED] user={target_user.id} duration={duration}")

        # 시간 표시 문자열
        total_seconds = int(duration.total_seconds())
        if total_seconds < 3600:
            duration_str = f"{total_seconds // 60}분"
        elif total_seconds < 86400:
            h = total_seconds // 3600
            duration_str = f"{h}시간"
        else:
            d = total_seconds // 86400
            duration_str = f"{d}일"

        result_text = f"사용자를 mute 했습니다: {duration_str}"

        try:
            await update.message.delete()
        except Exception:
            pass

        sent = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=result_text,
        )
        await asyncio.sleep(3)
        try:
            await sent.delete()
        except Exception:
            pass

    except Exception as e:
        logger.error("cmd_mute 오류: %s", e)
        await update.message.reply_text(f"오류 발생: {e}")


# ═══════════════════════════════════════════════════
# /unmute 명령어 (관리자 전용)
# ═══════════════════════════════════════════════════

async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    두 가지 모드:
      [답글 모드]  mute된 사용자의 메시지에 답글로 /unmute  → 해당 사용자 mute 해제
      [직접 모드]  /unmute user_id                          → user_id 사용자 mute 해제
    """
    if not update.message:
        return

    try:
        # 관리자 체크 (직접 API 호출)
        member = await context.bot.get_chat_member(
            update.effective_chat.id,
            update.effective_user.id,
        )
        if member.status not in ('administrator', 'creator'):
            await send_temp(update, context, "관리자만 사용할 수 있습니다.")
            return

        reply_msg = update.message.reply_to_message
        target_user_id = None

        # 1순위: 답글 대상 사용자
        if reply_msg and reply_msg.from_user:
            target_user_id = reply_msg.from_user.id

        # 2순위: 직접 입력한 user_id
        elif context.args:
            try:
                target_user_id = int(context.args[0])
            except ValueError:
                await update.message.reply_text("형식: /unmute user_id (숫자만 입력)")
                return

        # 둘 다 없으면 사용법 안내
        if not target_user_id:
            await update.message.reply_text(
                "형식: /unmute user_id 또는 mute된 사용자의 메시지에 답글로 /unmute"
            )
            return

        # mute 해제 — 기본 권한 전체 복원
        await context.bot.restrict_chat_member(
            chat_id=update.effective_chat.id,
            user_id=target_user_id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_video_notes=True,
                can_send_voice_notes=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            ),
        )
        print(f"[UNMUTED] user={target_user_id}")

        if reply_msg:
            await update.message.reply_text("사용자 mute가 해제되었습니다.")
        else:
            await update.message.reply_text(f"사용자 mute가 해제되었습니다: {target_user_id}")

    except Exception as e:
        logger.error("cmd_unmute 오류: %s", e)
        await update.message.reply_text(f"오류 발생: {e}")


# ═══════════════════════════════════════════════════
# /stats 명령어
# ═══════════════════════════════════════════════════

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    if not await check_is_admin(update, context):
        await send_temp(update, context, "관리자만 사용할 수 있습니다.")
        return

    chat_id = update.message.chat_id

    if context.args:
        target = context.args[0]
        try:
            datetime.strptime(target, '%Y-%m-%d')
        except ValueError:
            await update.message.reply_text(
                "❌ 날짜 형식이 올바르지 않아요.\n예시: /stats 2026-04-28",
            )
            return
    else:
        target = today_kst()

    rows = db.get_stats_by_date(chat_id, target)

    if not rows:
        await update.message.reply_text(f"📭 {target} 날짜에 기록된 메시지가 없습니다.")
        return

    lines = [f"📊 {target} 메시지 통계\n"]
    grand_total = 0
    for rank, (name, count) in enumerate(rows, start=1):
        lines.append(f"{rank}. {name}: {count}개")
        grand_total += count
    lines.append(f"\n총 메시지: {grand_total}개")

    await update.message.reply_text('\n'.join(lines))


# ═══════════════════════════════════════════════════
# /stats_all 명령어
# ═══════════════════════════════════════════════════

async def cmd_stats_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    if not await check_is_admin(update, context):
        await send_temp(update, context, "관리자만 사용할 수 있습니다.")
        return

    chat_id = update.message.chat_id
    rows = db.get_all_stats(chat_id)

    if not rows:
        await update.message.reply_text("📭 기록된 메시지가 없습니다.")
        return

    lines: list[str] = ["📊 전체 날짜별 메시지 통계\n"]
    cur_date = None
    subtotal = 0

    for date, name, count in rows:
        if date != cur_date:
            if cur_date is not None:
                lines.append(f"  └ 소계: {subtotal}개\n")
            lines.append(f"📅 {date}")
            cur_date = date
            subtotal = 0
        lines.append(f"  • {name}: {count}개")
        subtotal += count

    if cur_date is not None:
        lines.append(f"  └ 소계: {subtotal}개")

    for chunk in send_in_chunks(lines):
        await update.message.reply_text(chunk)


# ═══════════════════════════════════════════════════
# /top 명령어
# ═══════════════════════════════════════════════════

async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    if not await check_is_admin(update, context):
        await send_temp(update, context, "관리자만 사용할 수 있습니다.")
        return

    chat_id = update.message.chat_id
    rows = db.get_top_users(chat_id, limit=20)

    if not rows:
        await update.message.reply_text("📭 기록된 메시지가 없습니다.")
        return

    MEDALS = {1: '🥇', 2: '🥈', 3: '🥉'}
    lines = ["🏆 전체 기간 메시지 TOP 순위\n"]
    for rank, (name, total) in enumerate(rows, start=1):
        badge = MEDALS.get(rank, f"{rank}.")
        lines.append(f"{badge} {name}: {total}개")

    await update.message.reply_text('\n'.join(lines))


# ═══════════════════════════════════════════════════
# /reset 명령어 (관리자 전용)
# ═══════════════════════════════════════════════════

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    if not await check_is_admin(update, context):
        await send_temp(update, context, "관리자만 사용할 수 있습니다.")
        return

    db.reset_all(update.message.chat_id)
    await update.message.reply_text("✅ 이 채팅방의 모든 메시지 통계가 초기화되었습니다.")


# ═══════════════════════════════════════════════════
# /ac 명령어 (코인 현물 차트)
# ═══════════════════════════════════════════════════

async def cmd_ac(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ac 티커 [인터벌] → 코인 현물 캔들 차트 (Binance→Bybit) + Upbit/Bithumb 가격"""
    if not update.message:
        return

    if not context.args:
        await update.message.reply_text(
            "형식: /ac 티커 [인터벌]\n"
            "예시: /ac BTC  /  /ac ETH 4h  /  /ac BTC 1w\n"
            "지원 인터벌: 1h / 4h / 12h / 1d / 1w / 1y  (기본: 1d)"
        )
        return

    args = list(context.args)
    ticker = args[0].upper()
    timeframe = '1d'

    if len(args) >= 2:
        tf = parse_timeframe(args[-1])
        if tf is None:
            await update.message.reply_text(
                f"지원하지 않는 인터벌: {args[-1]}\n"
                "지원 인터벌: 1h / 4h / 12h / 1d / 1w / 1y"
            )
            return
        timeframe = tf

    # 코인 심볼 추출 (BTCUSDT → BTC, BTC/USDT → BTC)
    coin = ticker.replace('USDT', '').replace('/', '').replace('USDT', '')

    processing_msg = await update.message.reply_text(f"차트 생성 중... {ticker} ({timeframe})")

    # 차트 + 업비트 + 빗썸 병렬 요청
    gathered = await asyncio.gather(
        asyncio.to_thread(create_clean_candlestick_chart, ticker, timeframe),
        asyncio.to_thread(fetch_upbit_ticker, coin),
        asyncio.to_thread(fetch_bithumb_ticker, coin),
        return_exceptions=True,
    )
    result        = gathered[0] if not isinstance(gathered[0], Exception) else {
        'success': False, 'error': str(gathered[0]), 'caption': '',
        'file_path': None, 'symbol': ticker, 'timeframe': timeframe,
    }
    upbit_price   = gathered[1] if isinstance(gathered[1], (int, float)) else None
    bithumb_price = gathered[2] if isinstance(gathered[2], (int, float)) else None

    try:
        await processing_msg.delete()
    except Exception:
        pass

    # 한국 거래소 가격을 caption에 추가
    if isinstance(result, dict) and result.get('success'):
        kr_lines = []
        if upbit_price is not None:
            kr_lines.append(f"🇰🇷 업비트: ₩{format_price(upbit_price)}")
        if bithumb_price is not None:
            kr_lines.append(f"🇰🇷 빗썸:   ₩{format_price(bithumb_price)}")
        if kr_lines:
            result['caption'] = (result.get('caption') or '') + '\n\n' + '\n'.join(kr_lines)

    await _send_chart_result(update, result)


# ═══════════════════════════════════════════════════
# 차트 공통 헬퍼
# ═══════════════════════════════════════════════════

async def _send_chart_result(
    update: Update,
    result: dict,
) -> None:
    """차트 result 딕셔너리를 텔레그램으로 전송하는 공통 헬퍼."""
    if not result['success']:
        await update.message.reply_text(result.get('error', '차트 생성에 실패했습니다.'))
        return

    base_caption = result.get('caption') or f"📊 {result['symbol']}  {result['timeframe']}"
    caption      = ad_manager.build_caption(base_caption)

    try:
        with open(result['file_path'], 'rb') as f:
            sent = await update.message.reply_photo(photo=f, caption=caption)
        ad_manager.record(
            chat_id=update.effective_chat.id,
            message_id=sent.message_id,
            base_caption=base_caption,
            msg_type='photo',
        )
    except Exception as e:
        logger.error("차트 전송 실패: %s", e)
        await update.message.reply_text(f"차트 전송 실패: {e}")
    finally:
        try:
            os.remove(result['file_path'])
        except Exception:
            pass


# ═══════════════════════════════════════════════════
# /ap 명령어 (코인 선물 차트)
# ═══════════════════════════════════════════════════

async def cmd_ap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ap 티커 [인터벌] → 코인 선물(PERPS) 캔들 차트 (Binance→Bybit)"""
    if not update.message:
        return

    if not context.args:
        await update.message.reply_text(
            "형식: /ap 티커 [인터벌]\n"
            "예시: /ap BTC  /  /ap ETH 4h  /  /ap BTC 1w\n"
            "지원 인터벌: 1h / 4h / 12h / 1d / 1w / 1y  (기본: 1d)"
        )
        return

    args = list(context.args)
    ticker = args[0].upper()
    timeframe = '1d'

    if len(args) >= 2:
        tf = parse_timeframe(args[-1])
        if tf is None:
            await update.message.reply_text(
                f"지원하지 않는 인터벌: {args[-1]}\n"
                "지원 인터벌: 1h / 4h / 12h / 1d / 1w / 1y"
            )
            return
        timeframe = tf

    processing_msg = await update.message.reply_text(f"선물 차트 생성 중... {ticker} ({timeframe})")

    result = await asyncio.to_thread(create_perps_chart, ticker, timeframe)

    try:
        await processing_msg.delete()
    except Exception:
        pass

    await _send_chart_result(update, result)


# ═══════════════════════════════════════════════════
# /ak 명령어 (한국 주식 차트)
# ═══════════════════════════════════════════════════

async def ak_chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /ak 종목명 [인터벌]  → 한국 주식 차트 (pykrx 기반)
    예: /ak 삼성전자  /  /ak 005930  /  /ak 한국전력 1w
    """
    if not update.message:
        return

    if not context.args:
        await update.message.reply_text(
            "형식: /ak 종목명 또는 종목코드 [인터벌]\n"
            "예시: /ak 삼성전자  /  /ak 005930  /  /ak 한국전력 1w\n"
            "지원 인터벌: 1d / 1w / 1y  (기본: 1d)\n"
            "※ 1h / 4h / 12h 입력 시 일봉으로 대체 표시"
        )
        return

    try:
        args = list(context.args)

        # 마지막 인자가 인터벌이면 분리
        if args[-1].lower() in VALID_INTERVALS:
            timeframe = parse_timeframe(args[-1]) or '1d'
            query = ' '.join(args[:-1]).strip()
        else:
            timeframe = '1d'
            query = ' '.join(args).strip()

        if not query:
            await update.message.reply_text("형식: /ak 삼성전자 또는 /ak 005930")
            return

        print("[AK COMMAND]", query, timeframe)

        processing_msg = await update.message.reply_text(f"검색 중... {query}")

        ticker, result = find_kr_stock(query)

        try:
            await processing_msg.delete()
        except Exception:
            pass

        # 완전 일치 → 차트 생성
        if ticker:
            name = result
            print(f"[AK] ticker={ticker} name={name} tf={timeframe}")
            chart_path, base_caption = create_kr_stock_chart(ticker, name, timeframe)
            caption = ad_manager.build_caption(base_caption)
            try:
                with open(chart_path, 'rb') as f:
                    sent = await update.message.reply_photo(photo=f, caption=caption)
                ad_manager.record(
                    chat_id=update.effective_chat.id,
                    message_id=sent.message_id,
                    base_caption=base_caption,
                    msg_type='photo',
                )
            finally:
                try:
                    os.remove(chart_path)
                except Exception:
                    pass
            return

        # 부분 일치 → 후보 목록 안내
        if isinstance(result, list) and result:
            lines = ["정확한 종목명을 입력해주세요:\n"]
            for i, (t, n) in enumerate(result, 1):
                lines.append(f"{i}. {n} ({t})")
            await update.message.reply_text('\n'.join(lines))
            return

        await update.message.reply_text(f"종목을 찾을 수 없습니다: {query}")

    except Exception as e:
        logger.error("[AK ERROR] %s", e)
        await update.message.reply_text(f"오류 발생: {e}")


# ═══════════════════════════════════════════════════
# /au 명령어 (미국 주식 차트)
# ═══════════════════════════════════════════════════

async def cmd_au(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /au 티커           → 미국 주식 일봉(1d) 차트
    /au 티커 인터벌    → 지정 인터벌 차트 (1d, 1h)
    예: /au AAPL  /  /au TSLA 1h
    """
    if not update.message:
        return

    if not context.args:
        await update.message.reply_text(
            "형식: /au 티커 [인터벌]\n"
            "예시: /au AAPL  /  /au TSLA 4h  /  /au NVDA 1w\n"
            "지원 인터벌: 1h / 4h / 12h / 1d / 1w / 1y  (기본: 1d)"
        )
        return

    ticker = context.args[0].upper()
    timeframe = '1d'

    if len(context.args) >= 2:
        tf = parse_timeframe(context.args[1])
        if tf is None:
            await update.message.reply_text(
                f"지원하지 않는 인터벌: {context.args[1]}\n"
                "지원 인터벌: 1h / 4h / 12h / 1d / 1w / 1y"
            )
            return
        timeframe = tf

    processing_msg = await update.message.reply_text(f"차트 생성 중... {ticker} ({timeframe})")

    result = await asyncio.to_thread(create_us_stock_chart, ticker, timeframe)

    try:
        await processing_msg.delete()
    except Exception:
        pass

    await _send_chart_result(update, result)


# ═══════════════════════════════════════════════════
# /help 명령어
# ═══════════════════════════════════════════════════

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    text = (
        "🤖 봇 명령어 안내\n"
        "\n"
        "📊 통계 (관리자)\n"
        "/stats → 오늘(KST) 유저별 메시지 수\n"
        "/stats YYYY-MM-DD → 특정 날짜 통계\n"
        "/stats_all → 전체 날짜별 통계\n"
        "/top → 전체 기간 TOP 유저\n"
        "\n"
        "🚫 스팸 차단 (관리자)\n"
        "/bw 문구 → 차단 문구 직접 추가\n"
        "/bw → 메시지에 답글 시 자동 차단 + 삭제 + mute\n"
        "/banwords → 차단 목록 (최신순 / 5초 후 삭제)\n"
        "/dw 문구 → 차단 문구 삭제\n"
        "/mute → 답글 대상 24시간 mute\n"
        "/mute 1h / 30m / 7d → 시간 지정 mute\n"
        "/unmute → 답글로 mute 해제\n"
        "/unmute user_id → 직접 해제\n"
        "\n"
        "📈 시세 / 차트\n"
        "/kp → 김치프리미엄 (USD/KRW · USDT/KRW)\n"
        "/ac BTC → 코인 현물 차트 (Binance/Bybit)\n"
        "/ac BTC 4h → 인터벌 지정\n"
        "/ap BTC → 코인 선물(PERPS) 차트 + 펀딩비\n"
        "/ap ETH 1w → 인터벌 지정\n"
        "/ak 삼성전자 → 한국 주식\n"
        "/ak 005930 1y → 종목코드 + 인터벌\n"
        "/au AAPL → 미국 주식\n"
        "/au TSLA 4h → 인터벌 지정\n"
        "지원 인터벌: 1h / 4h / 12h / 1d / 1w / 1y\n"
        "(한국 주식: 1h/4h/12h 요청 시 일봉으로 대체)\n"
        "\n"
        "📣 광고문구 (관리자)\n"
        "/bw 문구 → 광고문구 설정 (이전 메시지 자동 업데이트)\n"
        "/bw → 현재 광고문구 확인\n"
        "/bw clear → 광고문구 삭제\n"
        "\n"
        "🔒 신규 입장자 인증\n"
        "새 멤버 입장 시 수학 문제 자동 출제 (10초 제한)\n"
        "\n"
        "🔧 디버그 (관리자)\n"
        "/nettest → 서버 네트워크 접근 테스트"
    )
    await update.message.reply_text(text)


# ═══════════════════════════════════════════════════
# /kp 명령어 (김치프리미엄)
# ═══════════════════════════════════════════════════

async def cmd_kp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/kp → 김프 & 주요지수 (코스피/코스닥/나스닥/S&P500 등락률 포함)"""
    if not update.message:
        return

    now = time.time()
    if _KP_CACHE['text'] and now - _KP_CACHE['ts'] < _KP_TTL:
        await update.message.reply_text(_KP_CACHE['text'])
        return

    status = await update.message.reply_text("📊 지수 조회 중...")
    try:
        results = await asyncio.gather(
            asyncio.to_thread(_fetch_usdkrw),
            asyncio.to_thread(_fetch_usdtkrw),
            asyncio.to_thread(_fetch_naver_index, 'KOSPI'),
            asyncio.to_thread(_fetch_naver_index, 'KOSDAQ'),
            asyncio.to_thread(_fetch_us_index, '^IXIC', '나스닥'),
            asyncio.to_thread(_fetch_us_index, '^GSPC', 'S&P500'),
            asyncio.to_thread(_fetch_btc_dominance),
            asyncio.to_thread(_fetch_fear_greed),
            return_exceptions=True,
        )
        (usdkrw_r, usdtkrw_r,
         kospi_r, kosdaq_r,
         nasdaq_r, sp500_r,
         btc_dom_r, fg_r) = results

        def _unpack_float(r):
            return float(r) if not isinstance(r, Exception) else None

        def _unpack_pair(r):
            return r if not isinstance(r, Exception) else (None, None)

        usdkrw  = _unpack_float(usdkrw_r)
        usdtkrw = _unpack_float(usdtkrw_r)
        kp      = (usdtkrw / usdkrw - 1) * 100 if usdkrw and usdtkrw else None

        kospi_val,  kospi_chg  = _unpack_pair(kospi_r)
        kosdaq_val, kosdaq_chg = _unpack_pair(kosdaq_r)
        nasdaq_val, nasdaq_chg = _unpack_pair(nasdaq_r)
        sp500_val,  sp500_chg  = _unpack_pair(sp500_r)
        btc_dom                = _unpack_float(btc_dom_r)
        fear_value, fear_class = _unpack_pair(fg_r)

        def _fv(v, fmt):
            return format(v, fmt) if v is not None else 'N/A'

        def _idx(val, chg):
            if val is None:
                return 'N/A'
            s = f"{val:,.2f}"
            if chg is not None:
                s += f" ({'+' if chg >= 0 else ''}{chg:.2f}%)"
            return s

        btc_str  = f"{btc_dom:.2f}%" if btc_dom is not None else 'N/A'
        fear_str = f"{fear_value} ({fear_class})" if fear_value else 'N/A'

        message = (
            f"김프 & 주요지수\n\n"
            f"💵 USD/KRW: {_fv(usdkrw, ',.2f')}\n"
            f"🪙 USDT/KRW: {_fv(usdtkrw, ',.2f')}\n\n"
            f"🇰🇷 김프: {(f'{kp:+.2f}%') if kp is not None else 'N/A'}\n\n"
            f"🇰🇷 코스피: {_idx(kospi_val, kospi_chg)}\n"
            f"🇰🇷 코스닥: {_idx(kosdaq_val, kosdaq_chg)}\n"
            f"🇺🇸 나스닥: {_idx(nasdaq_val, nasdaq_chg)}\n"
            f"🇺🇸 S&P500: {_idx(sp500_val, sp500_chg)}\n\n"
            f"👑 BTC 도미넌스: {btc_str}\n"
            f"😱 공포탐욕지수: {fear_str}"
        )

        logger.info(
            "[KP] usdkrw=%s usdtkrw=%s kp=%s "
            "kospi=%s/%s kosdaq=%s/%s nasdaq=%s/%s sp500=%s/%s btc=%s fg=%s",
            usdkrw, usdtkrw,
            f"{kp:.2f}" if kp is not None else "N/A",
            kospi_val, kospi_chg, kosdaq_val, kosdaq_chg,
            nasdaq_val, nasdaq_chg, sp500_val, sp500_chg,
            btc_dom, fear_value,
        )
        _KP_CACHE['text'] = message
        _KP_CACHE['ts']   = now
        text = message

    except Exception as e:
        logger.error("[KP] error: %s", e)
        text = f"❌ 조회 실패: {e}"

    try:
        await status.delete()
    except Exception:
        pass
    await update.message.reply_text(text)


# ═══════════════════════════════════════════════════
# 신규 입장자 수학 인증
# ═══════════════════════════════════════════════════

async def on_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """새 멤버 입장 → 즉시 채팅 제한 + 수학 인증 버튼 전송."""
    msg = update.message
    if not msg or not msg.new_chat_members:
        return

    for member in msg.new_chat_members:
        if member.is_bot:
            continue
        user_id = member.id
        chat_id = msg.chat_id

        # 관리자 제외
        try:
            m = await context.bot.get_chat_member(chat_id, user_id)
            if m.status in ('administrator', 'creator'):
                continue
        except Exception:
            pass

        # 즉시 채팅 제한
        try:
            await context.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=ChatPermissions(can_send_messages=False),
            )
        except Exception as e:
            logger.warning("[VERIFY] restrict failed user=%d: %s", user_id, e)

        # 기존 인증 대기 중이면 취소
        if user_id in _pending_verification:
            old = _pending_verification.pop(user_id)
            if 'task' in old:
                old['task'].cancel()
            try:
                await context.bot.delete_message(old['chat_id'], old['msg_id'])
            except Exception:
                pass

        # 문제 생성 + 메시지 전송
        question, answer = _make_captcha()
        keyboard  = _captcha_keyboard(user_id, answer)
        name_str  = member.first_name or str(user_id)

        sent = await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🤖 [{name_str}] 님 환영합니다!\n\n"
                f"🧮 인증 문제: {question} = ?\n\n"
                "⏱ 10초 안에 정답 버튼을 눌러주세요.\n"
                "시간 초과 시 자동 퇴장 처리됩니다."
            ),
            reply_markup=keyboard,
        )

        task = asyncio.create_task(
            _verification_timeout(context.bot, chat_id, user_id, sent.message_id)
        )
        _pending_verification[user_id] = {
            'chat_id':  chat_id,
            'answer':   answer,
            'question': question,
            'task':     task,
            'msg_id':   sent.message_id,
        }
        logger.info("[VERIFY] user=%d question=%s", user_id, question)


async def _verification_timeout(bot, chat_id: int, user_id: int, msg_id: int) -> None:
    """10초 후 인증 미완료 시 퇴장 처리."""
    await asyncio.sleep(10)
    if user_id not in _pending_verification:
        return   # 이미 인증 완료

    del _pending_verification[user_id]
    logger.info("[VERIFY] user=%d result=timeout_kick", user_id)

    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        pass

    try:
        await bot.ban_chat_member(chat_id, user_id)
        await asyncio.sleep(0.5)
        await bot.unban_chat_member(chat_id, user_id)   # 재입장 허용
    except Exception as e:
        logger.warning("[VERIFY] kick failed user=%d: %s", user_id, e)
        return

    try:
        notice = await bot.send_message(
            chat_id,
            "⏰ 인증 시간 초과로 퇴장 처리되었습니다. 재입장 후 다시 시도하세요.",
        )
        await asyncio.sleep(5)
        await notice.delete()
    except Exception:
        pass


async def handle_captcha_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    수학 인증 버튼 클릭 처리.
    callback_data 형식: captcha:{user_id}:{clicked_value}
    """
    query = update.callback_query
    if not query:
        return
    await query.answer()

    try:
        _, uid_str, val_str = query.data.split(':')
        target_uid = int(uid_str)
        clicked    = int(val_str)
    except (ValueError, AttributeError):
        return

    # 클릭한 사람이 인증 대상인지 확인
    if query.from_user.id != target_uid:
        await query.answer("이 인증은 다른 사용자의 것입니다.", show_alert=True)
        return

    entry = _pending_verification.get(target_uid)
    if not entry:
        try:
            await query.edit_message_text("이미 처리된 인증입니다.")
        except Exception:
            pass
        return

    if clicked == entry['answer']:
        # 정답 → 제한 해제 + 환영
        entry['task'].cancel()
        del _pending_verification[target_uid]
        try:
            await context.bot.restrict_chat_member(
                chat_id=entry['chat_id'],
                user_id=target_uid,
                permissions=_FULL_PERMISSIONS,
            )
        except Exception as e:
            logger.error("[VERIFY] restore permissions failed user=%d: %s", target_uid, e)

        name_str = query.from_user.first_name or str(target_uid)
        try:
            await query.edit_message_text(f"✅ {name_str} 님, 인증 완료! 환영합니다 🎉")
        except Exception:
            pass
        logger.info("[VERIFY] user=%d result=success", target_uid)

    else:
        # 오답 → 새 문제로 교체 (타이머 유지)
        question, answer = _make_captcha()
        keyboard = _captcha_keyboard(target_uid, answer)
        entry['answer']   = answer
        entry['question'] = question
        try:
            await query.edit_message_text(
                f"❌ 틀렸습니다! 다시 시도하세요.\n\n"
                f"🧮 새 문제: {question} = ?\n\n"
                "⏱ 남은 시간 내에 정답 버튼을 눌러주세요.",
                reply_markup=keyboard,
            )
        except Exception:
            pass
        logger.info("[VERIFY] user=%d wrong clicked=%d expected=%d",
                    target_uid, clicked, entry['answer'])


# ═══════════════════════════════════════════════════
# /nettest — 네트워크 접근 디버그 (관리자 전용)
# ═══════════════════════════════════════════════════

async def cmd_nettest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    if not await check_is_admin(update, context):
        await send_temp(update, context, "관리자만 사용할 수 있습니다.")
        return

    status_msg = await update.message.reply_text("🔍 네트워크 테스트 중...")

    def _run_tests() -> str:
        import requests as req

        lines = ["🌐 네트워크 테스트\n"]

        # 1. Bybit
        try:
            r = req.get("https://api.bybit.com/v5/market/time", timeout=10)
            lines.append(f"✅ Bybit: {r.status_code}")
        except Exception as e:
            print("[NETTEST ERROR]", traceback.format_exc())
            lines.append(f"❌ Bybit: {e}")

        # 2. Binance
        try:
            r = req.get("https://api.binance.com/api/v3/time", timeout=10)
            lines.append(f"✅ Binance: {r.status_code}")
        except Exception as e:
            print("[NETTEST ERROR]", traceback.format_exc())
            lines.append(f"❌ Binance: {e}")

        lines.append("")

        # 3. 서버 IP
        try:
            r = req.get("https://httpbin.org/ip", timeout=10)
            lines.append("🌍 Server IP:")
            lines.append(r.text.strip()[:80])
        except Exception as e:
            print("[NETTEST ERROR]", traceback.format_exc())
            lines.append(f"❌ httpbin: {e}")

        return "\n".join(lines)

    try:
        report = await asyncio.to_thread(_run_tests)
    except Exception:
        print("[NETTEST ERROR]", traceback.format_exc())
        report = "네트워크 테스트 실패\n" + traceback.format_exc()

    try:
        await status_msg.delete()
    except Exception:
        pass

    await update.message.reply_text(report)


# ═══════════════════════════════════════════════════
# 뉴스 브리핑
# ═══════════════════════════════════════════════════

def _split_message(text: str, max_len: int = 4000) -> list:
    """텔레그램 4096자 제한에 맞게 메시지 분할."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind('\n\n', 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()
    return chunks


async def news_briefing_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """스케줄러가 호출하는 자동 뉴스 브리핑 발송 함수."""
    data    = context.job.data or {}
    chat_id = data.get('chat_id')
    period  = data.get('period', 'morning')
    hours   = 12 if period == 'morning' else 9
    print(f"[NEWS SEND] {period} briefing → chat_id={chat_id} hours={hours}")
    try:
        text = await asyncio.to_thread(
            news_utils.get_briefing, hours, period, None, True
        )
        for chunk in _split_message(text):
            await context.bot.send_message(
                chat_id=chat_id,
                text=chunk,
                disable_web_page_preview=True,
            )
        print(f"[NEWS SEND] {period} briefing sent OK")
    except Exception:
        print(f"[NEWS SEND] Error sending {period} briefing:")
        import traceback
        traceback.print_exc()


async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/news [btc|eth|sol|...] → 최신 크립토 뉴스 즉시 출력 (테스트 모드)."""
    print("[CMD_NEWS REAL HANDLER]")
    if not update.message:
        return

    query_filter: str | None = context.args[0].lower() if context.args else None
    label = f" ({query_filter.upper()})" if query_filter else ""

    print(f"[NEWS TEST] /news{label} requested by user={update.effective_user.id}")

    processing_msg = await update.message.reply_text(f"📰 뉴스 수집 중{label}...")

    try:
        text = await asyncio.to_thread(
            news_utils.get_briefing,
            24,           # 최근 24시간
            'test',       # 테스트 헤더
            query_filter,
            False,        # 캐시 무시 (즉시 최신 결과)
            5,            # 기본 5개
        )
    except Exception as e:
        logger.error("[NEWS TEST] error: %s", e)
        text = "뉴스 조회 실패: " + str(e)

    try:
        await processing_msg.delete()
    except Exception:
        pass

    chat_id = update.effective_chat.id
    is_empty = "없었슈" in text
    print(f"[TELEGRAM NEWS SEND] len={len(text)} is_empty_msg={is_empty} chat_id={chat_id}")
    print(text[:1000])
    for chunk in _split_message(text):
        await update.message.reply_text(chunk, disable_web_page_preview=True)
    print(f"[NEWS SEND] /news{label} sent OK")


# ═══════════════════════════════════════════════════
# 봇 시작
# ═══════════════════════════════════════════════════

def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # CommandHandler 를 MessageHandler 보다 먼저 등록
    app.add_handler(CommandHandler('stats',     cmd_stats))
    app.add_handler(CommandHandler('stats_all', cmd_stats_all))
    app.add_handler(CommandHandler('top',       cmd_top))
    app.add_handler(CommandHandler('reset',     cmd_reset))
    app.add_handler(CommandHandler('bw',        cmd_banword))
    app.add_handler(CommandHandler('banwords',  cmd_banwords))
    app.add_handler(CommandHandler('dw',        cmd_delword))
    app.add_handler(CommandHandler('mute',      cmd_mute))
    app.add_handler(CommandHandler('unmute',    cmd_unmute))
    app.add_handler(CommandHandler('help',      cmd_help))
    app.add_handler(CommandHandler('nettest',   cmd_nettest))
    app.add_handler(CommandHandler('ac',        cmd_ac))
    app.add_handler(CommandHandler('ap',        cmd_ap))
    app.add_handler(CommandHandler('ak',        ak_chart))
    app.add_handler(CommandHandler('au',        cmd_au))
    app.add_handler(CommandHandler('kp',        cmd_kp))
    app.add_handler(CommandHandler('news',      cmd_news))

    # 뉴스 자동 발송 스케줄러 (TARGET_CHAT_ID 환경변수 필요)
    if TARGET_CHAT_ID and app.job_queue:
        try:
            chat_id_int = int(TARGET_CHAT_ID)
            app.job_queue.run_daily(
                news_briefing_job,
                time=_dt.time(8, 0, 0, tzinfo=KST),
                data={'chat_id': chat_id_int, 'period': 'morning'},
                name='morning_news',
            )
            app.job_queue.run_daily(
                news_briefing_job,
                time=_dt.time(17, 0, 0, tzinfo=KST),
                data={'chat_id': chat_id_int, 'period': 'evening'},
                name='evening_news',
            )
            logger.info(
                "[NEWS SEND] Scheduled 08:00 & 17:00 KST → chat_id=%s", TARGET_CHAT_ID
            )
        except ValueError:
            logger.warning("[NEWS SEND] TARGET_CHAT_ID is not a valid integer: %s", TARGET_CHAT_ID)
    elif TARGET_CHAT_ID:
        logger.warning(
            "[NEWS SEND] TARGET_CHAT_ID set but job_queue unavailable. "
            "Install: python-telegram-bot[job-queue]"
        )

    # InlineKeyboard 콜백 핸들러
    app.add_handler(
        CallbackQueryHandler(delete_banword_callback, pattern=r'^del_banword:\d+$')
    )
    app.add_handler(
        CallbackQueryHandler(handle_captcha_callback, pattern=r'^captcha:\d+:\d+$')
    )

    # 신규 입장자 인증 핸들러
    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_member)
    )

    # 일반 텍스트 메시지 핸들러 (명령어 제외)
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    logger.info("봇이 시작되었습니다. 종료하려면 Ctrl+C 를 누르세요.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
