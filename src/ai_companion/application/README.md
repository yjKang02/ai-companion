# 대화 유스케이스

[ports.py](ports.py)의 인터페이스로 외부 의존성을 표현하고 [conversation.py](conversation.py)에서 한 턴을 처리한다. Discord·HTTP·모델 SDK를 이 계층으로 가져오지 않는다.

새 방 구조는 [room_ports.py](room_ports.py)의 저장·연결·실행 포트와 [rooms.py](rooms.py)의 `RoomService`·`RoutedRoomService`로 구성한다. 방별 모델·컨텍스트 적용과 revision 검증을 수행하고 생성 결과를 반환한다. 실제 외부 송신과 인증은 호출 경계에서 처리한다. 기존 conversation 경로는 CLI 전환 전까지 유지한다. 계약과 전환 순서는 [백엔드 구현 설계](../../../docs/백엔드구현설계.md)를 따른다.

방 생성·편집에는 연결 ID 없는 `RoomModelConfig`를 사용한다. `RoomBindings`를 별도 주입하며 `bind()`로 실행 연결을 지정·해제한다. `get()`·`history()`는 모델 연결 없이 조회할 수 있고 `binding()`은 로컬 연결 상태를 조회한다. 콘텐츠 revision과 바인딩 generation은 독립적이다. 생성된 미연결 방은 실행 검증이 완료된 상태가 아니다.
