"""단일 개인 라이브러리의 영구 RoomStore와 단계별 생성·삭제 복구."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import TypeVar

from ai_companion.adapters.library_files import (
    FileLease,
    canonical_room_id,
    initialize_library,
    library_identity,
    plain_path,
)
from ai_companion.adapters.room_registry import RegisteredRoom, RoomRegistry
from ai_companion.adapters.sqlite_room import SqliteRoomDatabase, validate_room
from ai_companion.adapters.sqlite_runtime import SqliteRuntimeDatabase
from ai_companion.adapters.sqlite_support import report_failure, settled_thread
from ai_companion.application.room_ports import RevisionConflict, RoomNotFound, StorageUnavailable
from ai_companion.domain import ChatMessage, ChatResult, Room, RoomTurn, StoredRoomInput, TurnState

_T = TypeVar("_T")
_DB = "conversation.db"
_FILES = (_DB + "-journal", _DB + "-wal", _DB + "-shm", _DB)


async def _files(operation: Callable[[], _T]) -> _T:
    def run() -> _T:
        try:
            return operation()
        except (OSError, UnicodeError, ValueError) as error:
            report_failure("library_files", error)
            raise StorageUnavailable(
                "라이브러리 파일 작업에 실패했습니다. 상태를 확인하세요."
            ) from None

    return await settled_thread(run)


async def create_library(path: Path) -> None:
    """명시적 신규 초기화. 기존 디렉터리에 식별 파일을 덮어쓰지 않는다."""
    await _files(lambda: initialize_library(path.absolute()))


class PersistentRoomStore:
    def __init__(
        self, registry: RoomRegistry, root: Path, library_id: str, root_identity: tuple[int, int]
    ) -> None:
        self.registry = registry
        self._root = root
        self._library_id = library_id
        self._root_identity = root_identity
        self._gate = asyncio.Lock()
        self._closed = False

    async def _check(self) -> None:
        if self._closed:
            raise StorageUnavailable("종료된 방 저장소입니다.")
        if await _files(lambda: library_identity(self._root)) != self._library_id:
            raise StorageUnavailable("라이브러리 식별자가 변경되었습니다.")
        info = await _files(self._root.stat)
        if (info.st_dev, info.st_ino) != self._root_identity:
            raise StorageUnavailable("라이브러리 폴더가 교체되었습니다.")

    def _folder(self, room_id: str, prefix: str = "") -> Path:
        canonical_room_id(room_id)
        return self._root / (prefix + room_id)

    async def _database(self, entry: RegisteredRoom, folder: Path) -> SqliteRoomDatabase:
        path = folder / _DB
        await _files(lambda: plain_path(path))
        database = await SqliteRoomDatabase.open(path, entry.room_id)
        if entry.storage_id is not None and database.storage_id != entry.storage_id:
            raise StorageUnavailable("등록된 방 파일과 다른 파일입니다.")
        return database

    async def _active(self, room_id: str) -> SqliteRoomDatabase:
        entry = await self.registry.get(room_id)
        if entry.phase != "active":
            raise RoomNotFound("사용할 수 없는 방입니다. 생성·삭제 복구 상태를 확인하세요.")
        return await self._database(entry, self._folder(room_id))

    async def create(self, room: Room) -> None:
        validate_room(room)
        if room.revision != 1:
            raise RevisionConflict("새 방은 revision 1이어야 합니다.")
        async with self._gate:
            await self._check()
            staging, final = self._folder(room.id, ".creating-"), self._folder(room.id)
            if await _files(lambda: staging.exists() or final.exists()):
                raise RevisionConflict("이미 존재하는 방 경로입니다.")
            await self.registry.begin_create(room.id)
            await _files(staging.mkdir)
            database = await SqliteRoomDatabase.create(staging / _DB, room)
            previous = await self.registry.get(room.id)
            await self.registry.transition(
                previous, replace(previous, storage_id=database.storage_id)
            )
            await self._complete_create(await self.registry.get(room.id))

    async def _complete_create(self, entry: RegisteredRoom) -> None:
        staging, final = self._folder(entry.room_id, ".creating-"), self._folder(entry.room_id)
        staged, published = await _files(lambda: (staging.exists(), final.exists()))
        if staged == published:
            raise StorageUnavailable("생성 중인 방 파일을 명확히 확인할 수 없습니다.")
        database = await self._database(entry, staging if staged else final)
        if entry.storage_id is None:
            updated = replace(entry, storage_id=database.storage_id)
            await self.registry.transition(entry, updated)
            entry = updated
        if staged:

            def publish() -> None:
                plain_path(staging)
                plain_path(final, missing=True)
                if final.exists():
                    raise StorageUnavailable("게시 경로가 이미 존재합니다.")
                staging.rename(final)

            await _files(publish)
        await self.registry.transition(entry, replace(entry, phase="active"))

    async def get(self, room_id: str) -> Room:
        async with self._gate:
            await self._check()
            return await (await self._active(room_id)).get()

    async def list_rooms(self) -> tuple[Room, ...]:
        async with self._gate:
            await self._check()
            return tuple(
                [
                    await (await self._active(entry.room_id)).get()
                    for entry in await self.registry.entries()
                    if entry.phase == "active"
                ]
            )

    async def update(self, room: Room, expected_revision: int) -> None:
        async with self._gate:
            await self._check()
            await (await self._active(room.id)).update(room, expected_revision)

    async def history(self, room_id: str) -> tuple[ChatMessage, ...]:
        async with self._gate:
            await self._check()
            return await (await self._active(room_id)).history()

    async def accept(
        self, incoming: StoredRoomInput, expected_revision: int, *, allow_new: bool = True
    ) -> tuple[RoomTurn, bool]:
        async with self._gate:
            await self._check()
            return await (await self._active(incoming.room_id)).accept(
                incoming, expected_revision, allow_new=allow_new
            )

    async def finish(
        self, turn: RoomTurn, state: TurnState, result: ChatResult | None = None
    ) -> RoomTurn:
        async with self._gate:
            await self._check()
            try:
                database = await self._active(turn.input.room_id)
            except RoomNotFound:
                return replace(turn, state=TurnState.SUPERSEDED, result=None)
            return await database.finish(turn, state, result)

    async def delete(self, room_id: str, expected_revision: int) -> None:
        async with self._gate:
            await self._check()
            entry = await self.registry.get(room_id)
            if entry.phase == "active":
                database = await self._active(room_id)
                if (await database.get()).revision != expected_revision:
                    raise RevisionConflict("방 설정이 변경되었습니다.")
                updated = replace(entry, phase="deleting")
                await self.registry.transition(entry, updated)
                entry = updated
            if entry.phase not in ("deleting", "cleaning"):
                raise RevisionConflict("생성 중인 방은 삭제할 수 없습니다.")
            await self._complete_delete(entry)

    async def _complete_delete(self, entry: RegisteredRoom) -> None:
        final, trash = self._folder(entry.room_id), self._folder(entry.room_id, ".deleting-")
        if entry.phase == "deleting":
            exists, moved = await _files(lambda: (final.exists(), trash.exists()))
            if exists == moved:
                raise StorageUnavailable("삭제 대상 방 폴더를 명확히 확인할 수 없습니다.")
            await self._database(entry, final if exists else trash)

            def quarantine() -> tuple[str, str]:
                source = final if exists else trash
                plain_path(source)
                self._inspect_files(source)
                if exists:
                    plain_path(trash, missing=True)
                    if trash.exists():
                        raise StorageUnavailable("정리 폴더가 이미 존재합니다.")
                    final.rename(trash)
                info = trash.stat()
                return str(info.st_dev), str(info.st_ino)

            device, inode = await _files(quarantine)
            updated = replace(entry, phase="cleaning", folder_device=device, folder_inode=inode)
            await self.registry.transition(entry, updated)
            entry = updated
        # cleaning의 기록은 파일 ID 확인과 격리 후에만 남긴다.
        if await _files(lambda: (trash / _DB).exists()):
            await self._database(entry, trash)

        def clean() -> None:
            plain_path(trash, missing=True)
            if not trash.exists():
                return
            info = trash.stat()
            if (str(info.st_dev), str(info.st_ino)) != (entry.folder_device, entry.folder_inode):
                raise StorageUnavailable("정리 폴더가 교체되었습니다.")
            self._inspect_files(trash)
            for name in _FILES:
                target = trash / name
                # 허용 목록의 직접 자식 파일만 삭제한다. 재귀·링크 추적 없음.
                if target.exists():
                    plain_path(target)
                    target.unlink()
            trash.rmdir()

        await _files(clean)
        await self.registry.finish_delete(entry.room_id)

    @staticmethod
    def _inspect_files(folder: Path) -> None:
        for child in folder.iterdir():
            plain_path(child)
            if child.name not in _FILES or not child.is_file():
                raise StorageUnavailable("미확인 파일이 있는 방 폴더는 자동 삭제하지 않습니다.")

    async def recover(self) -> None:
        """OS 잠금을 소유하고 아직 요청을 받지 않는 조립 시점에만 호출한다."""
        async with self._gate:
            await self._check()
            for entry in await self.registry.entries():
                if entry.phase == "creating":
                    await self._complete_create(entry)
                elif entry.phase in ("deleting", "cleaning"):
                    await self._complete_delete(entry)
            for entry in await self.registry.entries():
                await (await self._active(entry.room_id)).interrupt_pending()

    async def close(self) -> None:
        async with self._gate:
            self._closed = True


@asynccontextmanager
async def persistent_room_store(
    runtime_path: Path, library_path: Path
) -> AsyncIterator[PersistentRoomStore]:
    """경로·잠금·라이브러리 식별을 확인하고 복구가 성공해야 저장소를 노출한다."""
    if ".." in runtime_path.parts or ".." in library_path.parts:
        raise StorageUnavailable("상위 이동 없는 저장소 경로가 필요합니다.")
    runtime_path, library_path = await _files(
        lambda: (runtime_path.absolute(), library_path.absolute())
    )
    leases = [
        FileLease(runtime_path.with_name(runtime_path.name + ".lock")),
        FileLease(library_path / ".library-lock"),
    ]
    store = None
    try:

        def acquire() -> tuple[str, tuple[int, int]]:
            plain_path(runtime_path)
            identity = library_identity(library_path)
            if runtime_path.is_relative_to(library_path):
                raise StorageUnavailable("내부 운영 DB는 외부 라이브러리 밖에 두어야 합니다.")
            for lease in leases:
                lease.acquire()
            info = library_path.stat()
            return identity, (info.st_dev, info.st_ino)

        library_id, root_identity = await _files(acquire)
        runtime = await SqliteRuntimeDatabase.open(runtime_path)
        registry = RoomRegistry(runtime)
        await registry.attach(library_id)
        store = PersistentRoomStore(registry, library_path, library_id, root_identity)
        await store.recover()
        yield store
    finally:
        if store is not None:
            closing = asyncio.create_task(store.close())
            cancelled = False
            while not closing.done():
                try:
                    await asyncio.shield(closing)
                except asyncio.CancelledError:
                    cancelled = True
            closing.result()
        else:
            cancelled = False
        # 취소 중에도 lease 객체는 작업 스레드 완료 후에만 해제한다.
        for lease in reversed(leases):
            lease.release()
        if cancelled:
            raise asyncio.CancelledError
