"""Register source files by content hash; original files are never modified or moved."""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

import duckdb

FILE_KINDS: Final = ("tb", "gl", "ar_aging", "ap_aging", "bank", "statement", "sales")
_FILE_COLUMNS = "file_id, entity_id, path, sha256, kind, imported_at, row_count, page_count, notes"


@dataclass(frozen=True)
class RegisteredFile:
    """An immutable source-file registration read from the database."""

    file_id: str
    entity_id: str
    path: str
    sha256: str
    kind: str
    imported_at: datetime
    row_count: int | None
    page_count: int | None
    notes: str | None


class IntakeError(ValueError):
    """Base error for source-file registration failures."""


class MissingSourceFileError(IntakeError):
    """The supplied source path does not exist."""


class NotAFileError(IntakeError):
    """The supplied source path is not a regular file."""


class EmptySourceFileError(IntakeError):
    """The supplied source file contains no bytes."""


class InvalidFileKindError(IntakeError):
    """The supplied source-file kind is unsupported."""


class UnknownEntityError(IntakeError):
    """The supplied entity has not been registered."""


class DuplicateFileError(IntakeError):
    """The full content hash already belongs to a registered source file."""

    existing: RegisteredFile

    def __init__(self, existing: RegisteredFile) -> None:
        """Attach the original registration and include its identity in the message."""
        self.existing = existing
        super().__init__(
            f"Source file already registered: file_id={existing.file_id}, "
            f"path={existing.path}, imported_at={existing.imported_at}"
        )


def _source_path(path: Path) -> Path:
    """Convert a source path without resolving, normalizing, or making it absolute."""
    try:
        return Path(path)
    except (TypeError, ValueError) as exc:
        raise IntakeError(f"Invalid source path: {path!r}") from exc


def compute_sha256(path: Path, chunk_size: int = 65536) -> str:
    """Stream a file in bounded chunks and return its lowercase SHA-256 digest."""
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")
    path = _source_path(path)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(chunk_size):
                digest.update(chunk)
    except FileNotFoundError as exc:
        raise MissingSourceFileError(f"Source file does not exist: {path.as_posix()}") from exc
    except IsADirectoryError as exc:
        raise NotAFileError(f"Source path is not a regular file: {path.as_posix()}") from exc
    except OSError as exc:
        raise IntakeError(f"Cannot read source file {path.as_posix()}: {exc}") from exc
    return digest.hexdigest()


def _lookup_file(
    conn: duckdb.DuckDBPyConnection, column: Literal["sha256", "file_id"], value: str
) -> RegisteredFile | None:
    """Read a registration using one of the two fixed lookup columns."""
    try:
        row = conn.execute(
            f"SELECT {_FILE_COLUMNS} FROM source_file WHERE {column} = ?", [value]
        ).fetchone()
    except duckdb.Error as exc:
        raise IntakeError(f"Cannot look up source file by {column}: {exc}") from exc
    return RegisteredFile(*row) if row is not None else None


def find_by_sha256(conn: duckdb.DuckDBPyConnection, sha256: str) -> RegisteredFile | None:
    """Return the registration for a full SHA-256 hash, or None if absent."""
    return _lookup_file(conn, "sha256", sha256)


def get_file(conn: duckdb.DuckDBPyConnection, file_id: str) -> RegisteredFile | None:
    """Return the registration for a file ID, or None if absent."""
    return _lookup_file(conn, "file_id", file_id)


def register_file(
    conn: duckdb.DuckDBPyConnection,
    entity_id: str,
    path: Path,
    kind: str,
    *,
    row_count: int | None = None,
    page_count: int | None = None,
    notes: str | None = None,
    stored_path: str | None = None,
) -> RegisteredFile:
    """Validate and insert one immutable registration without modifying its source file."""
    if kind not in FILE_KINDS:
        raise InvalidFileKindError(f"Invalid file kind {kind!r}; allowed: {', '.join(FILE_KINDS)}")
    for name, count in (("row_count", row_count), ("page_count", page_count)):
        if count is not None and (
            isinstance(count, bool) or not isinstance(count, int) or count < 0
        ):
            raise IntakeError(f"{name} must be a non-negative int (not bool), or None")

    path = _source_path(path)
    try:
        if not path.exists():
            raise MissingSourceFileError(f"Source file does not exist: {path.as_posix()}")
        if not path.is_file():
            raise NotAFileError(f"Source path is not a regular file: {path.as_posix()}")
        if path.stat().st_size == 0:
            raise EmptySourceFileError(f"Source file is empty: {path.as_posix()}")
    except OSError as exc:
        raise IntakeError(f"Cannot inspect source file {path.as_posix()}: {exc}") from exc

    try:
        entity = conn.execute("SELECT 1 FROM entity WHERE entity_id = ?", [entity_id]).fetchone()
    except duckdb.Error as exc:
        raise IntakeError(f"Cannot look up entity {entity_id}: {exc}") from exc
    if entity is None:
        raise UnknownEntityError(f"Unknown entity: {entity_id}")

    sha256 = compute_sha256(path)
    existing = find_by_sha256(conn, sha256)
    if existing is not None:
        raise DuplicateFileError(existing)

    file_id = "F_" + sha256[:16]
    registration_path = stored_path if stored_path is not None else path.as_posix()
    try:
        conn.execute("BEGIN TRANSACTION")
    except duckdb.Error as exc:
        # A failed BEGIN does not make the caller's existing transaction ours.
        raise IntakeError(f"Cannot begin source-file registration: {exc}") from exc
    try:
        conn.execute(
            """
            INSERT INTO source_file (
                file_id, entity_id, path, sha256, kind, row_count, page_count, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [file_id, entity_id, registration_path, sha256, kind, row_count, page_count, notes],
        )
        conn.execute("COMMIT")
    except BaseException as exc:
        try:
            conn.execute("ROLLBACK")
        except duckdb.Error as rollback_error:
            # DuckDB may already have ended a transaction rejected at COMMIT.
            exc.add_note(f"Rollback also reported: {rollback_error}")
        if isinstance(exc, (duckdb.ConstraintException, duckdb.TransactionException)):
            try:
                existing = find_by_sha256(conn, sha256)
            except IntakeError as lookup_error:
                raise IntakeError(
                    f"Cannot register {file_id}; duplicate recheck failed: {lookup_error}"
                ) from exc
            if existing is not None:
                raise DuplicateFileError(existing) from exc
        if isinstance(exc, duckdb.Error):
            raise IntakeError(f"Cannot register source file {file_id}: {exc}") from exc
        raise

    registered = get_file(conn, file_id)
    if registered is None:
        raise IntakeError(f"Registered source file could not be read back: {file_id}")
    return registered
