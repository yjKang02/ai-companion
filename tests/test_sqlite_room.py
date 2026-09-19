import asyncio
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import replace

import pytest

from ai_companion.adapters.sqlite_room import SqliteRoomDatabase
from ai_companion.adapters.sqlite_runtime import SqliteRuntimeDatabase
from ai_companion.application.room_ports import InputConflict, RevisionConflict, StorageUnavailable
from ai_companion.domain import (
    ChatMessage,
    ChatResult,
    Role,
    Room,
    RoomContext,
    RoomInput,
    RoomModelConfig,
    StoredRoomInput,
    TokenUsage,
    TurnState,
)


def room(identifier="room"):
    return Room(identifier, "방", RoomContext("캐릭터", "사용자", "지시"), RoomModelConfig("model"))


def incoming(identifier="input", text="안녕", room_id="room"):
    return StoredRoomInput(identifier, room_id, text)


@pytest.fixture
async def saved(tmp_path):
    path = tmp_path / "방 #1.db"
    return path, await SqliteRoomDatabase.create(path, room())


async def test_reopen_restores_context_input_response_and_usage(saved):
    path, db = saved
    turn, accepted = await db.accept(incoming(), 1)
    assert accepted
    result = ChatResult("응답", "stop", TokenUsage(12, 4))
    finished = await db.finish(turn, TurnState.COMPLETED, result)
    reopened = await SqliteRoomDatabase.open(path, "room")
    assert await reopened.get() == room()
    assert await reopened.accept(incoming(), 1) == (finished, False)
    assert await reopened.history() == (
        ChatMessage(Role.USER, "안녕"),
        ChatMessage(Role.ASSISTANT, "응답"),
    )
    assert await reopened.finish(turn, TurnState.FAILED) == finished


async def test_separate_process_reads_persisted_room_and_internal_receipt(tmp_path):
    runtime_path, room_path = tmp_path / "system.db", tmp_path / "room.db"
    runtime = await SqliteRuntimeDatabase.create(runtime_path)
    external = await SqliteRoomDatabase.create(room_path, room())
    receipt = await runtime.receipts.reserve("room", "external-source", "external-event")
    turn, _ = await external.accept(StoredRoomInput(receipt.input_id, "room", "안녕"), 1)
    await runtime.receipts.mark_accepted(receipt)
    await external.finish(turn, TurnState.COMPLETED, ChatResult("응답"))
    script = """
import asyncio, sys
from pathlib import Path
from ai_companion.adapters.sqlite_runtime import SqliteRuntimeDatabase
from ai_companion.adapters.sqlite_room import SqliteRoomDatabase
from ai_companion.domain import StoredRoomInput, TurnState
async def check():
    runtime = await SqliteRuntimeDatabase.open(Path(sys.argv[1]))
    room = await SqliteRoomDatabase.open(Path(sys.argv[2]), "room")
    receipt = await runtime.receipts.reserve("room", "external-source", "external-event")
    assert receipt.accepted
    stored = StoredRoomInput(receipt.input_id, "room", "안녕")
    turn, accepted = await room.accept(stored, 1, allow_new=False)
    assert not accepted and turn.state == TurnState.COMPLETED
    assert [message.content for message in await room.history()] == ["안녕", "응답"]
asyncio.run(check())
"""
    completed = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", script, str(runtime_path), str(room_path)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr


async def test_concurrent_completion_stores_exactly_one_response(saved):
    path, first = saved
    second = await SqliteRoomDatabase.open(path, "room")
    turn, _ = await first.accept(incoming(), 1)
    results = await asyncio.gather(
        first.finish(turn, TurnState.COMPLETED, ChatResult("first")),
        second.finish(turn, TurnState.COMPLETED, ChatResult("second")),
    )
    assert results[0] == results[1]
    assert len(await first.history()) == 2


async def test_two_rooms_keep_data_and_edits_independent(tmp_path):
    first = await SqliteRoomDatabase.create(tmp_path / "first.db", room())
    second = await SqliteRoomDatabase.create(tmp_path / "second.db", room("second"))
    edited = replace(room(), context=RoomContext("변경 캐릭터"), revision=2)
    await first.update(edited, 1)
    turn, _ = await first.accept(incoming(), 2)
    await first.finish(turn, TurnState.COMPLETED, ChatResult("응답"))
    assert await second.get() == room("second")
    assert await second.history() == ()
    with pytest.raises(InputConflict):
        await second.accept(incoming(), 1)
    with pytest.raises(InputConflict):
        await second.finish(turn, TurnState.COMPLETED, ChatResult("잘못된 방"))


async def test_update_keeps_revision_snapshot_and_supersedes_late_result(saved):
    path, db = saved
    turn, _ = await db.accept(incoming(), 1)
    edited = replace(room(), model=RoomModelConfig("other-model"), revision=2)
    await db.update(edited, 1)
    late = await db.finish(turn, TurnState.COMPLETED, ChatResult("늦은 응답"))
    assert late.state == TurnState.SUPERSEDED and late.result is None
    assert await db.history() == ()
    with closing(sqlite3.connect(path)) as raw:
        assert raw.execute("SELECT model_id FROM revisions ORDER BY revision").fetchall() == [
            ("model",),
            ("other-model",),
        ]
    with pytest.raises(RevisionConflict):
        await db.update(replace(edited, revision=3), 1)
    assert await db.get() == edited


async def test_concurrent_edit_and_duplicate_input_have_one_winner(saved):
    path, first = saved
    second = await SqliteRoomDatabase.open(path, "room")
    edits = [replace(room(), name=name, revision=2) for name in ("a", "b")]
    outcomes = await asyncio.gather(
        first.update(edits[0], 1), second.update(edits[1], 1), return_exceptions=True
    )
    assert sum(isinstance(outcome, RevisionConflict) for outcome in outcomes) == 1
    assert await first.get() in edits
    outcomes = await asyncio.gather(first.accept(incoming(), 2), second.accept(incoming(), 2))
    assert sorted(accepted for _, accepted in outcomes) == [False, True]
    assert outcomes[0][0] == outcomes[1][0]


async def test_changed_body_unknown_result_and_missing_accepted_input_fail(saved):
    _, db = saved
    turn, _ = await db.accept(incoming(), 1)
    with pytest.raises(InputConflict):
        await db.accept(incoming(text="다른 본문"), 1)
    with pytest.raises(InputConflict):
        await db.accept(incoming("missing"), 1, allow_new=False)
    with pytest.raises(InputConflict):
        await db.finish(replace(turn, room_revision=99), TurnState.FAILED)
    with pytest.raises(InputConflict):
        await db.finish(replace(turn, input=incoming("missing")), TurnState.FAILED)
    assert await db.accept(incoming(), 1) == (turn, False)


@pytest.mark.parametrize(
    "state", [TurnState.FAILED, TurnState.CANCELLED, TurnState.SUPERSEDED, TurnState.INTERRUPTED]
)
async def test_unsuccessful_turns_persist_without_history(saved, state):
    path, db = saved
    turn, _ = await db.accept(incoming(), 1)
    failed = await db.finish(turn, state, ChatResult("저장하면 안 되는 응답"))
    reopened = await SqliteRoomDatabase.open(path, "room")
    assert await reopened.accept(incoming(), 1) == (failed, False)
    assert failed.result is None
    assert await reopened.history() == ()
    with closing(sqlite3.connect(path)) as raw:
        assert raw.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


async def test_open_is_not_recovery_and_explicit_recovery_never_retries(saved):
    path, db = saved
    pending, _ = await db.accept(incoming(), 1)
    finished, _ = await db.accept(incoming("finished"), 1)
    completed = await db.finish(finished, TurnState.COMPLETED, ChatResult("완료"))
    reopened = await SqliteRoomDatabase.open(path, "room")
    assert await reopened.accept(incoming(), 1) == (pending, False)
    assert await reopened.interrupt_pending() == 1
    assert await reopened.interrupt_pending() == 0
    interrupted = replace(pending, state=TurnState.INTERRUPTED)
    assert await reopened.accept(incoming(), 1) == (interrupted, False)
    assert (
        await reopened.finish(pending, TurnState.COMPLETED, ChatResult("이전 프로세스 결과"))
        == interrupted
    )
    assert await reopened.accept(incoming("finished"), 1) == (completed, False)


async def test_history_is_ordered_by_input_not_completion(saved):
    _, db = saved
    first, _ = await db.accept(incoming("first", "먼저"), 1)
    second, _ = await db.accept(incoming("second", "나중"), 1)
    await db.finish(second, TurnState.COMPLETED, ChatResult("둘째"))
    await db.finish(first, TurnState.COMPLETED, ChatResult("첫째"))
    assert [message.content for message in await db.history()] == ["먼저", "첫째", "나중", "둘째"]


async def test_input_and_response_have_one_body_source_and_no_transport_fields(saved):
    path, db = saved
    with pytest.raises(TypeError):
        await db.accept(RoomInput("room", "private-user", "private-message", "hello"), 1)
    turn, _ = await db.accept(incoming(text="unique input content"), 1)
    await db.finish(turn, TurnState.COMPLETED, ChatResult("unique output content"))
    with closing(sqlite3.connect(path)) as raw:
        dump = "\n".join(raw.iterdump())
    assert dump.count("unique input content") == 1
    assert dump.count("unique output content") == 1
    for name in ("source", "request_id", "connection_id", "base_url", "secret_ref", "private-user"):
        assert name not in dump


@pytest.mark.parametrize("stage", ["revision", "turn", "message"])
async def test_failed_second_write_rolls_back_the_whole_operation(saved, stage, caplog):
    path, db = saved
    turn, _ = await db.accept(incoming(), 1)
    target, action = {
        "revision": ("room", "UPDATE"),
        "turn": ("turns", "INSERT"),
        "message": ("messages", "INSERT"),
    }[stage]
    with closing(sqlite3.connect(path)) as raw, raw:
        raw.execute(
            f"CREATE TRIGGER fail_write BEFORE {action} ON {target} "
            "BEGIN SELECT RAISE(ABORT, 'private body'); END"
        )
    with pytest.raises(StorageUnavailable) as error:
        if stage == "revision":
            await db.update(replace(room(), revision=2), 1)
        elif stage == "turn":
            await db.accept(incoming("second"), 1)
        else:
            await db.finish(turn, TurnState.COMPLETED, ChatResult("응답"))
    assert "private body" not in str(error.value) and "private body" not in caplog.text
    assert await db.get() == room()
    assert await db.accept(incoming(), 1) == (turn, False)
    assert await db.history() == ()
    with closing(sqlite3.connect(path)) as raw:
        assert raw.execute("SELECT count(*) FROM revisions").fetchone()[0] == 1
        assert raw.execute("SELECT count(*) FROM inputs").fetchone()[0] == 1


async def test_missing_wrong_room_runtime_file_and_existing_file_are_rejected(tmp_path):
    path = tmp_path / "room.db"
    with pytest.raises(StorageUnavailable):
        await SqliteRoomDatabase.open(path, "room")
    assert not path.exists()
    await SqliteRoomDatabase.create(path, room())
    before = path.read_bytes()
    with pytest.raises(StorageUnavailable):
        await SqliteRoomDatabase.create(path, room())
    with pytest.raises(StorageUnavailable):
        await SqliteRoomDatabase.open(path, "wrong")
    assert path.read_bytes() == before
    runtime_path = tmp_path / "system.db"
    await SqliteRuntimeDatabase.create(runtime_path)
    with pytest.raises(StorageUnavailable):
        await SqliteRoomDatabase.open(runtime_path, "room")


@pytest.mark.parametrize(
    "damage", ["version", "table", "foreign-key", "orphan-input", "missing-response", "garbage"]
)
async def test_corruption_is_not_auto_repaired(saved, damage):
    path, db = saved
    turn, _ = await db.accept(incoming(), 1)
    await db.finish(turn, TurnState.COMPLETED, ChatResult("응답"))
    if damage == "garbage":
        path.write_bytes(b"not sqlite private content")
    else:
        with closing(sqlite3.connect(path)) as raw, raw:
            if damage == "version":
                raw.execute("PRAGMA user_version=99")
            elif damage == "table":
                raw.execute("DROP TABLE messages")
            elif damage == "foreign-key":
                raw.execute("UPDATE inputs SET revision=99")
            elif damage == "orphan-input":
                raw.execute("DELETE FROM messages")
                raw.execute("DELETE FROM turns")
            else:
                raw.execute("DELETE FROM messages")
    before = path.read_bytes()
    with pytest.raises(StorageUnavailable):
        await SqliteRoomDatabase.open(path, "room")
    assert path.read_bytes() == before


async def test_moved_file_can_be_opened_but_replaced_file_is_not_silently_used(saved, tmp_path):
    path, db = saved
    moved = tmp_path / "moved.db"
    path.rename(moved)
    with pytest.raises(StorageUnavailable):
        await db.get()
    assert not path.exists()
    assert await (await SqliteRoomDatabase.open(moved, "room")).get() == room()
    await SqliteRoomDatabase.create(path, room())
    with pytest.raises(StorageUnavailable, match="교체"):
        await db.get()


@pytest.mark.parametrize(
    "model", [RoomModelConfig("m", temperature=float("nan")), RoomModelConfig("m", max_tokens=True)]
)
async def test_invalid_options_never_create_a_file(tmp_path, model):
    path = tmp_path / "invalid.db"
    with pytest.raises(ValueError):
        await SqliteRoomDatabase.create(path, replace(room(), model=model))
    assert not path.exists()
