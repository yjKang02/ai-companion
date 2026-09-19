# 외부 어댑터

- [discord.py](discord.py): Discord SDK의 이벤트·송신·연결 수명을 처리한다.
- [lmstudio.py](lmstudio.py): LM Studio HTTP 요청과 응답을 공통 계약으로 변환한다.
- [memory.py](memory.py): 재시작하면 사라지는 임시 대화 기록을 제공한다.
- [room_memory.py](room_memory.py): 새 방·모델 연결·별도 실행 바인딩·입력 경로·입력 상태의 메모리 검증 구현. `InMemoryRoomBindings`는 generation을 검사하고 연결 해제에도 증가시킨다. 영구 저장소가 아니다.
- [sqlite_runtime.py](sqlite_runtime.py): 내부 운영 SQLite v1의 모델 연결·실행 바인딩·입력 접수표 구현. `SqliteRuntimeDatabase.create()`로 새 파일을 만들거나 `open()`으로 기존 파일만 연다. `connections`·`bindings`·`receipts`를 기존 포트에 주입한다. 모델 등록·수정도 비동기이므로 `await`가 필요하다.
- [model_executor.py](model_executor.py): 공유 HTTP 클라이언트에서 방별 모델 설정을 검증하고 비밀 참조를 해석해 기존 LM Studio 어댑터로 실행한다.

`room_memory.py`의 `InMemoryInputReceipts`는 외부 입력 출처·요청 키를 방 ID·불투명 입력 ID에 대응시킨다. 본문은 저장하지 않으며 `InMemoryRoomStore`의 입력·턴에는 외부 서비스 식별자를 넣지 않는다. 접수표도 메모리 구현이므로 영구 중복 방지·삭제 복구·삭제 후 재연결의 과거 이벤트 차단을 보장하지 않는다.

SQLite 어댑터는 내부 데이터만 보존한다. 방 DB·registry·삭제 작업 복구·비밀 저장은 포함하지 않으며 기존 CLI의 기본 저장소를 바꾸지 않는다. 자동 마이그레이션·DB 재생성·메모리 대체를 하지 않고 `StorageUnavailable`로 실패한다. DB 경로·부모 폴더·접근 권한은 호출자가 준비한다. 상세 계약은 [내부 SQLite 구현](../../../docs/백엔드구현설계.md#내부-운영-sqlite-어댑터)을 따른다.

어댑터를 추가할 때 기존 대화 경로의 [공통 대화 포트](../application/ports.py) 또는 새 방 경로의 [방 실행 포트](../application/room_ports.py)에서 해당 계약을 구현하고 [조립 지점](../bootstrap.py)에 연결한다.
