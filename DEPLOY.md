# 텔레그램 봇 배포 가이드

Oracle Cloud Ubuntu 서버에서 24시간 봇을 실행하는 방법을 단계별로 설명합니다.

---

## 1단계: GitHub 업로드 전 보안 확인

코드에 토큰이 남아 있으면 GitHub에 올리는 순간 외부에 노출됩니다.
업로드 전에 반드시 아래 명령어로 확인하세요.

**Windows PowerShell:**
```powershell
findstr /S /I "TOKEN AAF AAH" *.py *.txt *.md
```

**출력이 아래처럼 환경변수 로딩 코드만 나와야 합니다:**
```
bot.py:TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
```

실제 토큰 문자열(`AAF...` 또는 `AAH...`로 시작하는 긴 문자열)이 보이면
GitHub 업로드 전에 반드시 제거하고, BotFather에서 토큰을 재발급 받으세요.

**BotFather 토큰 재발급:**
1. 텔레그램에서 [@BotFather](https://t.me/BotFather) 에 접속
2. `/mybots` 입력 → 봇 선택
3. `API Token` → `Revoke current token`
4. 새 토큰 복사해서 보관

---

## 2단계: .gitignore 확인

프로젝트 폴더에 `.gitignore` 파일이 있는지 확인합니다.

**PowerShell:**
```powershell
Get-Content .gitignore
```

아래 항목이 포함되어 있어야 합니다:
```
.env
config.py
*.db
*.sqlite
*.sqlite3
__pycache__/
*.pyc
*.png
venv/
```

---

## 3단계: GitHub Private 저장소 생성

1. [github.com](https://github.com) 로그인
2. 우측 상단 `+` → `New repository`
3. Repository name 입력 (예: `my-telegram-bot`)
4. **Private** 선택 (중요)
5. `Create repository` 클릭
6. 생성된 저장소 URL 복사 (예: `https://github.com/계정명/my-telegram-bot.git`)

---

## 4단계: 로컬에서 git 초기화 및 push

PowerShell에서 프로젝트 폴더로 이동 후 실행:

```powershell
git init
git add .
git commit -m "initial deploy"
git branch -M main
git remote add origin https://github.com/계정명/my-telegram-bot.git
git push -u origin main
```

push 후 GitHub 저장소 페이지에서 `bot.py`를 열어
실제 토큰 문자열이 없는지 한 번 더 확인하세요.

---

## 5단계: Oracle Cloud Ubuntu 서버 생성

1. [cloud.oracle.com](https://cloud.oracle.com) 로그인
2. `Compute` → `Instances` → `Create Instance`
3. 이미지: **Ubuntu 22.04**
4. Shape: **VM.Standard.E2.1.Micro** (무료 티어)
5. SSH 키: `Generate a key pair` → **Private Key 다운로드** (분실 시 재접속 불가)
6. `Create` 클릭
7. Instance 생성 완료 후 Public IP 주소 확인

---

## 6단계: SSH 접속

**Windows PowerShell:**
```powershell
ssh -i "다운로드한키파일.key" ubuntu@서버IP주소
```

처음 접속 시 `Are you sure you want to continue connecting?` → `yes` 입력

---

## 7단계: 서버 기본 패키지 설치

서버에 접속한 후 실행:

```bash
sudo apt update
sudo apt install python3 python3-pip python3-venv git -y
```

---

## 8단계: GitHub에서 코드 clone

```bash
cd /home/ubuntu
git clone https://github.com/계정명/my-telegram-bot.git
cd my-telegram-bot
```

---

## 9단계: Python 가상환경 생성

```bash
python3 -m venv venv
source venv/bin/activate
```

프롬프트 앞에 `(venv)` 가 표시되면 가상환경이 활성화된 것입니다.

---

## 10단계: 패키지 설치

```bash
pip install -r requirements.txt
```

설치가 완료되면 가상환경을 비활성화합니다:

```bash
deactivate
```

---

## 11단계: 봇 systemd 서비스 파일 생성

```bash
sudo nano /etc/systemd/system/telegram-bot.service
```

아래 내용을 붙여넣기 합니다.
`PROJECT_FOLDER`는 실제 폴더명으로, `YOUR_NEW_TOKEN`은 BotFather에서 받은 토큰으로 교체하세요:

```ini
[Unit]
Description=Telegram Bot
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/my-telegram-bot
ExecStart=/home/ubuntu/my-telegram-bot/venv/bin/python bot.py
Restart=always
RestartSec=5
Environment=TELEGRAM_BOT_TOKEN=YOUR_NEW_TOKEN

[Install]
WantedBy=multi-user.target
```

저장: `Ctrl+X` → `Y` → `Enter`

---

## 12단계: TELEGRAM_BOT_TOKEN 확인

서비스 파일의 `Environment=` 줄에 토큰이 올바르게 입력됐는지 확인:

```bash
sudo grep "TELEGRAM_BOT_TOKEN" /etc/systemd/system/telegram-bot.service
```

출력 예시:
```
Environment=TELEGRAM_BOT_TOKEN=8554361839:AAH...
```

---

## 13단계: 봇 자동 실행 등록

```bash
sudo systemctl daemon-reload
sudo systemctl enable telegram-bot
sudo systemctl start telegram-bot
```

---

## 14단계: 봇 상태 확인

```bash
sudo systemctl status telegram-bot
```

`Active: active (running)` 이 보이면 정상입니다.

---

## 15단계: 로그 확인

**실시간 로그:**
```bash
journalctl -u telegram-bot -f
```

**최근 50줄:**
```bash
journalctl -u telegram-bot -n 50
```

종료: `Ctrl+C`

---

## 16단계: 코드 업데이트 후 서버 반영

로컬에서 코드를 수정하고 GitHub에 push한 뒤, 서버에서 실행:

```bash
cd /home/ubuntu/my-telegram-bot

# 최신 코드 받기
git pull

# 패키지가 추가된 경우
source venv/bin/activate
pip install -r requirements.txt
deactivate

# 봇 재시작
sudo systemctl restart telegram-bot

# 상태 확인
sudo systemctl status telegram-bot
```

---

## 17단계: 대시보드 systemd 서비스 파일 생성

대시보드는 외부에 공개하지 않고 로컬(127.0.0.1)에서만 실행합니다.

```bash
sudo nano /etc/systemd/system/telegram-dashboard.service
```

```ini
[Unit]
Description=Telegram Bot Dashboard
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/my-telegram-bot
ExecStart=/home/ubuntu/my-telegram-bot/venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

저장: `Ctrl+X` → `Y` → `Enter`

```bash
sudo systemctl daemon-reload
sudo systemctl enable telegram-dashboard
sudo systemctl start telegram-dashboard
sudo systemctl status telegram-dashboard
```

---

## 18단계: 대시보드 SSH 터널로 안전하게 보기

서버의 8000 포트는 외부에 열지 않습니다.
내 PC에서 SSH 터널을 통해 안전하게 접속합니다.

**내 PC PowerShell에서 실행:**

```powershell
ssh -L 8000:127.0.0.1:8000 -i "다운로드한키파일.key" ubuntu@서버IP주소
```

터널이 연결된 상태에서 브라우저 주소창에 입력:

```
http://127.0.0.1:8000
```

---

## 보안 주의사항

| 항목 | 주의 내용 |
|------|----------|
| 봇 토큰 | GitHub에 절대 올리지 말 것. 노출 시 BotFather에서 즉시 Revoke 후 재발급 |
| messages.db | 채팅 데이터 포함. `.gitignore`에 추가되어 있으므로 GitHub에 올라가지 않음 |
| .env / config.py | 시크릿 파일은 `.gitignore`에 포함 |
| 8000 포트 | Oracle Cloud Security List에서 열지 말 것. SSH 터널로만 접속 |
| SSH 키 | 다운로드한 `.key` 파일을 타인과 공유하거나 GitHub에 올리지 말 것 |

---

## 자주 쓰는 명령어 모음

```bash
# 봇 시작/중지/재시작/상태
sudo systemctl start telegram-bot
sudo systemctl stop telegram-bot
sudo systemctl restart telegram-bot
sudo systemctl status telegram-bot

# 대시보드 시작/중지/재시작/상태
sudo systemctl start telegram-dashboard
sudo systemctl stop telegram-dashboard
sudo systemctl restart telegram-dashboard
sudo systemctl status telegram-dashboard

# 실시간 로그
journalctl -u telegram-bot -f
journalctl -u telegram-dashboard -f
```
