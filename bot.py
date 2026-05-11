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
import logging
import os
import traceback
from datetime import datetime, timedelta, timezone

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

from database import Database
from chart_utils import (
    create_clean_candlestick_chart,
    create_perps_chart,
    create_kr_stock_chart,
    create_us_stock_chart,
    find_kr_stock,
    normalize_symbol,
    format_price,
    VALID_INTERVALS,
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

MUTE_HOURS = 24

logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

KST = pytz.timezone('Asia/Seoul')

db = Database()


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
    두 가지 모드:
      [답글 모드]  광고 메시지에 답글로 /bw  → 해당 메시지 텍스트를 차단 문구로 등록 + 삭제 + mute
      [직접 모드]  /bw 광고문구              → "광고문구"를 차단 문구로 등록
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
        word = None

        # 1순위: 답글 대상 메시지의 텍스트
        if reply_msg:
            raw = (reply_msg.text or reply_msg.caption or '').strip()
            if raw:
                word = raw[:200]

        # 2순위: /bw 뒤 직접 입력한 인자
        if not word and context.args:
            word = ' '.join(context.args).strip()

        # 둘 다 없으면 사용법 안내
        if not word:
            await update.message.reply_text(
                "형식: /bw 광고문구 또는 광고 메시지에 답글로 /bw"
            )
            return

        if reply_msg:
            print(f"[BANWORD REPLY ADD] word='{word}'")
        else:
            print(f"[BANWORD ADD] word='{word}'")

        added = db.add_blocked_word(word)
        result_msg = (
            f"차단 문구가 추가되었습니다: {word}"
            if added else
            f"이미 등록된 문구입니다: {word}"
        )

        # 답글 방식: 원본 삭제 + mute
        if reply_msg and reply_msg.from_user and not reply_msg.from_user.is_bot:

            # 원본 메시지 삭제
            try:
                await reply_msg.delete()
                print(f"[SPAM MESSAGE DELETED] message_id={reply_msg.message_id}")
            except Exception as e:
                result_msg += f"\n원본 메시지 삭제 실패: {e}"

            # 원본 작성자 mute
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

            # 관리자의 /bw 명령어 메시지 삭제 시도
            try:
                await update.message.delete()
            except Exception:
                pass

            sent = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=result_msg,
            )
            await asyncio.sleep(3)
            try:
                await sent.delete()
            except Exception:
                pass
            return

        # 직접 입력 방식: 결과만 reply
        sent = await update.message.reply_text(result_msg)
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
# /ac 명령어 (캔들스틱 차트)
# ═══════════════════════════════════════════════════

async def cmd_ac(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /ac 티커           → 일봉(1d) 차트
    /ac 티커 인터벌    → 지정 인터벌 차트
    지원 인터벌: 1d, 1h, 4h, 15m, 5m
    """
    if not update.message:
        return

    if not context.args:
        await update.message.reply_text(
            "형식: /ac 티커 [인터벌]\n"
            "예시: /ac BTC  /  /ac ETH 1h  /  /ac BTC 4h\n"
            f"지원 인터벌: {', '.join(sorted(VALID_INTERVALS))}"
        )
        return

    ticker = context.args[0].upper()
    timeframe = context.args[1].lower() if len(context.args) >= 2 else '1d'

    if timeframe not in VALID_INTERVALS:
        await update.message.reply_text(
            f"지원하지 않는 인터벌입니다: {timeframe}\n"
            f"지원 인터벌: {', '.join(sorted(VALID_INTERVALS))}"
        )
        return

    symbol = normalize_symbol(ticker)
    processing_msg = await update.message.reply_text(f"차트 생성 중... {symbol} ({timeframe})")

    result = create_clean_candlestick_chart(symbol, timeframe)

    try:
        await processing_msg.delete()
    except Exception:
        pass

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

    # chart_utils가 caption을 만들어 줬으면 그대로 사용, 없으면 간단하게 생성
    caption = result.get('caption') or (
        f"📊 {result['symbol']}  {result['timeframe']}"
    )

    try:
        with open(result['file_path'], 'rb') as f:
            await update.message.reply_photo(photo=f, caption=caption)
    except Exception as e:
        logger.error("차트 전송 실패: %s", e)
        await update.message.reply_text(f"차트 전송 실패: {e}")
    finally:
        try:
            os.remove(result['file_path'])
        except Exception:
            pass


# ═══════════════════════════════════════════════════
# /ap 명령어 (코인 PERPS 차트)
# ═══════════════════════════════════════════════════

async def cmd_ap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /ap 티커           → 선물 일봉(1d) 차트
    /ap 티커 인터벌    → 지정 인터벌 선물 차트
    지원 인터벌: 1d, 1h, 4h, 15m, 5m
    """
    if not update.message:
        return

    if not context.args:
        await update.message.reply_text(
            "형식: /ap 티커 [인터벌]\n"
            "예시: /ap BTC  /  /ap ETH 1h  /  /ap BP 1h\n"
            f"지원 인터벌: {', '.join(sorted(VALID_INTERVALS))}"
        )
        return

    ticker = context.args[0].upper()
    timeframe = context.args[1].lower() if len(context.args) >= 2 else '1d'

    if timeframe not in VALID_INTERVALS:
        await update.message.reply_text(
            f"지원하지 않는 인터벌입니다: {timeframe}\n"
            f"지원 인터벌: {', '.join(sorted(VALID_INTERVALS))}"
        )
        return

    symbol = normalize_symbol(ticker)
    processing_msg = await update.message.reply_text(f"차트 생성 중... {symbol} PERPS ({timeframe})")

    result = create_perps_chart(symbol, timeframe)

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
    /ak 종목코드        → 한국 주식 일봉 차트
    /ak 종목명          → 종목명으로 검색 (하드코딩 매핑 우선)
    예: /ak 005930  /  /ak 삼성전자  /  /ak SK하이닉스
    """
    if not update.message:
        return

    if not context.args:
        await update.message.reply_text(
            "형식: /ak 종목코드 또는 종목명\n"
            "예시: /ak 005930  /  /ak 삼성전자  /  /ak SK하이닉스\n"
            "한국 주식은 일봉(1d)만 지원합니다."
        )
        return

    try:
        args = list(context.args)

        # 마지막 인자가 인터벌이면 분리
        if args[-1].lower() in VALID_INTERVALS:
            timeframe = args[-1].lower()
            query = ' '.join(args[:-1]).strip()
        else:
            timeframe = '1d'
            query = ' '.join(args).strip()

        if not query:
            await update.message.reply_text("형식: /ak 삼성전자 또는 /ak 005930")
            return

        print("[AK COMMAND]", query)

        processing_msg = await update.message.reply_text(f"검색 중... {query}")

        ticker, result = find_kr_stock(query)

        try:
            await processing_msg.delete()
        except Exception:
            pass

        # 완전 일치 → 차트 생성
        if ticker:
            name = result
            print("[AK MAP FOUND]", ticker, name)
            print("[AK FINAL]", ticker, name)
            chart_path, caption = create_kr_stock_chart(ticker, name, timeframe)
            try:
                with open(chart_path, 'rb') as f:
                    await update.message.reply_photo(photo=f, caption=caption)
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
            "예시: /au AAPL  /  /au TSLA 1h  /  /au NVDA\n"
            "지원 인터벌: 1d, 1h"
        )
        return

    ticker = context.args[0].upper()
    timeframe = context.args[1].lower() if len(context.args) >= 2 else '1d'

    processing_msg = await update.message.reply_text(f"차트 생성 중... {ticker} ({timeframe})")

    result = create_us_stock_chart(ticker, timeframe)

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
        "📈 차트\n"
        "/ac BTC → 코인 차트 (현물, 없으면 PERPS)\n"
        "/ac BTC 1h → 인터벌 지정\n"
        "/ap BTC → 코인 선물 차트\n"
        "/ak 삼성전자 → 한국 주식\n"
        "/ak 005930 → 종목코드 조회\n"
        "/au AAPL → 미국 주식\n"
        "\n"
        "🔧 디버그 (관리자)\n"
        "/nettest → 서버 네트워크 접근 테스트"
    )
    await update.message.reply_text(text)


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

    # InlineKeyboard 콜백 핸들러
    app.add_handler(
        CallbackQueryHandler(delete_banword_callback, pattern=r'^del_banword:\d+$')
    )

    # 일반 텍스트 메시지 핸들러 (명령어 제외)
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    logger.info("봇이 시작되었습니다. 종료하려면 Ctrl+C 를 누르세요.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
