# MADR-001: Python·discord.py와 LM Studio로 첫 연결 구현

- 상태: 승인됨 — 사용자가 현재 폴더에서 discord.py와 LM Studio 연결 구현을 요청했다.
- 결정일: 2026-09-13
- 관련 이슈: [M01·M02 작업 원본](../프로젝트.md#작업-목록)

## 1. 배경과 문제

기존 폴더에는 설계·조사 문서만 있었다. 사용자는 이 폴더에서 프로그래밍을 시작하고 기존 문서를 docs로 옮기도록 요청했다. 메신저와 모델 교체를 고려하면서, 첫 모델 서버는 LM Studio로 지정했다.

## 2. 결정

Python과 discord.py로 DM 수신·전송을 구현하고, LM Studio의 OpenAI 호환 Chat Completions API를 HTTPX 비동기 클라이언트로 호출한다. 기존 계획·조사는 docs에 보관하고 프로젝트 루트에 README·AGENTS·Python 패키지·테스트를 둔다.

공통 데이터는 불변 dataclass, 외부 계약은 Protocol로 표현한다. 대화 서비스에 ChatModel·MessageSink·ConversationStore를 주입하고, 모델 요청 변환과 Discord 이벤트 처리는 각각의 어댑터 안에 둔다.

## 3. 검토한 대안

- Python·TypeScript·C#·Java/Kotlin은 [언어 비교](../조사/002-개발언어-비교.md)에서 검토했다. Go는 사용자가 제외했다.
- 최초 기획의 Ollama는 참고 대상으로 남기고, 사용자 요청에 따라 첫 서버를 LM Studio로 바꿨다.
- 공급자 전용 SDK 대신 첫 텍스트 연결에 필요한 HTTP API만 사용한다. LM Studio 전용 모델 관리 기능이 필요해지면 SDK 도입을 다시 검토한다.

## 4. 선택 이유

사용자가 선택한 Python·Discord·LM Studio를 가장 짧은 대화 흐름으로 연결하면서, SDK가 코어로 전파되지 않는 경계를 먼저 검증할 수 있다. LM Studio는 대화 이력을 전달하는 호환 API를 제공하며, HTTPX는 비동기 호출과 테스트용 transport 주입을 지원한다. [LM Studio](https://lmstudio.ai/docs/developer/openai-compat/chat-completions), [HTTPX](https://www.python-httpx.org/async/)

## 5. 결과와 감수하는 비용

### 기대 효과

- Discord를 실행하지 않고도 모델 연결을 확인할 수 있다.
- 가짜 모델·송신기를 사용해 대화 로직과 오류 처리를 검증할 수 있다.
- 외부 API나 메신저를 추가할 때 수정할 위치가 명확하다.

### 감수하는 비용

- 첫 연결 단계의 대화 기록과 중복 수신 방지는 메모리에만 존재한다. 재시작 후 복구·DB·실시간 편집은 아직 없다.
- 모델 오류 시 한 번 안내하고 자동 재시도·원격 우회는 하지 않는다.
- 초기 큐는 하나이며 추론도 직렬화한다. 큐가 가득 차면 새 입력을 버리고 로그만 남긴다. 이는 후속 설계의 대기 안내 정책보다 단순한 초기 동작이다.
- 공통 모델 계약은 텍스트 생성만 다룬다. 스트리밍·도구·이미지 지원은 별도 능력 계약이 필요하다.
- 실제 Discord 왕복과 LM Studio 생성은 오프라인 테스트와 구분하여 확인해야 한다.
