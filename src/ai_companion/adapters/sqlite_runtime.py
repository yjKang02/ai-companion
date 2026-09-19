"""설치 내부 운영 데이터의 SQLite 저장. 방 본문·키 원문은 저장하지 않는다."""

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import TypeVar
from uuid import uuid4

from ai_companion.application.room_ports import (
    ConnectionUnavailable,
    InputConflict,
    RevisionConflict,
    StorageUnavailable,
)
from ai_companion.domain import InputReceipt, ModelConnection, RoomRuntimeBinding

_T = TypeVar("_T")
_log = logging.getLogger(__name__)
_APPLICATION_ID = 0x41494352
_VERSION = 1
_SCHEMA = (
    "CREATE TABLE metadata (id INTEGER PRIMARY KEY CHECK(id = 1), storage_id TEXT NOT NULL)",
    """CREATE TABLE model_connections (
        id TEXT PRIMARY KEY NOT NULL, provider TEXT NOT NULL, base_url TEXT NOT NULL,
        secret_ref TEXT, enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
        revision INTEGER NOT NULL CHECK(revision >= 1))""",
    """CREATE TABLE room_bindings (
        room_id TEXT PRIMARY KEY NOT NULL,
        connection_id TEXT REFERENCES model_connections(id),
        generation INTEGER NOT NULL CHECK(generation >= 1))""",
    """CREATE TABLE input_receipts (
        source TEXT NOT NULL, request_id TEXT NOT NULL, room_id TEXT NOT NULL,
        input_id TEXT NOT NULL UNIQUE, accepted INTEGER NOT NULL CHECK(accepted IN (0, 1)),
        PRIMARY KEY(source, request_id))""",
    "CREATE INDEX receipts_by_room ON input_receipts(room_id)",
)


def _report_failure(operation: str, error: BaseException) -> None:
    """진단 분류·숫자 코드만 기록한다. 예외 원문·경로·traceback은 로그에도 넣지 않는다."""
    if isinstance(error, sqlite3.Error):
        category, code = "sqlite", getattr(error, "sqlite_errorcode", None)
    elif isinstance(error, OSError):
        category, code = "os", error.errno
    else:
        category, code = "other", None
    _log.warning(
        "storage_failure operation=%s category=%s code=%s",
        operation,
        category,
        code if type(code) is int else 0,
    )


async def _settled_thread(operation: Callable[[], _T]) -> _T:
    """취소돼도 작업 스레드가 끝난 뒤 취소를 전파해 호출자의 변경 잠금을 유지한다."""
    task = asyncio.create_task(asyncio.to_thread(operation))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    if cancelled:
        # 취소 중 발생한 예외를 회수하되 저장 성공으로 반환하지 않는다.
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                _report_failure("cancelled", error)
        raise asyncio.CancelledError
    return task.result()


class SqliteRuntimeDatabase:
    """단일 라이브러리의 내부 DB. create/open을 명시적으로 구분하며 자동 복구하지 않는다."""

    def __init__(self, path: Path, storage_id: str) -> None:
        self._path = path
        self._storage_id = storage_id
        self.connections = SqliteModelConnections(self)
        self.bindings = SqliteRoomBindings(self)
        self.receipts = SqliteInputReceipts(self)

    @classmethod
    async def create(cls, path: Path) -> "SqliteRuntimeDatabase":
        """기존 파일을 덮어쓰지 않고 v1 DB를 만든다. 부모 폴더는 호출자가 준비한다."""

        def initialize() -> tuple[Path, str]:
            try:
                resolved = path.resolve()
                with resolved.open("xb"):
                    pass
                storage_id = str(uuid4())
                with closing(sqlite3.connect(resolved.as_uri() + "?mode=rw", uri=True)) as db:
                    with db:
                        db.execute("BEGIN IMMEDIATE")
                        for statement in _SCHEMA:
                            db.execute(statement)
                        db.execute("INSERT INTO metadata VALUES (1, ?)", (storage_id,))
                        db.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
                        db.execute(f"PRAGMA user_version = {_VERSION}")
                return resolved, storage_id
            except (OSError, sqlite3.Error) as error:
                _report_failure("create", error)
                raise StorageUnavailable(
                    "내부 DB를 생성하지 못했습니다. 기존 파일은 덮어쓰지 않습니다."
                ) from None

        resolved, storage_id = await _settled_thread(initialize)
        return cls(resolved, storage_id)

    @classmethod
    async def open(cls, path: Path) -> "SqliteRuntimeDatabase":
        """기존 DB만 연다. 누락·다른 종류·미지원 버전·손상은 새 파일 생성 없이 거부한다."""

        def inspect() -> tuple[Path, str]:
            try:
                resolved = path.resolve()
                with closing(sqlite3.connect(resolved.as_uri() + "?mode=rw", uri=True)) as db:
                    db.execute("PRAGMA foreign_keys = ON")
                    with db:
                        db.execute("BEGIN")
                        storage_id = cls._identity(db)
                        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                            raise StorageUnavailable("내부 DB 무결성 확인이 필요합니다.")
                        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                            raise StorageUnavailable("내부 DB 참조 무결성 확인이 필요합니다.")
                        # v1 필수 테이블·필드가 없으면 사용할 수 없다.
                        db.execute(
                            "SELECT id, provider, base_url, secret_ref, enabled, revision "
                            "FROM model_connections LIMIT 0"
                        )
                        db.execute(
                            "SELECT room_id, connection_id, generation FROM room_bindings LIMIT 0"
                        )
                        db.execute(
                            "SELECT source, request_id, room_id, input_id, accepted "
                            "FROM input_receipts LIMIT 0"
                        )
                        return resolved, storage_id
            except (OSError, sqlite3.Error) as error:
                _report_failure("open", error)
                raise StorageUnavailable(
                    "내부 DB를 열 수 없습니다. 파일·권한·손상을 확인하세요."
                ) from None

        resolved, storage_id = await _settled_thread(inspect)
        return cls(resolved, storage_id)

    @staticmethod
    def _identity(db: sqlite3.Connection) -> str:
        if (
            db.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID
            or db.execute("PRAGMA user_version").fetchone()[0] != _VERSION
        ):
            raise StorageUnavailable("지원하지 않는 내부 DB 종류 또는 스키마 버전입니다.")
        row = db.execute("SELECT storage_id FROM metadata WHERE id = 1").fetchone()
        if row is None or not isinstance(row[0], str) or not row[0]:
            raise StorageUnavailable("내부 DB 식별 정보가 올바르지 않습니다.")
        return str(row[0])

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
                        if self._identity(db) != self._storage_id:
                            raise StorageUnavailable(
                                "내부 DB가 교체되었습니다. 다시 열어 확인하세요."
                            )
                        return operation(db)
            except (OSError, sqlite3.Error) as error:
                _report_failure("write" if write else "read", error)
                raise StorageUnavailable(
                    "내부 DB 작업에 실패했습니다. 저장 결과를 확인하세요."
                ) from None

        return await _settled_thread(execute)


class SqliteModelConnections:
    def __init__(self, database: SqliteRuntimeDatabase) -> None:
        self._database = database

    async def register(self, connection: ModelConnection) -> None:
        if not connection.id.strip() or connection.revision != 1:
            raise RevisionConflict("연결 ID 또는 초기 revision이 올바르지 않습니다.")

        def save(db: sqlite3.Connection) -> None:
            if db.execute(
                "SELECT 1 FROM model_connections WHERE id = ?", (connection.id,)
            ).fetchone():
                raise RevisionConflict("이미 등록된 모델 연결입니다.")
            db.execute(
                "INSERT INTO model_connections VALUES (?, ?, ?, ?, ?, ?)",
                (
                    connection.id,
                    connection.provider,
                    connection.base_url,
                    connection.secret_ref,
                    int(connection.enabled),
                    connection.revision,
                ),
            )

        await self._database._run(save, write=True)

    async def update(self, connection: ModelConnection, expected_revision: int) -> None:
        if connection.revision != expected_revision + 1:
            raise RevisionConflict("연결 revision이 올바르지 않습니다.")

        def save(db: sqlite3.Connection) -> None:
            changed = db.execute(
                """UPDATE model_connections SET provider = ?, base_url = ?, secret_ref = ?,
                   enabled = ?, revision = ? WHERE id = ? AND revision = ?""",
                (
                    connection.provider,
                    connection.base_url,
                    connection.secret_ref,
                    int(connection.enabled),
                    connection.revision,
                    connection.id,
                    expected_revision,
                ),
            )
            if changed.rowcount != 1:
                raise RevisionConflict("연결이 변경되었습니다.")

        await self._database._run(save, write=True)

    async def get(self, connection_id: str) -> ModelConnection:
        def read(db: sqlite3.Connection) -> ModelConnection:
            row = db.execute(
                "SELECT * FROM model_connections WHERE id = ?", (connection_id,)
            ).fetchone()
            if row is None:
                raise ConnectionUnavailable("등록된 모델 연결을 찾을 수 없습니다.")
            return ModelConnection(
                row["id"],
                row["provider"],
                row["base_url"],
                row["secret_ref"],
                bool(row["enabled"]),
                row["revision"],
            )

        return await self._database._run(read)


class SqliteRoomBindings:
    def __init__(self, database: SqliteRuntimeDatabase) -> None:
        self._database = database

    @staticmethod
    def _read(db: sqlite3.Connection, room_id: str) -> RoomRuntimeBinding:
        row = db.execute("SELECT * FROM room_bindings WHERE room_id = ?", (room_id,)).fetchone()
        if row is None:
            return RoomRuntimeBinding(room_id)
        return RoomRuntimeBinding(room_id, row["connection_id"], row["generation"])

    async def get(self, room_id: str) -> RoomRuntimeBinding:
        return await self._database._run(lambda db: self._read(db, room_id))

    async def set(
        self, room_id: str, connection_id: str | None, expected_generation: int
    ) -> RoomRuntimeBinding:
        def save(db: sqlite3.Connection) -> RoomRuntimeBinding:
            current = self._read(db, room_id)
            if current.generation != expected_generation:
                raise RevisionConflict("방의 실행 연결이 변경되었습니다.")
            if (
                connection_id is not None
                and db.execute(
                    "SELECT 1 FROM model_connections WHERE id = ?", (connection_id,)
                ).fetchone()
                is None
            ):
                raise ConnectionUnavailable("등록된 모델 연결을 찾을 수 없습니다.")
            binding = RoomRuntimeBinding(room_id, connection_id, current.generation + 1)
            db.execute(
                """INSERT INTO room_bindings VALUES (?, ?, ?)
                   ON CONFLICT(room_id) DO UPDATE SET connection_id = excluded.connection_id,
                   generation = excluded.generation""",
                (room_id, connection_id, binding.generation),
            )
            return binding

        return await self._database._run(save, write=True)

    async def delete(self, room_id: str) -> None:
        def remove(db: sqlite3.Connection) -> None:
            db.execute("DELETE FROM room_bindings WHERE room_id = ?", (room_id,))

        await self._database._run(remove, write=True)


class SqliteInputReceipts:
    def __init__(self, database: SqliteRuntimeDatabase) -> None:
        self._database = database

    async def reserve(self, room_id: str, source: str, request_id: str) -> InputReceipt:
        if not room_id.strip() or not source.strip() or not request_id.strip():
            raise ValueError("방과 입력 식별자가 필요합니다.")

        def save(db: sqlite3.Connection) -> InputReceipt:
            row = db.execute(
                "SELECT * FROM input_receipts WHERE source = ? AND request_id = ?",
                (source, request_id),
            ).fetchone()
            if row is not None:
                if row["room_id"] != room_id:
                    raise InputConflict("이미 다른 방에 배정된 입력입니다.")
                return InputReceipt(
                    row["input_id"], room_id, source, request_id, bool(row["accepted"])
                )
            receipt = InputReceipt(str(uuid4()), room_id, source, request_id)
            db.execute(
                "INSERT INTO input_receipts VALUES (?, ?, ?, ?, 0)",
                (source, request_id, room_id, receipt.input_id),
            )
            return receipt

        return await self._database._run(save, write=True)

    async def mark_accepted(self, receipt: InputReceipt) -> None:
        def save(db: sqlite3.Connection) -> None:
            changed = db.execute(
                """UPDATE input_receipts SET accepted = 1
                   WHERE source = ? AND request_id = ? AND room_id = ? AND input_id = ?""",
                (receipt.source, receipt.request_id, receipt.room_id, receipt.input_id),
            )
            if changed.rowcount != 1:
                raise InputConflict("예약되지 않은 입력입니다.")

        await self._database._run(save, write=True)

    async def delete(self, room_id: str) -> None:
        def remove(db: sqlite3.Connection) -> None:
            db.execute("DELETE FROM input_receipts WHERE room_id = ?", (room_id,))

        await self._database._run(remove, write=True)
