# 대화 유스케이스

[ports.py](ports.py)의 인터페이스로 외부 의존성을 표현하고 [conversation.py](conversation.py)에서 한 턴을 처리한다. Discord·HTTP·모델 SDK를 이 계층으로 가져오지 않는다.

새 방 구조는 [room_ports.py](room_ports.py)의 저장·연결·실행 포트와 [rooms.py](rooms.py)의 `RoomService`·`RoutedRoomService`로 구성한다. 방별 모델·컨텍스트 적용과 revision 검증을 수행하고 생성 결과를 반환한다. 실제 외부 송신과 인증은 호출 경계에서 처리한다. 기존 conversation 경로는 CLI 전환 전까지 유지한다. 계약과 전환 순서는 [백엔드 구현 설계](../../../docs/백엔드구현설계.md)를 따른다.
