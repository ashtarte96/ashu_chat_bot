# Render 배포 가이드

Render에서 텔레그램 봇과 대시보드를 함께 24시간 실행하는 방법입니다.

---

## 구조 설명

```
start.sh 실행
 ├── python bot.py          ← 백그라운드 실행 (텔레그램 봇)
 └── uvicorn app:app :10000 ← 포그라운드 실행 (FastAPI 대시보드)
```

Render는 포트 10000으로 들어오는 HTTP 요청을 받아 대시보드를 서빙합니다.
봇은 백그라운드에서 텔레그램 서버와 폴링 방식으로 통신합니다.

---

## 1단계: Render 가입

1. [render.com](https://render.com) 접속
2. `Get Started for Free` 클릭
3. **GitHub 계정으로 로그인** (권장)

---

## 2단계: New Web Service 생성

1. 대시보드에서 `New +` → `Web Service` 클릭
2. `Build and deploy from a Git repository` 선택
3. `Next` 클릭

---

## 3단계: GitHub 저장소 연결

1. `Connect a repository` 화면에서 GitHub 계정 연결
2. 봇 저장소(`my-telegram-bot` 등)를 검색 후 `Connect` 클릭

---

## 4단계: 서비스 설정

아래 값을 입력합니다:

| 항목 | 입력값 |
|------|--------|
| **Name** | `telegram-bot` (원하는 이름) |
| **Region** | `Singapore` 또는 `Oregon` |
| **Branch** | `main` |
| **Runtime** | `Python 3` |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `bash start.sh` |
| **Instance Type** | `Free` |

---

## 5단계: 환경변수 설정

서비스 설정 화면 하단 `Environment Variables` 섹션:

| Key | Value |
|-----|-------|
| `TELEGRAM_BOT_TOKEN` | BotFather에서 발급받은 토큰 |

`Add Environment Variable` 클릭 후 입력하고 저장합니다.

---

## 6단계: 배포 시작

`Create Web Service` 클릭

Render가 자동으로:
1. GitHub에서 코드를 내려받고
2. `pip install -r requirements.txt` 실행
3. `bash start.sh` 로 봇 + 대시보드 시작

배포 로그는 화면 하단 `Logs` 탭에서 실시간 확인 가능합니다.

---

## 7단계: 배포 확인

### 봇 확인
텔레그램에서 봇에게 `/help` 명령어를 보내 응답이 오는지 확인합니다.

### 대시보드 확인
Render가 제공한 URL로 접속합니다:
```
https://telegram-bot-xxxx.onrender.com
```
(서비스 상단 `Your service is live` 옆 URL)

---

## 코드 업데이트 후 재배포

로컬에서 코드를 수정 후:

```powershell
git add .
git commit -m "update"
git push
```

GitHub에 push하면 Render가 **자동으로 재배포**합니다.
수동 재배포가 필요하면 Render 대시보드 → `Manual Deploy` → `Deploy latest commit` 클릭.

---

## 로그 확인

Render 대시보드 → 서비스 선택 → `Logs` 탭

실시간으로 봇 로그와 대시보드 로그를 모두 확인할 수 있습니다.

---

## 주의사항

### Render 무료 플랜 Sleep 문제

| 항목 | 내용 |
|------|------|
| 무료 플랜 동작 | HTTP 요청이 없으면 **15분 후 서버가 sleep** |
| sleep 시 봇 | 텔레그램 메시지를 받지 못함 |
| sleep 시 대시보드 | 첫 요청 시 **30~60초 지연** 후 재시작 |

### Sleep 방지 방법 (무료 플랜)

외부 ping 서비스로 15분마다 대시보드 URL을 호출하면 sleep을 방지할 수 있습니다.

**UptimeRobot 사용 (무료):**
1. [uptimerobot.com](https://uptimerobot.com) 가입
2. `Add New Monitor` → `HTTP(s)` 선택
3. URL: `https://telegram-bot-xxxx.onrender.com`
4. 모니터링 간격: `5 minutes`

### 근본 해결: 유료 플랜

Render `Starter` 플랜 ($7/월)을 사용하면 sleep 없이 24시간 안정적으로 실행됩니다.

---

## Render 설정 화면 입력값 요약

```
Build Command : pip install -r requirements.txt
Start Command : bash start.sh

Environment Variables:
  TELEGRAM_BOT_TOKEN = [BotFather 토큰]
```

---

## 보안 주의사항

- 토큰은 Render `Environment Variables`에만 입력하고 코드에 직접 쓰지 않습니다
- `messages.db`는 `.gitignore`에 포함되어 GitHub에 올라가지 않습니다
- Render에서 제공하는 URL은 HTTPS이므로 대시보드 접속은 기본적으로 암호화됩니다
- 단, 대시보드에 인증이 없으므로 URL을 외부에 공유하지 마세요
