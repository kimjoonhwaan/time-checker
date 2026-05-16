# Windows 설치기 빌드

## 사전 준비

```bash
pip install -r ../requirements.txt
pip install pyinstaller
```

[Inno Setup 6](https://jrsoftware.org/isdl.php) 설치 후 `ISCC.exe`가 PATH에 있어야 함.

## 1. PyInstaller로 .exe 만들기

```bat
cd installer
build.bat
```

산출물: `..\dist\TimeChecker.exe`

## 2. Inno Setup으로 설치기 만들기

```bat
ISCC timechecker.iss
```

산출물: `installer\Output\TimeCheckerSetup.exe`

## 설치 마법사 동작

1. 설치 경로 선택 (기본 `%LocalAppData%\Programs\TimeChecker`)
2. "Windows 시작 시 자동 실행" 체크 가능
3. **Server URL + API Key 입력 페이지** — 비워두면 로컬 모드
4. 설치 완료 후 자동 실행

입력한 서버 URL/API 키는 `%APPDATA%\TimeChecker\config.json`에 저장됨.

## 파일 위치

| 종류 | 경로 |
|---|---|
| 실행파일 | 설치 경로 (사용자 선택) |
| 설정/오프라인 큐/로그 | `%APPDATA%\TimeChecker\` |
| 자동실행 등록 | `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` |

## SmartScreen 경고

코드 서명 인증서가 없으면 첫 실행 시 "Windows에서 PC를 보호했습니다" 경고가 뜬다. **추가 정보 → 실행**을 눌러 진행. 대중 배포 시에는 서명 인증서($100~/년) 구매를 검토.
