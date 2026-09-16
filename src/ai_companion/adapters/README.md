# 외부 어댑터

- [discord.py](discord.py): Discord SDK의 이벤트·송신·연결 수명을 처리한다.
- [lmstudio.py](lmstudio.py): LM Studio HTTP 요청과 응답을 공통 계약으로 변환한다.
- [memory.py](memory.py): 재시작하면 사라지는 임시 대화 기록을 제공한다.

어댑터를 추가할 때 [공통 포트](../application/ports.py)를 구현하고 [조립 지점](../bootstrap.py)에 연결한다.
