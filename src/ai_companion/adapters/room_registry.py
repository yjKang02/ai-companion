"""내부 DB v2의 단일 라이브러리 등록·수명주기 기록. 방 본문을 저장하지 않는다."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from ai_companion.adapters.library_files import FileLease, plain_path
from ai_companion.adapters.sqlite_runtime import SqliteRuntimeDatabase
from ai_companion.adapters.sqlite_support import report_failure, settled_thread
from ai_companion.application.room_ports import RevisionConflict, RoomNotFound, StorageUnavailable

_SCHEMA = (
    "CREATE TABLE library (slot INTEGER PRIMARY KEY CHECK(slot=1), id TEXT NOT NULL)",
    """CREATE TABLE room_registry (
       room_id TEXT PRIMARY KEY NOT NULL, storage_id TEXT,
       phase TEXT NOT NULL CHECK(phase IN ('creating','active','deleting','cleaning')),
       folder_device TEXT, folder_inode TEXT,
       CHECK(phase='creating' OR storage_id IS NOT NULL),
       CHECK(phase!='cleaning' OR (folder_device IS NOT NULL AND folder_inode IS NOT NULL)))""",
    "CREATE TABLE retired_rooms (room_id TEXT PRIMARY KEY NOT NULL)",
)


async def migrate_runtime_v2(path: Path, backup_path: Path) -> None:
    """실행자를 종료한 뒤 호출한다. 새 백업을 완료한 뒤 v1을 원자적으로 확장한다."""

    def migrate() -> None:
        lease = FileLease(path.absolute().with_name(path.name + ".lock"))
        try:
            plain_path(path.absolute())
            plain_path(backup_path.absolute(), missing=True)
            lease.acquire()
            source, backup = path.resolve(), backup_path.resolve()
            if source == backup:
                raise StorageUnavailable("백업 경로는 원본과 달라야 합니다.")
            with closing(sqlite3.connect(source.as_uri() + "?mode=rw", uri=True)) as db:
                db.execute("PRAGMA foreign_keys=ON")
                identity = SqliteRuntimeDatabase._identity(db)
                if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise StorageUnavailable("v1 DB에서만 명시적 마이그레이션을 실행합니다.")
                if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise StorageUnavailable("원본 DB 무결성 확인이 필요합니다.")
                if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise StorageUnavailable("원본 DB 참조 무결성 확인이 필요합니다.")
                with backup.open("xb"):
                    pass
                with closing(sqlite3.connect(backup.as_uri() + "?mode=rw", uri=True)) as copy:
                    db.backup(copy)
                    if copy.execute("PRAGMA quick_check").fetchone() != ("ok",):
                        raise StorageUnavailable("백업 검증에 실패했습니다.")
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    if (
                        SqliteRuntimeDatabase._identity(db) != identity
                        or db.execute("PRAGMA user_version").fetchone()[0] != 1
                    ):
                        raise StorageUnavailable("마이그레이션 도중 원본이 변경되었습니다.")
                    for statement in _SCHEMA:
                        db.execute(statement)
                    db.execute("PRAGMA user_version=2")
        except (OSError, sqlite3.Error) as error:
            report_failure("migrate", error)
            raise StorageUnavailable(
                "내부 DB 마이그레이션에 실패했습니다. 백업과 원본을 확인하세요."
            ) from None
        finally:
            lease.release()

    await settled_thread(migrate)


@dataclass(frozen=True, slots=True)
class RegisteredRoom:
    room_id: str
    storage_id: str | None
    phase: str
    folder_device: str | None = None
    folder_inode: str | None = None


class RoomRegistry:
    def __init__(self, runtime: SqliteRuntimeDatabase) -> None:
        self.runtime = runtime

    async def attach(self, library_id: str) -> None:
        def save(db: sqlite3.Connection) -> None:
            if db.execute("PRAGMA user_version").fetchone()[0] != 2:
                raise StorageUnavailable("등록부는 내부 DB v2가 필요합니다. 명시적으로 이전하세요.")
            row = db.execute("SELECT id FROM library WHERE slot=1").fetchone()
            if row is None:
                db.execute("INSERT INTO library VALUES (1, ?)", (library_id,))
            elif row[0] != library_id:
                raise StorageUnavailable("다른 라이브러리에 연결된 내부 DB입니다.")
            db.execute(
                "SELECT room_id, storage_id, phase, folder_device, folder_inode "
                "FROM room_registry LIMIT 0"
            )

        await self.runtime._run(save, write=True)

    async def entries(self) -> tuple[RegisteredRoom, ...]:
        def read(db: sqlite3.Connection) -> tuple[RegisteredRoom, ...]:
            return tuple(
                RegisteredRoom(*row)
                for row in db.execute(
                    "SELECT room_id, storage_id, phase, folder_device, folder_inode "
                    "FROM room_registry ORDER BY room_id"
                )
            )

        return await self.runtime._run(read)

    async def get(self, room_id: str) -> RegisteredRoom:
        def read(db: sqlite3.Connection) -> RegisteredRoom:
            row = db.execute(
                "SELECT room_id, storage_id, phase, folder_device, folder_inode "
                "FROM room_registry WHERE room_id=?",
                (room_id,),
            ).fetchone()
            if row is None:
                raise RoomNotFound("등록된 방이 없습니다.")
            return RegisteredRoom(*row)

        return await self.runtime._run(read)

    async def begin_create(self, room_id: str) -> None:
        def save(db: sqlite3.Connection) -> None:
            if db.execute("SELECT 1 FROM room_registry WHERE room_id=?", (room_id,)).fetchone():
                raise RevisionConflict("이미 등록된 방 ID입니다.")
            if db.execute("SELECT 1 FROM retired_rooms WHERE room_id=?", (room_id,)).fetchone():
                raise RevisionConflict("삭제된 방 ID는 재사용할 수 없습니다.")
            db.execute(
                "INSERT INTO room_registry(room_id, phase) VALUES (?, 'creating')", (room_id,)
            )

        await self.runtime._run(save, write=True)

    async def transition(self, previous: RegisteredRoom, updated: RegisteredRoom) -> None:
        def save(db: sqlite3.Connection) -> None:
            if previous.room_id != updated.room_id:
                raise RevisionConflict("방 ID는 변경할 수 없습니다.")
            changed = db.execute(
                """UPDATE room_registry SET storage_id=?, phase=?,
                folder_device=?, folder_inode=? WHERE room_id=? AND phase=? AND storage_id IS ?""",
                (
                    updated.storage_id,
                    updated.phase,
                    updated.folder_device,
                    updated.folder_inode,
                    previous.room_id,
                    previous.phase,
                    previous.storage_id,
                ),
            )
            if changed.rowcount != 1:
                raise RevisionConflict("방 수명주기 상태가 변경되었습니다.")

        await self.runtime._run(save, write=True)

    async def finish_delete(self, room_id: str) -> None:
        def save(db: sqlite3.Connection) -> None:
            row = db.execute(
                "SELECT phase FROM room_registry WHERE room_id=?", (room_id,)
            ).fetchone()
            if row is None or row[0] != "cleaning":
                raise RevisionConflict("삭제 정리 단계가 아닙니다.")
            db.execute("DELETE FROM room_bindings WHERE room_id=?", (room_id,))
            db.execute("DELETE FROM input_receipts WHERE room_id=?", (room_id,))
            db.execute("INSERT INTO retired_rooms VALUES (?)", (room_id,))
            db.execute("DELETE FROM room_registry WHERE room_id=?", (room_id,))

        await self.runtime._run(save, write=True)
