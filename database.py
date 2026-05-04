"""
database.py
SQLite 데이터베이스 관리 모듈.
메시지 카운트 저장/조회/초기화 기능을 담당한다.
"""

import sqlite3
import logging
from datetime import datetime, timezone
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


class Database:
    """메시지 카운트 + 차단 문구 전용 SQLite 래퍼 클래스"""

    def __init__(self, db_path: str = 'messages.db'):
        self.db_path = db_path
        self._init_db()

    # ──────────────────────────────────────────
    # 내부 헬퍼
    # ──────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        """
        테이블이 없으면 생성한다 (최초 실행 및 재실행 시 모두 안전하게 실행됨).

        messages 테이블:
          user_id, display_name, chat_id, date, count

        blocked_words 테이블:
          id, word (UNIQUE), created_at
        """
        with self._connect() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS messages (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id      INTEGER NOT NULL,
                    display_name TEXT    NOT NULL,
                    chat_id      INTEGER NOT NULL,
                    date         TEXT    NOT NULL,
                    count        INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(user_id, chat_id, date)
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS blocked_words (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    word       TEXT UNIQUE NOT NULL,
                    created_at TEXT NOT NULL
                )
            ''')
            conn.commit()
        logger.info("DB 초기화 완료: %s", self.db_path)

    # ──────────────────────────────────────────
    # 메시지 카운트 - 쓰기
    # ──────────────────────────────────────────

    def increment_count(
        self,
        user_id: int,
        display_name: str,
        chat_id: int,
        date: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                '''
                INSERT INTO messages (user_id, display_name, chat_id, date, count)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(user_id, chat_id, date)
                DO UPDATE SET
                    count        = count + 1,
                    display_name = excluded.display_name
                ''',
                (user_id, display_name, chat_id, date),
            )
            conn.commit()

    def reset_all(self, chat_id: int) -> None:
        with self._connect() as conn:
            conn.execute('DELETE FROM messages WHERE chat_id = ?', (chat_id,))
            conn.commit()
        logger.info("채팅방 %d 데이터 초기화 완료", chat_id)

    # ──────────────────────────────────────────
    # 메시지 카운트 - 읽기
    # ──────────────────────────────────────────

    def get_stats_by_date(self, chat_id: int, date: str) -> List[Tuple[str, int]]:
        with self._connect() as conn:
            cur = conn.execute(
                '''
                SELECT display_name, count
                FROM   messages
                WHERE  chat_id = ? AND date = ?
                ORDER  BY count DESC
                ''',
                (chat_id, date),
            )
            return cur.fetchall()

    def get_all_stats(self, chat_id: int) -> List[Tuple[str, str, int]]:
        with self._connect() as conn:
            cur = conn.execute(
                '''
                SELECT date, display_name, count
                FROM   messages
                WHERE  chat_id = ?
                ORDER  BY date DESC, count DESC
                ''',
                (chat_id,),
            )
            return cur.fetchall()

    def get_top_users(self, chat_id: int, limit: int = 20) -> List[Tuple[str, int]]:
        with self._connect() as conn:
            cur = conn.execute(
                '''
                SELECT
                    (
                        SELECT display_name
                        FROM   messages
                        WHERE  user_id = m.user_id AND chat_id = ?
                        ORDER  BY date DESC
                        LIMIT  1
                    ) AS display_name,
                    SUM(count) AS total
                FROM  messages m
                WHERE chat_id = ?
                GROUP BY user_id
                ORDER BY total DESC
                LIMIT ?
                ''',
                (chat_id, chat_id, limit),
            )
            return cur.fetchall()

    # ──────────────────────────────────────────
    # 차단 문구 관리
    # ──────────────────────────────────────────

    def add_blocked_word(self, word: str) -> bool:
        """
        :return: True=추가 성공, False=이미 존재 또는 빈 문자열
        word는 strip() + 200자 제한 처리 후 저장한다.
        """
        word = word.strip()[:200]
        if not word:
            return False
        created_at = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        try:
            with self._connect() as conn:
                conn.execute(
                    'INSERT INTO blocked_words (word, created_at) VALUES (?, ?)',
                    (word, created_at),
                )
                conn.commit()
            logger.info("차단 문구 추가: %s", word)
            return True
        except sqlite3.IntegrityError:
            return False

    def remove_blocked_word(self, word: str) -> bool:
        """
        문구(텍스트)로 삭제한다.
        :return: True=삭제 성공, False=존재하지 않음
        """
        with self._connect() as conn:
            cur = conn.execute(
                'DELETE FROM blocked_words WHERE word = ?',
                (word,),
            )
            conn.commit()
            deleted = cur.rowcount > 0
        if deleted:
            logger.info("차단 문구 삭제: %s", word)
        return deleted

    def remove_blocked_word_by_id(self, word_id: int) -> Optional[str]:
        """
        id로 삭제한다.
        :return: 삭제된 word 문자열, 존재하지 않으면 None
        """
        with self._connect() as conn:
            row = conn.execute(
                'SELECT word FROM blocked_words WHERE id = ?',
                (word_id,),
            ).fetchone()
            if not row:
                return None
            conn.execute('DELETE FROM blocked_words WHERE id = ?', (word_id,))
            conn.commit()
        logger.info("차단 문구 삭제 (id=%d): %s", word_id, row[0])
        return row[0]

    def get_blocked_words(self) -> List[str]:
        """
        :return: [word, ...] 등록 순서(id) 오름차순
        """
        with self._connect() as conn:
            cur = conn.execute(
                'SELECT word FROM blocked_words ORDER BY id'
            )
            return [row[0] for row in cur.fetchall()]

    def get_blocked_words_with_ids(self) -> List[Tuple[int, str]]:
        """
        InlineKeyboard 버튼용 — id 포함 목록 반환.
        :return: [(id, word), ...] 최신 등록순(id DESC)
        """
        with self._connect() as conn:
            cur = conn.execute(
                'SELECT id, word FROM blocked_words ORDER BY created_at DESC, id DESC'
            )
            return [(row[0], row[1]) for row in cur.fetchall()]
