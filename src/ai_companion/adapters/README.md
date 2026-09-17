# 외부 어댑터

- [discord.py](discord.py): Discord SDK의 이벤트·송신·연결 수명을 처리한다.
- [lmstudio.py](lmstudio.py): LM Studio HTTP 요청과 응답을 공통 계약으로 변환한다.
- [memory.py](memory.py): 재시작하면 사라지는 임시 대화 기록을 제공한다.
- [room_memory.py](room_memory.py): 새 방·모델 연결·입력 경로·입력 상태의 메모리 검증 구현. 영구 저장소가 아니다.
- [model_executor.py](model_executor.py): 공유 HTTP 클라이언트에서 방별 모델 설정을 검증하고 비밀 참조를 해석해 기존 LM Studio 어댑터로 실행한다.

어댑터를 추가할 때 [공통 포트](../application/ports.py)를 구현하고 [조립 지점](../bootstrap.py)에 연결한다.
