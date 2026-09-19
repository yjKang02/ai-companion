"""개인 라이브러리 경로 검증과 OS 잠금. 재귀 삭제는 제공하지 않는다."""

import os
import stat
import sys
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4

from ai_companion.application.room_ports import StorageUnavailable


def plain_path(path: Path, *, missing: bool = False) -> None:
    """경로 구성요소의 symlink/reparse를 거부한다. 협력하지 않는 외부 교체는 지원하지 않는다."""
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            if missing:
                continue
            raise StorageUnavailable("라이브러리 경로가 없습니다.") from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise StorageUnavailable("링크 또는 reparse 경로는 지원하지 않습니다.")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise StorageUnavailable("여러 경로가 공유하는 파일은 지원하지 않습니다.")


def canonical_room_id(room_id: str) -> None:
    try:
        if str(UUID(room_id)) != room_id:
            raise ValueError
    except ValueError:
        raise StorageUnavailable("안전한 UUID 방 ID가 필요합니다.") from None


def initialize_library(path: Path) -> None:
    """기존 폴더를 재사용하지 않고 새 개인 라이브러리를 만든다."""
    plain_path(path, missing=True)
    path.mkdir()
    with (path / ".library-id").open("x", encoding="ascii") as marker:
        marker.write(str(uuid4()))
        marker.flush()
        os.fsync(marker.fileno())


def library_identity(path: Path) -> str:
    plain_path(path)
    marker = path / ".library-id"
    plain_path(marker)
    with marker.open(encoding="ascii") as stream:
        value = stream.read(128)
    canonical_room_id(value)
    return value


class FileLease:
    """프로세스 종료 시 OS가 해제하는 비차단 잠금. 잠금 파일은 제거하지 않는다."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: BinaryIO | None = None

    def acquire(self) -> None:
        plain_path(self.path, missing=True)
        stream = self.path.open("a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                if stream.seek(0, os.SEEK_END) == 0:
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            stream.close()
            raise
        self._file = stream

    def release(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
