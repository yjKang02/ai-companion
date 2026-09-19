import asyncio
import sqlite3
import threading
from contextlib import closing
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest

from ai_companion.adapters import sqlite_runtime
from ai_companion.adapters.room_memory import InMemoryRoomStore
from ai_companion.adapters.sqlite_runtime import SqliteRuntimeDatabase
from ai_companion.application.room_ports import (
    ConnectionUnavailable,
    InputConflict,
    RevisionConflict,
    StorageUnavailable,
)
from ai_companion.application.rooms import RoomService
from ai_companion.domain import ChatResult, ModelConnection, RoomContext, RoomInput, RoomModelConfig


def connection(identifier="local"):
    return ModelConnection(identifier, "lmstudio", "http://localhost:1234/v1", "key-reference")


async def test_reopen_preserves_connections_bindings_and_receipts(tmp_path):
    path = tmp_path / "개인 설정 #1.db"
    database = await SqliteRuntimeDatabase.create(path)
    await database.connections.register(connection())
    updated = replace(connection(), enabled=False, revision=2)
    await database.connections.update(updated, 1)
    binding = await database.bindings.set("room", "local", 0)
    receipt = await database.receipts.reserve("room", "telegram/user", "event")
    await database.receipts.mark_accepted(receipt)
    reopened = await SqliteRuntimeDatabase.open(path)
    assert await reopened.connections.get("local") == updated
    assert await reopened.bindings.get("room") == binding
    assert await reopened.receipts.reserve("room", "telegram/user", "event") == replace(
        receipt, accepted=True
    )


async def test_create_never_overwrites_and_open_never_creates(tmp_path):
    path = tmp_path / "system.db"
    with pytest.raises(StorageUnavailable):
        await SqliteRuntimeDatabase.open(path)
    assert not path.exists()
    database = await SqliteRuntimeDatabase.create(path)
    await database.connections.register(connection())
    before = path.read_bytes()
    with pytest.raises(StorageUnavailable):
        await SqliteRuntimeDatabase.create(path)
    assert path.read_bytes() == before
    with pytest.raises(StorageUnavailable):
        await SqliteRuntimeDatabase.create(tmp_path / "missing" / "system.db")
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize(
    "kind", ["empty", "garbage", "unrelated", "version", "identity", "table", "foreign-key"]
)
async def test_invalid_database_is_rejected_without_rewriting(tmp_path, kind):
    path = tmp_path / "private-path.db"
    if kind == "empty":
        path.touch()
    elif kind == "garbage":
        path.write_bytes(b"private invalid content")
    elif kind == "unrelated":
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE unrelated (id INTEGER)")
    else:
        await SqliteRuntimeDatabase.create(path)
        with closing(sqlite3.connect(path)) as db, db:
            if kind == "version":
                db.execute("PRAGMA user_version = 99")
            elif kind == "identity":
                db.execute("DELETE FROM metadata")
            elif kind == "table":
                db.execute("DROP TABLE input_receipts")
            else:
                db.execute("INSERT INTO room_bindings VALUES ('room', 'missing', 1)")
    before = path.read_bytes()
    with pytest.raises(StorageUnavailable) as error:
        await SqliteRuntimeDatabase.open(path)
    assert path.read_bytes() == before
    assert str(path) not in str(error.value)
    assert "private" not in str(error.value)


async def test_open_handle_rejects_replaced_or_missing_database(tmp_path):
    path = tmp_path / "system.db"
    database = await SqliteRuntimeDatabase.create(path)
    path.rename(tmp_path / "saved.db")
    with pytest.raises(StorageUnavailable):
        await database.connections.get("local")
    assert not path.exists()
    other = await SqliteRuntimeDatabase.create(path)
    await other.connections.register(connection())
    with pytest.raises(StorageUnavailable, match="교체"):
        await database.connections.get("local")
    assert await other.connections.get("local") == connection()


async def test_schema_version_change_after_open_prevents_writes(tmp_path):
    path = tmp_path / "system.db"
    database = await SqliteRuntimeDatabase.create(path)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA user_version = 99")
    with pytest.raises(StorageUnavailable):
        await database.connections.register(connection())
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 99
        assert db.execute("SELECT count(*) FROM model_connections").fetchone()[0] == 0


async def test_external_identifiers_are_bound_as_data_not_sql(tmp_path):
    path = tmp_path / "system.db"
    database = await SqliteRuntimeDatabase.create(path)
    identifier = "한글'; DROP TABLE model_connections; --"
    await database.connections.register(connection(identifier))
    await database.bindings.set("room'", identifier, 0)
    receipt = await database.receipts.reserve("room'", identifier, "event' OR 1=1")
    await database.receipts.mark_accepted(receipt)
    reopened = await SqliteRuntimeDatabase.open(path)
    assert await reopened.connections.get(identifier) == connection(identifier)
    assert (await reopened.bindings.get("room'")).connection_id == identifier
    assert await reopened.receipts.reserve("room'", identifier, "event' OR 1=1") == replace(
        receipt, accepted=True
    )


async def test_model_cas_and_duplicates_are_atomic_across_instances(tmp_path):
    path = tmp_path / "system.db"
    first = await SqliteRuntimeDatabase.create(path)
    second = await SqliteRuntimeDatabase.open(path)
    results = await asyncio.gather(
        first.connections.register(connection()),
        second.connections.register(connection()),
        return_exceptions=True,
    )
    assert sum(isinstance(result, RevisionConflict) for result in results) == 1
    updates = [replace(connection(), provider=provider, revision=2) for provider in ("a", "b")]
    results = await asyncio.gather(
        first.connections.update(updates[0], 1),
        second.connections.update(updates[1], 1),
        return_exceptions=True,
    )
    assert sum(isinstance(result, RevisionConflict) for result in results) == 1
    assert await first.connections.get("local") in updates
    with pytest.raises(RevisionConflict):
        await first.connections.update(replace(connection(), revision=4), 2)
    with pytest.raises(ConnectionUnavailable):
        await first.connections.get("missing")


async def test_binding_cas_unbind_aba_and_missing_connection(tmp_path):
    path = tmp_path / "system.db"
    first = await SqliteRuntimeDatabase.create(path)
    second = await SqliteRuntimeDatabase.open(path)
    await first.connections.register(connection())
    initial = await first.bindings.get("room")
    assert initial.generation == 0 and initial.connection_id is None
    results = await asyncio.gather(
        first.bindings.set("room", "local", 0),
        second.bindings.set("room", "local", 0),
        return_exceptions=True,
    )
    assert sum(isinstance(result, RevisionConflict) for result in results) == 1
    unbound = await first.bindings.set("room", None, 1)
    rebound = await second.bindings.set("room", "local", 2)
    assert unbound.generation == 2 and unbound.connection_id is None
    assert rebound.generation == 3
    with pytest.raises(ConnectionUnavailable):
        await first.bindings.set("room", "missing", 3)
    assert await second.bindings.get("room") == rebound


async def test_receipt_identity_namespace_conflicts_and_idempotent_mark(tmp_path):
    path = tmp_path / "system.db"
    first = await SqliteRuntimeDatabase.create(path)
    second = await SqliteRuntimeDatabase.open(path)
    receipts = await asyncio.gather(
        first.receipts.reserve("room", "source", "event"),
        second.receipts.reserve("room", "source", "event"),
    )
    assert receipts[0] == receipts[1]
    receipt = receipts[0]
    other = await first.receipts.reserve("room", "other", "event")
    assert other.input_id != receipt.input_id
    with pytest.raises(InputConflict):
        await second.receipts.reserve("other-room", "source", "event")
    await first.receipts.mark_accepted(receipt)
    await second.receipts.mark_accepted(receipt)
    assert (await second.receipts.reserve("room", "source", "event")).accepted
    with pytest.raises(InputConflict):
        await first.receipts.mark_accepted(replace(receipt, input_id="forged"))
    with pytest.raises(InputConflict):
        await first.receipts.mark_accepted(replace(receipt, room_id="forged"))


async def test_competing_receipt_room_assignment_has_one_winner(tmp_path):
    path = tmp_path / "system.db"
    first = await SqliteRuntimeDatabase.create(path)
    second = await SqliteRuntimeDatabase.open(path)
    results = await asyncio.gather(
        first.receipts.reserve("first", "source", "event"),
        second.receipts.reserve("second", "source", "event"),
        return_exceptions=True,
    )
    assert sum(isinstance(result, InputConflict) for result in results) == 1


async def test_owned_deletion_preserves_shared_connections_and_other_rooms(tmp_path):
    path = tmp_path / "system.db"
    database = await SqliteRuntimeDatabase.create(path)
    await database.connections.register(connection())
    await database.bindings.set("room", "local", 0)
    other_binding = await database.bindings.set("other", "local", 0)
    receipt = await database.receipts.reserve("room", "source", "event")
    other_receipt = await database.receipts.reserve("other", "source", "other-event")
    await database.bindings.delete("room")
    await database.receipts.delete("room")
    await database.bindings.delete("room")
    await database.receipts.delete("room")
    reopened = await SqliteRuntimeDatabase.open(path)
    assert (await reopened.bindings.get("room")).connection_id is None
    assert await reopened.bindings.get("other") == other_binding
    assert await reopened.connections.get("local") == connection()
    assert await reopened.receipts.reserve("other", "source", "other-event") == other_receipt
    with pytest.raises(InputConflict):
        await reopened.receipts.mark_accepted(receipt)


async def test_constraint_failure_rolls_back_without_raw_sql_details(tmp_path):
    path = tmp_path / "system.db"
    database = await SqliteRuntimeDatabase.create(path)
    await database.connections.register(connection())
    with pytest.raises(StorageUnavailable) as error:
        await database.connections.update(replace(connection(), enabled=2, revision=2), 1)
    assert "CHECK" not in str(error.value)
    assert await database.connections.get("local") == connection()


async def test_locked_database_is_error_not_memory_fallback(tmp_path, monkeypatch):
    path = tmp_path / "system.db"
    database = await SqliteRuntimeDatabase.create(path)
    connect = sqlite3.connect

    def short_timeout(*args, **kwargs):
        return connect(*args, **{**kwargs, "timeout": 0.01})

    with closing(connect(path)) as locked:
        locked.execute("BEGIN EXCLUSIVE")
        monkeypatch.setattr(sqlite_runtime.sqlite3, "connect", short_timeout)
        with pytest.raises(StorageUnavailable):
            await database.connections.register(connection())
    with pytest.raises(ConnectionUnavailable):
        await database.connections.get("local")


@pytest.mark.parametrize("fail", [False, True])
async def test_cancel_waits_for_transaction_to_settle_without_blocking_loop(
    tmp_path, monkeypatch, fail
):
    path = tmp_path / "system.db"
    database = await SqliteRuntimeDatabase.create(path)
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    connect = sqlite3.connect

    class PausedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql.startswith("INSERT INTO input_receipts"):
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(5):
                    raise sqlite3.OperationalError("test timeout")
                if fail:
                    raise sqlite3.OperationalError("private provider failure")
            return super().execute(sql, parameters)

    monkeypatch.setattr(
        sqlite_runtime.sqlite3,
        "connect",
        lambda *a, **kw: connect(*a, **kw, factory=PausedConnection),
    )
    task = asyncio.create_task(database.receipts.reserve("room", "source", "event"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    monkeypatch.setattr(sqlite_runtime.sqlite3, "connect", connect)
    with closing(connect(path)) as db:
        assert db.execute("SELECT count(*) FROM input_receipts").fetchone()[0] == (0 if fail else 1)


async def test_room_service_uses_durable_runtime_without_putting_body_in_internal_db(tmp_path):
    path = tmp_path / "system.db"
    database = await SqliteRuntimeDatabase.create(path)
    await database.connections.register(connection())
    rooms = InMemoryRoomStore()
    executor = Mock()
    executor.generate = AsyncMock(return_value=ChatResult("private response body"))
    service = RoomService(
        rooms, database.connections, executor, database.bindings, database.receipts
    )
    room = await service.create("방", RoomContext("private character"), RoomModelConfig("model"))
    await service.bind(room.id, room.revision, 0, "local")
    incoming = RoomInput(room.id, "external-user", "external-message", "private input body")
    turn = await service.respond(incoming)
    reopened = await SqliteRuntimeDatabase.open(path)
    resumed = RoomService(
        rooms, reopened.connections, executor, reopened.bindings, reopened.receipts
    )
    assert await resumed.respond(incoming) == turn
    executor.generate.assert_awaited_once()
    with closing(sqlite3.connect(path)) as db:
        dump = "\n".join(db.iterdump())
    assert "external-user" in dump and "key-reference" in dump
    assert "private input body" not in dump
    assert "private response body" not in dump
    assert "private character" not in dump

    # 방 DB가 아직 메모리인 현재 조합은 프로세스 재시작 복구를 완성하지 않는다.
    missing_rooms = InMemoryRoomStore()
    await missing_rooms.create(room)
    incomplete = RoomService(
        missing_rooms, reopened.connections, executor, reopened.bindings, reopened.receipts
    )
    with pytest.raises(InputConflict):
        await incomplete.respond(incoming)
    executor.generate.assert_awaited_once()
