# ai-companion

메신저와 모델을 교체할 수 있는 개인용 AI Companion 프로젝트다. Python 기반으로 Discord와 LM Studio부터 연결하고, 페르소나·개인 데이터를 조회하고 편집하는 시스템으로 확장한다.

현재 `main`은 환경 설정과 설계·운영 문서만 포함하는 초기 기준이다. 앱 기능은 `feature/discord-lmstudio-connection`에서 리뷰한 뒤 병합한다. 분리 전 구현은 `backup/initial-implementation-e625daf`에 보존했다. 원격은 [yjKang02/ai-companion](https://github.com/yjKang02/ai-companion)이다.

## 시작하기

Python 3.11 이상을 사용한다. Windows 개발 기준은 Python 3.13이며 프로젝트 루트에서 아래 명령을 실행한다.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -c "import ai_companion"
```

`main`의 패키지는 설치·도구 설정을 검증하는 빈 진입점이다. 봇 실행 명령과 실제 설정 로더는 기능 브랜치에 포함한다. `.env.example`은 앞으로 사용할 설정 목록이며 토큰 값은 비어 있다. `.env`, 가상환경, 캐시, DB, 로그는 Git에서 제외한다.

## 구조와 확장

| 위치 | 역할 |
| --- | --- |
| `pyproject.toml` | Python 의존성, 패키징, pytest·Ruff·mypy 설정 |
| `src/ai_companion/__init__.py` | 설치 확인용 최소 패키지. 애플리케이션 동작 없음 |
| `.env.example` | Discord·LM Studio 설정 예시 |
| `.github/` | CI, Dependabot, PR 템플릿 |
| `.coderabbit.yaml` | 한국어 코드 리뷰 지침 |
| `docs/` | 기존 대화에서 정리한 계획·조사·설계·기술 선택과 운영 안내 |

구현 방향은 [기본 설계](docs/기본설계.md)를 따른다. 작업 상태와 목표일은 [프로젝트 문서](docs/프로젝트.md#작업-목록) 한 곳에서 관리한다. 구현 전 문서와 백업의 과거 검증 결과를 main의 기능 완료로 해석하지 않는다.

## 개발 검증

```powershell
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m pip check
```

초기 CI는 Ubuntu의 Python 3.11·3.13, Windows의 Python 3.13에서 설치·import를 확인한다. 기능 브랜치에서는 같은 CI의 설치 점검 다음 단계가 pytest로 바뀐다. 스타일과 타입 검사는 Ubuntu 한 환경에서 수행한다. Discord 토큰·LM Studio 서버 없이 실행한다.

## 리뷰와 저장소 운영

기능 브랜치에서 변경하고 `main` 대상으로 PR을 작성한다. CI와 리뷰 내용을 확인한 뒤 사용자가 병합한다. 커밋 제목은 `init:`, `feat:`, `fix:`, `docs:`, `chore:`처럼 유형을 쓰고 제목·상세 본문은 한국어로 작성한다.

저장소 이름은 `ai-companion`이다. GitHub에서 사용자가 만든 최초 `.gitignore`와 `LICENSE`를 보존한다. CodeRabbit 설치·main 보호·squash merge 설정은 파일만으로 활성화되지 않는다. [저장소 운영 안내](docs/저장소운영.md)에서 적용할 설정과 준비 범위를 확인한다.

[프로젝트 문서](docs/프로젝트.md) · [설계](docs/기본설계.md) · [작업 규칙](AGENTS.md)
