import asyncio
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest

from ai_companion import bootstrap
from ai_companion.adapters import room_registry
from ai_companion.adapters.library_files import plain_path
from ai_companion.adapters.persistent_rooms import create_library, persistent_room_store
from ai_companion.adapters.room_registry import migrate_runtime_v2
from ai_companion.adapters.sqlite_room import SqliteRoomDatabase
from ai_companion.adapters.sqlite_runtime import SqliteRuntimeDatabase
from ai_companion.application.room_ports import (
    InputConflict,
    RevisionConflict,
    RoomNotFound,
    StorageUnavailable,
)
from ai_companion.application.rooms import RoomService
from ai_companion.domain import (
    ChatResult,
    ModelConnection,
    Room,
    RoomContext,
    RoomInput,
    RoomModelConfig,
    StoredRoomInput,
    TurnState,
)


@pytest.fixture
async def paths(tmp_path):
    internal, library = tmp_path / "system.db", tmp_path / "library"
    await SqliteRuntimeDatabase.create(internal)
    await migrate_runtime_v2(internal, tmp_path / "before-v2.db")
    await create_library(library)
    return internal, library


def content():
    return Room(str(uuid4()), "방", RoomContext("개인 캐릭터"), RoomModelConfig("model"))


def service_for(store, executor=None):
    runtime = store.registry.runtime
    if executor is None:
        executor = Mock()
        executor.generate = AsyncMock(return_value=ChatResult("대답"))
    return RoomService(store, runtime.connections, executor, runtime.bindings, runtime.receipts)


async def test_service_reopen_keeps_room_context_connection_and_duplicate_result(paths):
    internal, library = paths
    async with persistent_room_store(*paths) as store:
        runtime = store.registry.runtime
        await runtime.connections.register(
            ModelConnection("local", "lmstudio", "http://localhost:1234/v1")
        )
        service = service_for(store)
        room = await service.create("방", RoomContext("친구"), RoomModelConfig("model"))
        await service.bind(room.id, 1, 0, "local")
        command = RoomInput(room.id, "source", "event", "질문")
        result = await service.respond(command)
        assert len(await service.history(room.id)) == 2
    async with persistent_room_store(*paths) as store:
        executor = Mock()
        executor.generate = AsyncMock(side_effect=AssertionError("중복 실행"))
        service = service_for(store, executor)
        assert await service.get(room.id) == room
        assert await service.respond(command) == result
        assert (await service.binding(room.id)).connection_id == "local"
        assert await store.list_rooms() == (room,)
        assert await service.list_rooms() == (room,)
        executor.generate.assert_not_awaited()
    assert (library / room.id / "conversation.db").is_file()
    with closing(sqlite3.connect(internal)) as db:
        dump = "\n".join(db.iterdump())
    assert "친구" not in dump and "질문" not in dump and "대답" not in dump


async def test_restart_interrupts_pending_without_reexecution(paths):
    async with persistent_room_store(*paths) as store:
        room = content()
        await store.create(room)
        receipt = await store.registry.runtime.receipts.reserve(room.id, "source", "event")
        pending, _ = await store.accept(StoredRoomInput(receipt.input_id, room.id, "질문"), 1)
        await store.registry.runtime.receipts.mark_accepted(receipt)
    async with persistent_room_store(*paths) as store:
        interrupted = await service_for(store).respond(
            RoomInput(room.id, "source", "event", "질문")
        )
        assert interrupted.state == TurnState.INTERRUPTED
        assert await store.finish(pending, TurnState.COMPLETED, ChatResult("늦음")) == interrupted
        assert await store.history(room.id) == ()


async def test_delete_removes_owned_files_and_metadata_only_and_prevents_id_reuse(paths):
    internal, library = paths
    async with persistent_room_store(*paths) as store:
        first, second = content(), content()
        await store.create(first)
        await store.create(second)
        runtime = store.registry.runtime
        await runtime.connections.register(
            ModelConnection("local", "lmstudio", "http://localhost:1234/v1")
        )
        service = service_for(store)
        await service.bind(first.id, 1, 0, "local")
        await service.bind(second.id, 1, 0, "local")
        old = await service.respond(RoomInput(first.id, "source", "event", "질문"))
        await service.delete(first.id, 1)
        assert not (library / first.id).exists()
        assert not (library / (".deleting-" + first.id)).exists()
        assert await service.get(second.id) == second
        assert (await service.binding(second.id)).connection_id == "local"
        with pytest.raises(InputConflict):
            await runtime.receipts.mark_accepted(
                replace(await runtime.receipts.reserve(second.id, "other", "e"), room_id=first.id)
            )
        with pytest.raises(RevisionConflict):
            await store.create(first)
        assert (
            await store.finish(old, TurnState.COMPLETED, ChatResult("늦음"))
        ).state == TurnState.SUPERSEDED
    async with persistent_room_store(*paths) as store:
        assert await store.list_rooms() == (second,)
        with pytest.raises(RoomNotFound):
            await store.get(first.id)
    with closing(sqlite3.connect(internal)) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM input_receipts WHERE room_id=?", (first.id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT count(*) FROM room_bindings WHERE room_id=?", (first.id,)
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("phase", ["deleting", "cleaning", "cleanup"])
async def test_interrupted_deletion_recovers_on_reopen(paths, monkeypatch, phase):
    _, library = paths
    async with persistent_room_store(*paths) as store:
        room = content()
        await store.create(room)
        original = store.registry.transition

        async def transition(previous, updated):
            await original(previous, updated)
            if updated.phase == phase:
                raise OSError("접수된 단계 이후 종료")

        monkeypatch.setattr(store.registry, "transition", transition)
        if phase == "cleanup":
            monkeypatch.setattr(
                store.registry, "finish_delete", AsyncMock(side_effect=OSError("내부 정리 실패"))
            )
        with pytest.raises(OSError):
            await store.delete(room.id, 1)
        with pytest.raises(RoomNotFound):
            await store.get(room.id)
    async with persistent_room_store(*paths) as store:
        assert await store.list_rooms() == ()
        assert await store.registry.entries() == ()
    assert not (library / room.id).exists()
    assert not (library / (".deleting-" + room.id)).exists()


@pytest.mark.parametrize("after_publish", [False, True])
async def test_completed_file_creation_recovers_before_or_after_publish(
    paths, monkeypatch, after_publish
):
    async with persistent_room_store(*paths) as store:
        room = content()
        original = store.registry.transition

        async def fail(previous, updated):
            if after_publish and updated.phase == "active":
                raise OSError("게시 이후 종료")
            await original(previous, updated)
            if not after_publish and updated.phase == "creating":
                raise OSError("파일 ID 기록 이후 종료")

        monkeypatch.setattr(store.registry, "transition", fail)
        with pytest.raises(OSError):
            await store.create(room)
    async with persistent_room_store(*paths) as store:
        assert await store.get(room.id) == room
        assert (await store.registry.get(room.id)).phase == "active"


async def test_partial_unrecoverable_creation_fails_closed_preserving_file(paths, monkeypatch):
    _, library = paths
    room = content()
    original = SqliteRoomDatabase.create

    async def fail(path, room):
        path.touch()
        raise StorageUnavailable("초기화 중 종료")

    async with persistent_room_store(*paths) as store:
        monkeypatch.setattr(SqliteRoomDatabase, "create", fail)
        with pytest.raises(StorageUnavailable):
            await store.create(room)
    monkeypatch.setattr(SqliteRoomDatabase, "create", original)
    with pytest.raises(StorageUnavailable):
        async with persistent_room_store(*paths):
            pytest.fail("불완전 라이브러리 실행")
    assert (library / (".creating-" + room.id) / "conversation.db").exists()


async def test_delete_failure_does_not_remove_unknown_files_and_can_resume(paths):
    _, library = paths
    async with persistent_room_store(*paths) as store:
        room = content()
        await store.create(room)
        extra = library / room.id / "user-notes.txt"
        extra.write_text("보존", encoding="utf-8")
        with pytest.raises(StorageUnavailable):
            await store.delete(room.id, 1)
        assert extra.read_text(encoding="utf-8") == "보존"
        assert (library / room.id / "conversation.db").exists()
        extra.rename(library / "saved-notes.txt")
    async with persistent_room_store(*paths) as store:
        assert await store.list_rooms() == ()
    assert (library / "saved-notes.txt").read_text(encoding="utf-8") == "보존"


async def test_file_unlink_failure_keeps_cleanup_job_for_retry(paths, monkeypatch):
    _, library = paths
    room = content()
    unlink = Path.unlink

    def blocked(path, *args, **kwargs):
        if path.name == "conversation.db":
            raise PermissionError("locked")
        return unlink(path, *args, **kwargs)

    async with persistent_room_store(*paths) as store:
        await store.create(room)
        monkeypatch.setattr(Path, "unlink", blocked)
        with pytest.raises(StorageUnavailable):
            await store.delete(room.id, 1)
        assert (await store.registry.get(room.id)).phase == "cleaning"
    monkeypatch.setattr(Path, "unlink", unlink)
    async with persistent_room_store(*paths) as store:
        assert await store.list_rooms() == ()
    assert not (library / (".deleting-" + room.id)).exists()


async def test_missing_or_replaced_active_file_never_auto_created(paths):
    _, library = paths
    async with persistent_room_store(*paths) as store:
        room = content()
        await store.create(room)
    path = library / room.id / "conversation.db"
    path.rename(library / "saved.db")
    with pytest.raises(StorageUnavailable):
        async with persistent_room_store(*paths):
            pytest.fail("누락 파일 재생성")
    assert not path.exists()
    await SqliteRoomDatabase.create(path, room)
    with pytest.raises(StorageUnavailable, match="다른 파일"):
        async with persistent_room_store(*paths):
            pytest.fail("교체 파일 수용")


async def test_stale_revision_invalid_create_and_path_traversal_leave_state_unchanged(paths):
    async with persistent_room_store(*paths) as store:
        room = content()
        with pytest.raises(ValueError):
            await store.create(replace(room, context=RoomContext("")))
        with pytest.raises(StorageUnavailable):
            await store.create(replace(room, id="../outside"))
        assert await store.registry.entries() == ()
        await store.create(room)
        with pytest.raises(RevisionConflict):
            await store.delete(room.id, 2)
        assert await store.get(room.id) == room


async def test_live_model_reply_cannot_resurrect_deleted_room(paths):
    entered, release = asyncio.Event(), asyncio.Event()

    async def generating(*args):
        entered.set()
        await release.wait()
        return ChatResult("늦은 응답")

    executor = Mock()
    executor.generate = AsyncMock(side_effect=generating)
    async with persistent_room_store(*paths) as store:
        await store.registry.runtime.connections.register(
            ModelConnection("local", "lmstudio", "http://localhost:1234/v1")
        )
        service = service_for(store, executor)
        room = await service.create("방", RoomContext("친구"), RoomModelConfig("model"))
        await service.bind(room.id, 1, 0, "local")
        task = asyncio.create_task(service.respond(RoomInput(room.id, "source", "event", "질문")))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await service.delete(room.id, 1)
        finally:
            release.set()
        assert (await task).state == TurnState.SUPERSEDED
        assert await store.list_rooms() == ()


async def test_only_one_owner_even_from_another_process(paths):
    script = """
import asyncio, sys
from pathlib import Path
from ai_companion.adapters.persistent_rooms import persistent_room_store
from ai_companion.application.room_ports import StorageUnavailable
async def check():
    try:
        async with persistent_room_store(Path(sys.argv[1]), Path(sys.argv[2])):
            raise AssertionError('second owner')
    except StorageUnavailable:
        pass
asyncio.run(check())
"""
    async with persistent_room_store(*paths):
        with pytest.raises(StorageUnavailable):
            async with persistent_room_store(*paths):
                pytest.fail("중복 실행")
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-c", script, *map(str, paths)],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
    async with persistent_room_store(*paths) as store:
        assert await store.list_rooms() == ()


async def test_other_library_missing_library_and_closed_store_rejected(paths, tmp_path):
    internal, library = paths
    async with persistent_room_store(*paths) as store:
        pass
    with pytest.raises(StorageUnavailable):
        await store.list_rooms()
    other = tmp_path / "other"
    await create_library(other)
    with pytest.raises(StorageUnavailable):
        async with persistent_room_store(internal, other):
            pytest.fail("다른 라이브러리 수용")
    with pytest.raises(StorageUnavailable):
        async with persistent_room_store(internal, tmp_path / "missing"):
            pytest.fail("누락 라이브러리 생성")
    with pytest.raises(StorageUnavailable):
        await create_library(library)


async def test_bootstrap_assembles_durable_service_and_closes_http_client(paths, monkeypatch):
    runtime = await SqliteRuntimeDatabase.open(paths[0])
    await runtime.connections.register(
        ModelConnection("local", "lmstudio", "http://localhost:1234/v1")
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"choices": [{"message": {"role": "assistant", "content": "응답"}}]}
            )
        )
    )
    monkeypatch.setattr(bootstrap.httpx, "AsyncClient", lambda **kwargs: client)
    secrets = Mock()
    secrets.get = AsyncMock(side_effect=AssertionError("비밀 불필요"))
    async with bootstrap.persistent_room_backend(*paths, secrets) as service:
        room = await service.create("방", RoomContext("친구"), RoomModelConfig("model"))
        await service.bind(room.id, 1, 0, "local")
        assert (
            await service.respond(RoomInput(room.id, "source", "event", "질문"))
        ).result.text == "응답"
    assert client.is_closed
    async with persistent_room_store(*paths) as store:
        assert len(await store.history(room.id)) == 2


async def test_migration_preserves_records_and_backup_and_never_overwrites(tmp_path):
    path, backup = tmp_path / "v1.db", tmp_path / "backup.db"
    runtime = await SqliteRuntimeDatabase.create(path)
    connection = ModelConnection("local", "lmstudio", "http://localhost:1234/v1")
    await runtime.connections.register(connection)
    binding = await runtime.bindings.set("legacy-room", "local", 0)
    receipt = await runtime.receipts.reserve("legacy-room", "source", "event")
    await migrate_runtime_v2(path, backup)
    for database in (
        await SqliteRuntimeDatabase.open(path),
        await SqliteRuntimeDatabase.open(backup),
    ):
        assert await database.connections.get("local") == connection
        assert await database.bindings.get("legacy-room") == binding
        assert await database.receipts.reserve("legacy-room", "source", "event") == receipt
    before = backup.read_bytes()
    with pytest.raises(StorageUnavailable):
        await migrate_runtime_v2(path, backup)
    assert backup.read_bytes() == before


async def test_migration_schema_failure_rolls_back_and_v1_needs_explicit_upgrade(
    tmp_path, monkeypatch
):
    path, backup, library = tmp_path / "system.db", tmp_path / "backup.db", tmp_path / "library"
    await SqliteRuntimeDatabase.create(path)
    await create_library(library)
    with pytest.raises(StorageUnavailable):
        async with persistent_room_store(path, library):
            pytest.fail("암묵적 이전")
    monkeypatch.setattr(room_registry, "_SCHEMA", (*room_registry._SCHEMA, "invalid SQL"))
    with pytest.raises(StorageUnavailable):
        await migrate_runtime_v2(path, backup)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name='room_registry'").fetchone()
            is None
        )
    assert backup.exists()


async def test_migration_rejects_broken_foreign_key_before_creating_backup(tmp_path):
    path, backup = tmp_path / "v1.db", tmp_path / "backup.db"
    await SqliteRuntimeDatabase.create(path)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "INSERT INTO room_bindings(room_id, connection_id, generation) VALUES (?, ?, ?)",
            ("legacy-room", "missing-connection", 1),
        )
    with pytest.raises(StorageUnavailable):
        await migrate_runtime_v2(path, backup)
    assert not backup.exists()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT connection_id FROM room_bindings").fetchone() == (
            "missing-connection",
        )


async def test_process_exit_during_delete_is_recovered_and_releases_leases(paths):
    async with persistent_room_store(*paths) as store:
        room = content()
        await store.create(room)
    script = """
import asyncio, os, sys
from pathlib import Path
from ai_companion.adapters.persistent_rooms import persistent_room_store
async def interrupt():
    async with persistent_room_store(Path(sys.argv[1]), Path(sys.argv[2])) as store:
        original = store.registry.transition
        async def transition(previous, updated):
            await original(previous, updated)
            if updated.phase == 'cleaning':
                os._exit(0)
        store.registry.transition = transition
        await store.delete(sys.argv[3], 1)
        raise AssertionError('exit not reached')
asyncio.run(interrupt())
"""
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", script, *map(str, paths), room.id],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    async with persistent_room_store(*paths) as store:
        assert await store.list_rooms() == ()
        with pytest.raises(RoomNotFound):
            await store.get(room.id)


async def test_cancelled_creation_recovers_and_never_leaves_a_process_lock(paths, monkeypatch):
    room = content()
    async with persistent_room_store(*paths) as store:
        original = store.registry.transition

        async def cancel_after_commit(previous, updated):
            await original(previous, updated)
            raise asyncio.CancelledError

        monkeypatch.setattr(store.registry, "transition", cancel_after_commit)
        with pytest.raises(asyncio.CancelledError):
            await store.create(room)
    async with persistent_room_store(*paths) as store:
        assert await store.get(room.id) == room


async def test_migration_refuses_live_owner_and_keeps_existing_backup(paths, tmp_path):
    async with persistent_room_store(*paths):
        with pytest.raises(StorageUnavailable):
            await migrate_runtime_v2(paths[0], tmp_path / "new-backup.db")
    assert not (tmp_path / "new-backup.db").exists()
    v1 = tmp_path / "v1.db"
    await SqliteRuntimeDatabase.create(v1)
    backup = tmp_path / "existing.db"
    backup.write_bytes(b"do not change")
    with pytest.raises(StorageUnavailable):
        await migrate_runtime_v2(v1, backup)
    assert backup.read_bytes() == b"do not change"
    with closing(sqlite3.connect(v1)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1


async def test_library_lease_blocks_another_internal_database(paths, tmp_path):
    second = tmp_path / "second.db"
    await SqliteRuntimeDatabase.create(second)
    await migrate_runtime_v2(second, tmp_path / "second-backup.db")
    async with persistent_room_store(*paths):
        with pytest.raises(StorageUnavailable):
            async with persistent_room_store(second, paths[1]):
                pytest.fail("다른 내부 DB로 같은 라이브러리 실행")


async def test_hardlinked_database_is_never_deleted(paths, tmp_path):
    async with persistent_room_store(*paths) as store:
        room = content()
        await store.create(room)
        original = paths[1] / room.id / "conversation.db"
        alias = tmp_path / "shared.db"
        os.link(original, alias)
        with pytest.raises(StorageUnavailable):
            await store.delete(room.id, 1)
        assert original.exists() and alias.exists()
        assert (await store.registry.get(room.id)).phase == "active"


def test_reparse_component_is_rejected_without_following_it(tmp_path, monkeypatch):
    target = tmp_path / "junction"
    target.mkdir()
    lstat = Path.lstat

    def fake_lstat(path, *args, **kwargs):
        if path == target:
            from types import SimpleNamespace

            return SimpleNamespace(st_mode=0o40755, st_file_attributes=0x400, st_nlink=1)
        return lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    with pytest.raises(StorageUnavailable):
        plain_path(target / "conversation.db", missing=True)


async def test_internal_database_cannot_be_placed_inside_library(tmp_path):
    library = tmp_path / "library"
    await create_library(library)
    internal = library / "system.db"
    await SqliteRuntimeDatabase.create(internal)
    await migrate_runtime_v2(internal, tmp_path / "backup.db")
    with pytest.raises(StorageUnavailable):
        async with persistent_room_store(internal, library):
            pytest.fail("내부 운영 DB가 공유 라이브러리에 포함됨")
