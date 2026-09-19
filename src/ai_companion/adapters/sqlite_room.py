"""외부 방 파일 하나의 저장 구성요소. registry·파일 삭제·RoomStore 조립은 별도다."""

import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import TypeVar
from uuid import uuid4

from ai_companion.adapters.sqlite_support import report_failure, settled_thread
from ai_companion.application.room_ports import (
    InputConflict,
    RevisionConflict,
    StorageUnavailable,
)
from ai_companion.domain import (
    ChatMessage,
    ChatResult,
    Role,
    Room,
    RoomContext,
    RoomModelConfig,
    RoomTurn,
    StoredRoomInput,
    TokenUsage,
    TurnState,
)

_T = TypeVar("_T")
_APPLICATION_ID = 0x41494342
_VERSION = 1
_SCHEMA = (
    "CREATE TABLE metadata (id INTEGER PRIMARY KEY CHECK(id = 1), storage_id TEXT NOT NULL)",
    """CREATE TABLE room (
        slot INTEGER PRIMARY KEY CHECK(slot = 1), id TEXT NOT NULL UNIQUE,
        current_revision INTEGER NOT NULL,
        FOREIGN KEY(id, current_revision) REFERENCES revisions(room_id, revision)
        DEFERRABLE INITIALLY DEFERRED)""",
    """CREATE TABLE revisions (
        room_id TEXT NOT NULL REFERENCES room(id), revision INTEGER NOT NULL CHECK(revision >= 1),
        name TEXT NOT NULL, character TEXT NOT NULL, user TEXT NOT NULL, instructions TEXT NOT NULL,
        model_id TEXT NOT NULL, temperature REAL NOT NULL, max_tokens INTEGER NOT NULL,
        timeout REAL NOT NULL, PRIMARY KEY(room_id, revision))""",
    """CREATE TABLE inputs (
        id TEXT PRIMARY KEY NOT NULL, room_id TEXT NOT NULL, revision INTEGER NOT NULL,
        seq INTEGER NOT NULL UNIQUE, body TEXT NOT NULL,
        UNIQUE(id, room_id, revision),
        FOREIGN KEY(room_id, revision) REFERENCES revisions(room_id, revision))""",
    """CREATE TABLE turns (
        input_id TEXT PRIMARY KEY NOT NULL, room_id TEXT NOT NULL, revision INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN
          ('pending', 'completed', 'failed', 'cancelled', 'interrupted', 'superseded')),
        finish_reason TEXT, has_usage INTEGER NOT NULL DEFAULT 0 CHECK(has_usage IN (0, 1)),
        input_tokens INTEGER, output_tokens INTEGER,
        FOREIGN KEY(input_id, room_id, revision) REFERENCES inputs(id, room_id, revision))""",
    """CREATE TABLE messages (
        input_id TEXT PRIMARY KEY NOT NULL REFERENCES turns(input_id), content TEXT NOT NULL)""",
)


def validate_room(room: Room) -> None:
    if not isinstance(room.model, RoomModelConfig):
        raise TypeError("방에는 연결 ID 없는 모델 설정만 저장할 수 있습니다.")
    if not room.id.strip() or not room.name.strip() or not room.context.character.strip():
        raise ValueError("방 ID·이름·캐릭터가 필요합니다.")
    if not room.model.model_id.strip() or type(room.revision) is not int or room.revision < 1:
        raise ValueError("모델 ID와 양의 정수 revision이 필요합니다.")
    room.model.validate_options()


def _insert_revision(db: sqlite3.Connection, room: Room) -> None:
    db.execute(
        "INSERT INTO revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            room.id,
            room.revision,
            room.name,
            room.context.character,
            room.context.user,
            room.context.instructions,
            room.model.model_id,
            room.model.temperature,
            room.model.max_tokens,
            room.model.timeout,
        ),
    )


class SqliteRoomDatabase:
    """외부 방 파일의 단일 입력/단일 attempt 저장. 제품 수명주기 저장소는 아니다."""

    def __init__(self, path: Path, room_id: str, storage_id: str) -> None:
        self._path, self._room_id, self._storage_id = path, room_id, storage_id

    @property
    def storage_id(self) -> str:
        """등록부가 같은 방 ID의 다른 파일 교체를 검사할 때 사용하는 불투명 식별자."""
        return self._storage_id

    @classmethod
    async def create(cls, path: Path, room: Room) -> "SqliteRoomDatabase":
        validate_room(room)
        if room.revision != 1:
            raise RevisionConflict("새 방은 revision 1에서 시작해야 합니다.")

        def initialize() -> tuple[Path, str]:
            try:
                resolved = path.resolve()
                with resolved.open("xb"):
                    pass
                storage_id = str(uuid4())
                with closing(sqlite3.connect(resolved.as_uri() + "?mode=rw", uri=True)) as db:
                    db.execute("PRAGMA foreign_keys = ON")
                    with db:
                        db.execute("BEGIN IMMEDIATE")
                        for statement in _SCHEMA:
                            db.execute(statement)
                        db.execute("INSERT INTO metadata VALUES (1, ?)", (storage_id,))
                        db.execute("INSERT INTO room VALUES (1, ?, 1)", (room.id,))
                        _insert_revision(db, room)
                        db.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
                        db.execute(f"PRAGMA user_version = {_VERSION}")
                return resolved, storage_id
            except (OSError, sqlite3.Error) as error:
                report_failure("room_create", error)
                raise StorageUnavailable(
                    "방 DB를 생성하지 못했습니다. 기존 파일은 덮어쓰지 않습니다."
                ) from None

        resolved, storage_id = await settled_thread(initialize)
        return cls(resolved, room.id, storage_id)

    @staticmethod
    def _identity(db: sqlite3.Connection, expected_room_id: str) -> str:
        if (
            db.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID
            or db.execute("PRAGMA user_version").fetchone()[0] != _VERSION
        ):
            raise StorageUnavailable("지원하지 않는 방 DB 종류 또는 스키마 버전입니다.")
        row = db.execute(
            "SELECT r.id, m.storage_id FROM room r, metadata m WHERE r.slot=1 AND m.id=1"
        ).fetchone()
        if row is None or row[0] != expected_room_id or not isinstance(row[1], str) or not row[1]:
            raise StorageUnavailable("방 DB 식별 정보가 일치하지 않습니다.")
        return str(row[1])

    @classmethod
    async def open(cls, path: Path, expected_room_id: str) -> "SqliteRoomDatabase":
        """기존 파일의 방 ID·스키마를 검증한다. 열기만으로 진행 중 턴을 변경하지 않는다."""

        def inspect() -> tuple[Path, str]:
            try:
                resolved = path.resolve()
                with closing(sqlite3.connect(resolved.as_uri() + "?mode=rw", uri=True)) as db:
                    db.row_factory = sqlite3.Row
                    db.execute("PRAGMA foreign_keys = ON")
                    with db:
                        db.execute("BEGIN")
                        storage_id = cls._identity(db, expected_room_id)
                        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                            raise StorageUnavailable("방 DB 무결성 확인이 필요합니다.")
                        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                            raise StorageUnavailable("방 DB 참조 무결성 확인이 필요합니다.")
                        cls._read_room(db)
                        db.execute("SELECT id, room_id, revision, seq, body FROM inputs LIMIT 0")
                        db.execute(
                            "SELECT input_id, room_id, revision, state, finish_reason, has_usage, "
                            "input_tokens, output_tokens FROM turns LIMIT 0"
                        )
                        db.execute("SELECT input_id, content FROM messages LIMIT 0")
                        if db.execute("""SELECT 1 FROM inputs i LEFT JOIN turns t
                            ON t.input_id=i.id WHERE t.input_id IS NULL LIMIT 1""").fetchone():
                            raise StorageUnavailable("입력의 턴 기록이 없습니다.")
                        # 완료와 응답의 짝이 맞지 않는 기록을 정상 이력으로 숨기지 않는다.
                        if (
                            db.execute("""SELECT 1 FROM turns t LEFT JOIN messages m
                            ON m.input_id=t.input_id WHERE
                            (t.state='completed' AND m.input_id IS NULL) OR
                            (t.state!='completed' AND m.input_id IS NOT NULL) LIMIT 1""").fetchone()
                        ):
                            raise StorageUnavailable("방 응답 기록의 일관성 확인이 필요합니다.")
                        return resolved, storage_id
            except (OSError, sqlite3.Error, ValueError, TypeError) as error:
                report_failure("room_open", error)
                raise StorageUnavailable(
                    "방 DB를 열 수 없습니다. 파일·권한·손상을 확인하세요."
                ) from None

        resolved, storage_id = await settled_thread(inspect)
        return cls(resolved, expected_room_id, storage_id)

    async def _run(
        self, operation: Callable[[sqlite3.Connection], _T], *, write: bool = False
    ) -> _T:
        def execute() -> _T:
            try:
                with closing(
                    sqlite3.connect(self._path.as_uri() + "?mode=rw", uri=True, timeout=5)
                ) as db:
                    db.row_factory = sqlite3.Row
                    db.execute("PRAGMA foreign_keys = ON")
                    with db:
                        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                        if self._identity(db, self._room_id) != self._storage_id:
                            raise StorageUnavailable(
                                "방 DB가 교체되었습니다. 다시 열어 확인하세요."
                            )
                        return operation(db)
            except (OSError, sqlite3.Error, ValueError, TypeError) as error:
                report_failure("room_write" if write else "room_read", error)
                raise StorageUnavailable(
                    "방 DB 작업에 실패했습니다. 저장 결과를 확인하세요."
                ) from None

        return await settled_thread(execute)

    @staticmethod
    def _read_room(db: sqlite3.Connection) -> Room:
        row = db.execute("""SELECT v.* FROM room r JOIN revisions v
            ON v.room_id=r.id AND v.revision=r.current_revision WHERE r.slot=1""").fetchone()
        if row is None:
            raise StorageUnavailable("방 설정 revision을 찾을 수 없습니다.")
        room = Room(
            row["room_id"],
            row["name"],
            RoomContext(row["character"], row["user"], row["instructions"]),
            RoomModelConfig(row["model_id"], row["temperature"], row["max_tokens"], row["timeout"]),
            row["revision"],
        )
        validate_room(room)
        return room

    async def get(self) -> Room:
        return await self._run(self._read_room)

    async def update(self, room: Room, expected_revision: int) -> None:
        validate_room(room)
        if room.id != self._room_id or room.revision != expected_revision + 1:
            raise RevisionConflict("방 ID 또는 revision이 올바르지 않습니다.")

        def save(db: sqlite3.Connection) -> None:
            current = self._read_room(db)
            if current.revision != expected_revision:
                raise RevisionConflict("방 설정이 변경되었습니다.")
            _insert_revision(db, room)
            db.execute("UPDATE room SET current_revision=? WHERE slot=1", (room.revision,))

        await self._run(save, write=True)

    @staticmethod
    def _read_turn(db: sqlite3.Connection, input_id: str) -> RoomTurn | None:
        row = db.execute(
            """SELECT i.id, i.room_id, i.revision, i.body, t.state,
            t.finish_reason, t.has_usage, t.input_tokens, t.output_tokens, m.content
            FROM inputs i JOIN turns t ON t.input_id=i.id
            LEFT JOIN messages m ON m.input_id=i.id WHERE i.id=?""",
            (input_id,),
        ).fetchone()
        if row is None:
            return None
        result = None
        if row["state"] == TurnState.COMPLETED:
            if row["content"] is None:
                raise StorageUnavailable("완료한 턴의 응답 기록이 없습니다.")
            usage = (
                TokenUsage(row["input_tokens"], row["output_tokens"]) if row["has_usage"] else None
            )
            result = ChatResult(row["content"], row["finish_reason"], usage)
        return RoomTurn(
            StoredRoomInput(row["id"], row["room_id"], row["body"]),
            row["revision"],
            TurnState(row["state"]),
            result,
        )

    async def accept(
        self, incoming: StoredRoomInput, expected_revision: int, *, allow_new: bool = True
    ) -> tuple[RoomTurn, bool]:
        if not isinstance(incoming, StoredRoomInput):
            raise TypeError("외부 식별자가 없는 저장 입력만 받습니다.")
        if incoming.room_id != self._room_id:
            raise InputConflict("다른 방의 입력입니다.")
        if not incoming.id.strip() or not incoming.text.strip():
            raise ValueError("내부 입력 ID와 본문이 필요합니다.")

        def save(db: sqlite3.Connection) -> tuple[RoomTurn, bool]:
            room = self._read_room(db)
            if room.revision != expected_revision:
                raise RevisionConflict("방 설정이 변경되었습니다.")
            previous = self._read_turn(db, incoming.id)
            if previous is not None:
                if previous.input != incoming:
                    raise InputConflict("같은 입력 ID에 다른 내용이 있습니다.")
                return previous, False
            if not allow_new:
                raise InputConflict("접수된 입력의 방 기록이 없습니다. 복구 확인이 필요합니다.")
            db.execute(
                """INSERT INTO inputs VALUES (?, ?, ?,
                (SELECT COALESCE(MAX(seq), 0)+1 FROM inputs), ?)""",
                (incoming.id, incoming.room_id, expected_revision, incoming.text),
            )
            db.execute(
                "INSERT INTO turns(input_id, room_id, revision, state) VALUES (?, ?, ?, 'pending')",
                (incoming.id, incoming.room_id, expected_revision),
            )
            return RoomTurn(incoming, expected_revision), True

        return await self._run(save, write=True)

    async def finish(
        self, turn: RoomTurn, state: TurnState, result: ChatResult | None = None
    ) -> RoomTurn:
        if state == TurnState.PENDING or (
            state == TurnState.COMPLETED and (result is None or not result.text.strip())
        ):
            raise ValueError("완료 상태와 결과를 확인하세요.")
        if turn.input.room_id != self._room_id:
            raise InputConflict("다른 방의 결과입니다.")

        def save(db: sqlite3.Connection) -> RoomTurn:
            stored = self._read_turn(db, turn.input.id)
            if (
                stored is None
                or stored.input != turn.input
                or stored.room_revision != turn.room_revision
            ):
                raise InputConflict("접수되지 않은 결과입니다.")
            if stored.state != TurnState.PENDING:
                return stored
            final_state = (
                state
                if self._read_room(db).revision == turn.room_revision
                else TurnState.SUPERSEDED
            )
            final_result = result if final_state == TurnState.COMPLETED else None
            usage = final_result.usage if final_result else None
            db.execute(
                """UPDATE turns SET state=?, finish_reason=?, has_usage=?, input_tokens=?,
                output_tokens=? WHERE input_id=?""",
                (
                    final_state,
                    final_result.finish_reason if final_result else None,
                    int(usage is not None),
                    usage.input_tokens if usage else None,
                    usage.output_tokens if usage else None,
                    turn.input.id,
                ),
            )
            if final_result is not None:
                db.execute("INSERT INTO messages VALUES (?, ?)", (turn.input.id, final_result.text))
            return replace(stored, state=final_state, result=final_result)

        return await self._run(save, write=True)

    async def history(self) -> tuple[ChatMessage, ...]:
        def read(db: sqlite3.Connection) -> tuple[ChatMessage, ...]:
            rows = db.execute("""SELECT i.body, m.content FROM inputs i
                JOIN turns t ON t.input_id=i.id JOIN messages m ON m.input_id=i.id
                WHERE t.state='completed' ORDER BY i.seq""").fetchall()
            return tuple(
                message
                for row in rows
                for message in (ChatMessage(Role.USER, row[0]), ChatMessage(Role.ASSISTANT, row[1]))
            )

        return await self._run(read)

    async def interrupt_pending(self) -> int:
        """실행자가 없는 시작 시점에만 호출한다. 파일을 다시 여는 행위와 복구는 별개다."""

        def recover(db: sqlite3.Connection) -> int:
            return db.execute("UPDATE turns SET state='interrupted' WHERE state='pending'").rowcount

        return await self._run(recover, write=True)
