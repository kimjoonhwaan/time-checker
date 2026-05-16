# TimeChecker

Windows에서 사용한 컴퓨터 시간을 자동으로 측정하고, 웹 대시보드로 시각화하는 시간 추적 앱.

두 가지 모드로 동작:

- **LOCAL 모드** — 트래커·DB·대시보드 전부 PC 한 대에서 실행 (오리지널 동작)
- **REMOTE 모드** — 트래커만 PC에서 실행, 측정값을 Railway에 배포된 서버로 HTTP 전송. 어디서든 브라우저로 조회.

```
[Windows PC]                              [Railway]
 TimeChecker.exe (tray)                    server.py (Flask)
  ├ tracker.py  ──HTTP POST──▶            ├ /api/ingest/*  (write)
  └ %APPDATA%\TimeChecker\                 ├ /api/summary/* (read)
      ├ config.json                        └ /             (dashboard)
      ├ queue.db  (오프라인 큐)
      └ logs\                              SQLite (Volume에 마운트)
                                           /data/timetracker.db
```

---

## 설치 & 실행

### 옵션 A. 개발 모드 (Python 스크립트로 직접 실행)

```bash
pip install -r requirements.txt
python main.py
```

`config.json`에 `server_url`이 없으면 자동으로 LOCAL 모드(로컬 SQLite + `http://localhost:5000`).

### 옵션 B. 일반 사용자용 (.exe 설치 마법사)

빌드 절차는 [`installer/README.md`](installer/README.md) 참고.

사용자 입장의 설치 순서:

1. `TimeCheckerSetup.exe` 더블클릭
2. 설치 경로 선택 (기본 `%LocalAppData%\Programs\TimeChecker`)
3. **"Windows 시작 시 자동 실행"** 체크 (권장)
4. **Server URL + API Key 입력 페이지** —
   - 비워두면 LOCAL 모드 (로컬 DB + `http://localhost:5000`)
   - Railway URL을 넣으면 REMOTE 모드 (서버로 전송)
5. 설치 완료 후 자동으로 트레이에 아이콘 등장

설치된 파일 위치:

| 종류 | 경로 |
|---|---|
| 실행파일 | 설치 경로 (사용자 선택) |
| 설정·오프라인 큐·로그 | `%APPDATA%\TimeChecker\` |
| 자동실행 등록 | `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` |

### 옵션 C. 서버 배포 (Railway)

자세한 절차는 아래 [Railway 배포](#railway-배포) 섹션 참고.

---

## Railway 배포

### 1회만 하면 되는 셋업

1. [https://railway.app](https://railway.app)에 GitHub 계정으로 로그인
2. **New Project → Deploy from GitHub repo → 이 저장소 선택**
3. Railway가 `Dockerfile`을 자동 감지해 빌드 시작 (1~2분)
4. 빌드 완료 후 서비스 설정 진입:
   - **Variables** 탭에서 환경변수 추가:
     - `TIMECHECKER_API_KEY` = `<32바이트 랜덤 문자열>` (예: `python -c "import secrets;print(secrets.token_hex(32))"`)
     - `TIMECHECKER_DATA_DIR` 은 `Dockerfile`에 이미 `/data`로 박혀 있음
   - **Volumes** 탭에서 Volume 추가:
     - Name: `timechecker-data`
     - Mount path: `/data`
   - **Settings → Networking** 에서 **Generate Domain** 클릭 → `*.up.railway.app` URL 발급
5. 위 도메인이 곧 사용자가 클라이언트 설치 시 입력할 `server_url`

### 동작 확인

```bash
# 헬스체크
curl https://<your-app>.up.railway.app/

# Ingest 테스트 (API 키 필요)
curl -X POST https://<your-app>.up.railway.app/api/ingest/heartbeat \
  -H "X-API-Key: <YOUR_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"device_id":"smoke-test","state":"tracking","idle_seconds":0}'
```

---

## 설정 (`config.json`)

설정 파일은 다음 우선순위로 로드됨:

1. `TIMECHECKER_CONFIG_PATH` 환경변수
2. `%APPDATA%\TimeChecker\config.json` (Windows 사용자 폴더)
3. 프로젝트 루트의 `config.json` (개발 fallback)

### 주요 필드

| 필드 | 기본값 | 설명 |
|---|---|---|
| `server_url` | (없음) | 설정하면 REMOTE 모드로 전환. 예: `https://timechecker.up.railway.app` |
| `api_key` | (없음) | REMOTE 모드 시 `X-API-Key` 헤더로 전송 |
| `device_id` | 자동생성 | `호스트명-UUID8자` 형식. 다중 PC 구분용 |
| `idle_threshold_seconds` | 60 | 입력 없는 시간이 이 값을 넘으면 세션 종료 |
| `poll_interval_seconds` | 30 | 트래커 샘플링 간격 |
| `flask_port` | 5000 | LOCAL 모드 대시보드 포트 (자동 증가) |
| `excluded_processes` | `[...]` | 추적 제외 프로세스명 (vlc.exe 등) |
| `excluded_title_keywords` | `[...]` | 추적 제외 창 제목 키워드 (YouTube 등) |
| `data_dir` | OS 표준 | 데이터 폴더 경로 override |
| `db_path` | `<data_dir>/timetracker.db` | 서버 DB 파일 경로 override |
| `queue_db_path` | `<data_dir>/queue.db` | 로컬 오프라인 큐 파일 경로 override |

### 환경변수 (config보다 우선)

| 변수 | 용도 |
|---|---|
| `TIMECHECKER_DATA_DIR` | 데이터 폴더 |
| `TIMECHECKER_DB_PATH` | 서버 SQLite 파일 |
| `TIMECHECKER_QUEUE_DB_PATH` | 클라이언트 오프라인 큐 |
| `TIMECHECKER_CONFIG_PATH` | config.json 위치 |
| `TIMECHECKER_LOG_DIR` | 로그 디렉터리 |
| `TIMECHECKER_API_KEY` | 서버: ingest 인증 키 / 클라이언트: 송신 키 |
| `TIMECHECKER_SERVER_URL` | 클라이언트: REMOTE 서버 URL |
| `TIMECHECKER_DEVICE_ID` | 디바이스 식별자 override |
| `PORT` | 서버: HTTP 포트 (Railway 자동 주입) |

---

## 아키텍처

| 파일 | 역할 |
|---|---|
| `main.py` | 로컬 진입점 — `server_url` 유무로 LOCAL/REMOTE 모드 분기, 스레드 wiring |
| `server.py` | 서버 진입점 — Flask만 띄움 (Railway/gunicorn용) |
| `tracker.py` | 입력·창 감지 + 세션 상태 머신 (`IdleDetector`, `WindowDetector`, `TrackerLoop`) |
| `database.py` | SQLite 래퍼 — read/write 분리, `client_event_id` 멱등 처리 |
| `ingest_client.py` | `DatabaseManager`와 동일 시그니처의 HTTP 클라이언트 + 오프라인 큐 |
| `app.py` | Flask 라우트 — `/api/summary/*` (read), `/api/ingest/*` (write, API 키 인증) |
| `tray.py` | `pystray` 트레이 아이콘 + 메뉴 |
| `paths.py` | 경로 해석 (env → config → OS 기본값) |
| `templates/dashboard.html` | Chart.js 대시보드 |

### 데이터 흐름 (REMOTE 모드)

1. `TrackerLoop._tick()` — 30초마다 입력 idle 검사 + 포그라운드 창 검사
2. 세션 시작 → `IngestClient.open_session()` → 로컬 UUID 즉시 반환, 백그라운드로 `POST /api/ingest/session/open`
3. 네트워크 실패 시 `queue.db`에 적재, 백그라운드 워커가 30초마다 재시도 (UNIQUE 제약으로 멱등)
4. 창 변경 → `close_app_activity` + `open_app_activity` (UUID 기반)
5. 30초마다 별도 스레드가 `heartbeat` 전송 (상태/idle/제외앱)

서버는 `client_event_id`(UUID)를 PK처럼 사용해 동일 이벤트가 여러 번 와도 한 번만 적용. 클라이언트는 서버의 정수 id를 알 필요 없음.

---

## REST API

### 읽기 (대시보드용)

| Endpoint | Method | 설명 |
|---|---|---|
| `/` | GET | Dashboard HTML |
| `/api/summary/today` | GET | 오늘 총 시간 + 세션 목록 |
| `/api/summary/week` | GET | 최근 7일 |
| `/api/apps/today` | GET | 오늘 앱별 사용량 |
| `/api/sessions/<date>` | GET | 특정 날짜 세션 |
| `/api/tracker/status` | GET | 현재 상태 (REMOTE 모드는 heartbeat 기반) |
| `/api/stats/{daily,weekly,monthly}` | GET | 통계 |
| `/api/todos/*` | GET/POST/PUT/DELETE | 할 일 CRUD + 타이머 |
| `/api/config` | GET | 현재 config |

### 쓰기 (Ingest, 인증 필요)

모든 ingest 엔드포인트는 `X-API-Key` 헤더 필수.

| Endpoint | 설명 |
|---|---|
| `POST /api/ingest/session/open` | `{client_event_id, start_time, date, device_id}` |
| `POST /api/ingest/session/close` | `{session_client_event_id, end_time}` |
| `POST /api/ingest/activity/open` | `{client_event_id, session_client_event_id, process_name, window_title, start_time, device_id}` |
| `POST /api/ingest/activity/close` | `{activity_client_event_id, end_time}` |
| `POST /api/ingest/todo/start` | `{todo_id}` |
| `POST /api/ingest/todo/stop` | `{todo_id, reason}` |
| `GET  /api/ingest/todo/active` | 서버 측 현재 활성 todo |
| `POST /api/ingest/heartbeat` | `{device_id, state, idle_seconds, excluded_app}` |

---

## 개발

### 로컬에서 서버만 띄우기

```bash
TIMECHECKER_API_KEY=devkey TIMECHECKER_DB_PATH=dev.db PORT=5099 python server.py
```

### 로컬에서 클라이언트로 보내보기 (REMOTE 모드 시뮬)

`config.json`에 추가:

```json
{
  "server_url": "http://127.0.0.1:5099",
  "api_key": "devkey"
}
```

그리고 `python main.py`.

---

## 라이선스

MIT
