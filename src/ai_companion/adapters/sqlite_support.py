"""내부·방 SQLite 어댑터가 공유하는 취소와 비식별 진단 경계."""

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from typing import TypeVar

_T = TypeVar("_T")
_log = logging.getLogger(__name__)


def report_failure(operation: str, error: BaseException) -> None:
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


async def settled_thread(operation: Callable[[], _T]) -> _T:
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
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                report_failure("cancelled", error)
        raise asyncio.CancelledError
    return task.result()
