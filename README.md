# ai-companion

원격 저장소: [yjKang02/ai-companion](https://github.com/yjKang02/ai-companion)

Discord에서 짧은 일상 대화를 나누는 Python 애플리케이션이다. 첫 모델 서버는 **LM Studio**이며, 메신저와 모델을 교체할 수 있도록 공통 데이터·포트·어댑터로 나눈다.

2026-09-17부터 다음 개발은 [방별 컨텍스트 편집·삭제·영구 저장을 갖춘 개인용 채팅 기반](docs/기초설계.md)을 중심으로 설계한다. 아래 실행 안내는 기존 Discord·LM Studio 연결 구현에 해당하며, 현재 메모리 저장소는 새 재시작 보존 요구를 충족하지 않는다.

이 문서는 `feature/discord-lmstudio-connection`의 구현을 설명한다. `main`은 환경·설계만 담은 init 기준이며, 이 기능은 리뷰 후 병합한다. 분리 전 코드 원본은 `backup/initial-implementation-e625daf`에 보존했다. [저장소와 리뷰 운영](docs/저장소운영.md)에서 CI·Dependabot·CodeRabbit 설정과 GitHub 적용 항목을 확인한다.

현재 폴더에서 프로그래밍을 시작한다는 사용자 요청에 따라 코드와 문서를 함께 관리한다. 기존 계획·설계·조사는 [docs](docs/프로젝트.md)로 이동했다. 작업 상태·목표일의 원본은 [프로젝트 문서](docs/프로젝트.md#작업-목록)다.

## 시작하기

Python 3.11 이상이 필요하다. 아래 명령은 이 README가 있는 프로젝트 루트에서 실행한다. Windows PowerShell 기준이며 가상환경 활성화 없이 실행할 수 있다.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

이미 `.env`가 있으면 복사 단계를 생략하고 필요한 항목만 수정한다. 환경변수가 `.env`보다 우선한다. 토큰은 로컬 `.env`에 넣으며 문서나 채팅에 붙여 넣지 않는다.

### 1. LM Studio 준비

1. LM Studio에서 사용할 대화 모델을 준비하고 Developer 화면에서 서버를 시작한다.
2. 기본 API 주소는 `http://127.0.0.1:1234/v1`이다. 실제 서버 주소와 다르면 `.env`의 `LMSTUDIO_BASE_URL`을 수정한다.
3. 인증을 켰다면 `LMSTUDIO_API_KEY`에 해당 서버의 토큰을 입력한다. 인증을 끈 로컬 서버에서는 빈 값으로 둔다.
4. 모델 목록을 조회한다. Discord 설정과 모델 ID 없이 실행할 수 있다.

HTTP 연결은 `localhost`, IPv4 loopback(`127.0.0.0/8`), IPv6 loopback(`::1`)에서만 허용한다. LAN 주소를 포함한 다른 호스트는 HTTPS가 필요하다. 모델 서버를 다른 기기에 둘 경우 HTTPS 엔드포인트를 준비하고 그 주소를 설정한다.

```powershell
.venv\Scripts\python.exe -m ai_companion check-model
```

반환된 모델 ID를 `.env`의 `LMSTUDIO_MODEL`에 입력하고 실제 생성을 확인한다.

```powershell
.venv\Scripts\python.exe -m ai_companion check-model --prompt "한국어로 짧게 인사해 줘."
```

목록 조회 성공과 모델 생성 성공은 별개다. 현재 어댑터는 `/v1/chat/completions`에 비스트리밍 요청을 보내며, LM Studio의 별도 모델 관리·다운로드는 수행하지 않는다. 추론 태그가 본문에 섞인 응답은 거부한다. 그런 모델은 LM Studio의 추론 출력 설정을 확인하거나 일반 대화 모델로 시험한다.

근거: [LM Studio 서버](https://lmstudio.ai/docs/developer/core/server), [Chat Completions](https://lmstudio.ai/docs/developer/openai-compat/chat-completions), [인증](https://lmstudio.ai/docs/developer/core/authentication).

### 2. Discord 준비

1. [Discord Developer Portal](https://discord.com/developers/applications)에서 애플리케이션과 봇을 만들고 봇 토큰을 `.env`의 `DISCORD_TOKEN`에 입력한다.
2. 개발용 서버에 `bot` scope로 설치한다. 이 버전에는 관리자 권한이나 slash command scope가 필요하지 않다.
3. Discord의 개발자 모드를 켜고 본인의 사용자 ID를 복사해 `DISCORD_ALLOWED_USER_IDS`에 입력한다. 빈 목록으로 실행하면 설정 오류가 난다.
4. 봇을 시작하고, 봇 프로필에서 1:1 DM을 보낸다. DM이 열리지 않으면 공통 서버와 DM 허용 설정을 확인한다.

```powershell
.venv\Scripts\python.exe -m ai_companion run
```

`discord_ready` 로그가 출력되면 Discord 로그인이 완료된 것이다. 서버 채널, 그룹 DM, 봇 발화, 허용되지 않은 계정, 텍스트 없는 메시지는 무시한다. DM 수신 intent만 사용하며, 앱과의 DM은 Message Content 제한의 예외다. 서버 일반 채팅으로 확장할 때는 intents와 권한을 별도로 검토한다. [Discord Gateway](https://docs.discord.com/developers/events/gateway)

종료는 `Ctrl+C`다. 진행 중인 요청과 대기 입력은 취소되며 자동 재전송되지 않는다. 다른 경로의 설정 파일은 `--env-file 경로`를 하위 명령 앞에 지정한다.

## 구조와 확장

```text
docs/                         계획·설계·조사·결정
src/ai_companion/
  domain.py                   메시지·대화 주소·모델 요청과 결과 dataclass
  application/
    ports.py                  ChatModel, MessageSink, Messenger, ConversationStore
    conversation.py           SDK에 의존하지 않는 대화 서비스
  adapters/
    discord.py                DM 필터·수신 큐·타이핑·전송·연결 수명
    lmstudio.py               HTTP 요청·응답 검증·모델 오류 변환
    memory.py                 제한된 임시 대화 기록
  config.py                   환경 설정과 검증
  bootstrap.py                객체 생성·주입·자원 정리
  __main__.py                 실행 및 모델 점검 명령
tests/                        외부 연결 없는 회귀·계약 검증
```

`ConversationService`에 모델·송신기·저장소를 생성자로 주입한다. 외부 사용자 ID는 플랫폼과 묶으며, 대화 기록은 플랫폼·대화·사용자를 함께 키로 사용한다. 내부 사용자 계정 매핑은 지속 저장소 단계에서 추가한다.

- 모델 변경: LM Studio 안에서 모델만 바꾸면 `.env`를 수정하고 재시작한다. 다른 공급자는 `ChatModel.generate()` 구현과 조립 설정을 추가한다. OpenAI 호환이라는 이유만으로 모든 공급자를 지원한다고 가정하지 않는다.
- 메신저 변경: 수신 이벤트를 `IncomingMessage`로 변환하고 `Messenger` 포트를 구현한다. 타이핑 표시가 없으면 아무 동작 없는 비동기 컨텍스트로 구현한다.
- 저장소 변경: `ConversationStore`를 구현해 교체한다. 현재 포트는 메모리 저장소용 동기 계약이며, SQLite 도입 시 비동기 I/O 계약과 삭제·버전 정책을 함께 구체화한다.

실제 LM Studio HTTP 응답은 어댑터 안에서만 해석한다. 대화 서비스는 공급자의 JSON 필드나 Discord 객체를 모른다. 모델 호출은 비동기로 수행하고, 초기 추론은 1개씩 처리한다.

## 첫 연결 버전의 범위

최근 대화는 기본 20개 메시지, 최대 128개 대화를 **메모리에만** 보관한다. 재시작하면 사라진다. 성공적으로 보낸 턴만 기록하고, 최근 2048개 이벤트에 대해 프로세스 내 중복 처리를 막는다. 이 값들은 모델의 토큰 예산이나 영구 보존을 보장하지 않는다.

응답은 최대 600자로 제한한 메시지 하나다. 텍스트 입력은 4000자까지 받는다. 대기 큐는 기본 32개이며, 가득 차면 새 입력을 버리고 내용 없는 경고 로그를 남긴다. 모델 오류는 일반 안내 한 번으로 전달하고 전송 실패·결과 불명은 자동 재전송하지 않는다. 추론 제한 시간은 기본 60초이며 큐 대기 시간은 별도다.

SQLite, 웹 관리 화면, 페르소나 실시간 편집, 개인 사실 저장·삭제, 자동 기억, 외부 API 어댑터는 후속 작업이다. 현재 페르소나는 `.env`의 시스템 지시이며 변경 후 재시작해야 한다. 이 버전은 전체 제품 MVP 이전의 연결 기반이다.

## 개발 검증

```powershell
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe -m mypy
```

자동 테스트는 가짜 모델·메신저와 HTTPX MockTransport를 사용하며 실제 Discord 메시지를 보내지 않는다. 실제 접속은 위 `check-model`과 `run` 명령으로 별도 확인한다. 테스트 통과를 실제 서비스 연결 성공으로 해석하지 않는다.

코어 의존성 테스트는 표준 라이브러리와 프로젝트 내부 모듈의 정적 import를 허용하고, 어댑터·조립 계층으로의 직접 의존을 차단한다. 상대 import와 패키지 초기화 파일도 검사하며, domain을 파일에서 패키지로 분리할 수 있다. 새 내부 모듈 이름을 금지 목록에 넣지 않는다. 동적 import와 간접 의존은 이 정적 검사만으로 보장하지 않는다.

[최초 구현 리뷰](docs/초기구현리뷰.md)는 수정 전 타이핑 오류의 재현 기록이다. 현재 타이핑 표시의 HTTP 오류는 내용 없는 경고로 남기고 답변 생성을 계속한다. 본문의 모델 오류와 취소는 그대로 전달한다. 리뷰 반영과 실제 연결 검증 상태는 [작업 목록](docs/프로젝트.md#작업-목록)에서 관리한다.

[기본 설계](docs/기본설계.md) · [작업 목록](docs/프로젝트.md#작업-목록) · [작업 규칙](AGENTS.md)
