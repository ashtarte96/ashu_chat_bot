"""
app.py
채팅 통계 웹 대시보드 (FastAPI)

실행 방법:
    uvicorn app:app --reload

브라우저 접속:
    http://127.0.0.1:8000
"""

import json
import os
import sqlite3
from datetime import date
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

app = FastAPI(title="채팅 통계 대시보드")
templates = Jinja2Templates(directory="templates")

DB_PATH = "messages.db"


# ═══════════════════════════════════════════════════
# DB 조회
# ═══════════════════════════════════════════════════

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def query_stats(start_date: str, end_date: str) -> dict:
    """start_date ~ end_date 기간의 통계를 딕셔너리로 반환한다."""
    conn = _connect()
    try:
        p = (start_date, end_date)

        # ── 요약 ──────────────────────────────────────────
        total: int = conn.execute(
            "SELECT COALESCE(SUM(count), 0) FROM messages WHERE date BETWEEN ? AND ?", p
        ).fetchone()[0]

        unique_users: int = conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM messages WHERE date BETWEEN ? AND ?", p
        ).fetchone()[0]

        active_days: int = conn.execute(
            "SELECT COUNT(DISTINCT date) FROM messages WHERE date BETWEEN ? AND ?", p
        ).fetchone()[0]

        top_row = conn.execute(
            """
            SELECT display_name, SUM(count) AS total
            FROM   messages
            WHERE  date BETWEEN ? AND ?
            GROUP  BY user_id
            ORDER  BY total DESC
            LIMIT  1
            """,
            p,
        ).fetchone()

        # ── 차트 원본 데이터 (Python 리스트) ──────────────
        daily_rows = conn.execute(
            """
            SELECT date, SUM(count) AS total
            FROM   messages
            WHERE  date BETWEEN ? AND ?
            GROUP  BY date
            ORDER  BY date
            """,
            p,
        ).fetchall()

        user_chart_rows = conn.execute(
            """
            SELECT display_name, SUM(count) AS total
            FROM   messages
            WHERE  date BETWEEN ? AND ?
            GROUP  BY user_id
            ORDER  BY total DESC
            LIMIT  20
            """,
            p,
        ).fetchall()

        # ── 표 데이터 ──────────────────────────────────────
        user_table = conn.execute(
            """
            SELECT display_name, SUM(count) AS total
            FROM   messages
            WHERE  date BETWEEN ? AND ?
            GROUP  BY user_id
            ORDER  BY total DESC
            """,
            p,
        ).fetchall()

        date_table = conn.execute(
            """
            SELECT date, SUM(count) AS total
            FROM   messages
            WHERE  date BETWEEN ? AND ?
            GROUP  BY date
            ORDER  BY date DESC
            """,
            p,
        ).fetchall()

        detail_table = conn.execute(
            """
            SELECT date, display_name, count
            FROM   messages
            WHERE  date BETWEEN ? AND ?
            ORDER  BY date DESC, count DESC
            """,
            p,
        ).fetchall()

        return {
            "has_data": total > 0,
            # 요약
            "total": total,
            "unique_users": unique_users,
            "avg_per_day": round(total / active_days, 1) if active_days else 0,
            "top_user": (
                "{} ({:,}개)".format(top_row["display_name"], top_row["total"])
                if top_row else "-"
            ),
            # 차트 (Python 리스트 — route에서 JSON 직렬화)
            "_daily_labels": [str(r["date"]) for r in daily_rows],
            "_daily_values": [int(r["total"]) for r in daily_rows],
            "_user_labels":  [str(r["display_name"]) for r in user_chart_rows],
            "_user_values":  [int(r["total"]) for r in user_chart_rows],
            # 표 (Python 튜플 리스트)
            "user_table":   [(str(r["display_name"]), int(r["total"])) for r in user_table],
            "date_table":   [(str(r["date"]), int(r["total"])) for r in date_table],
            "detail_table": [(str(r["date"]), str(r["display_name"]), int(r["count"])) for r in detail_table],
        }
    finally:
        conn.close()


# ═══════════════════════════════════════════════════
# 라우트
# ═══════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    today = date.today().isoformat()
    start_date = start_date or today
    end_date   = end_date   or today

    error: Optional[str] = None
    stats: Optional[dict] = None

    # 차트 데이터 — 데이터가 없을 때도 빈 배열 JSON을 기본값으로
    daily_labels: str = "[]"
    daily_counts: str = "[]"
    user_labels:  str = "[]"
    user_counts:  str = "[]"

    if not os.path.exists(DB_PATH):
        error = (
            "messages.db 파일이 없습니다. "
            "텔레그램 봇을 먼저 실행하여 데이터를 쌓아 주세요."
        )
    else:
        try:
            stats = query_stats(start_date, end_date)

            # 차트 데이터를 JSON 문자열로 직렬화 (ensure_ascii=False → 한글 유지)
            if stats["has_data"]:
                daily_labels = json.dumps(stats["_daily_labels"], ensure_ascii=False)
                daily_counts = json.dumps(stats["_daily_values"], ensure_ascii=False)
                user_labels  = json.dumps(stats["_user_labels"],  ensure_ascii=False)
                user_counts  = json.dumps(stats["_user_values"],  ensure_ascii=False)

        except Exception as exc:
            error = f"데이터 조회 중 오류가 발생했습니다: {exc}"

    # Starlette 0.28+ : 첫 번째 인자가 request, 두 번째가 template name
    # context 에는 "request" 키 불필요
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        context={
            "start_date":   start_date,
            "end_date":     end_date,
            "stats":        stats,
            "error":        error,
            # 차트용 JSON 문자열 (템플릿에서 | safe 로 사용)
            "daily_labels": daily_labels,
            "daily_counts": daily_counts,
            "user_labels":  user_labels,
            "user_counts":  user_counts,
        },
    )
