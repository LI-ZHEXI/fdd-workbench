"""Source-file registration, validation, hashing, and path-preservation tests."""

import hashlib
import shutil
from datetime import datetime
from pathlib import Path

import duckdb
import pytest

from fdd_workbench.intake import registry
from fdd_workbench.intake.registry import (
    FILE_KINDS,
    DuplicateFileError,
    EmptySourceFileError,
    IntakeError,
    InvalidFileKindError,
    MissingSourceFileError,
    NotAFileError,
    RegisteredFile,
    UnknownEntityError,
    compute_sha256,
    find_by_sha256,
    get_file,
    register_file,
)


def test_register_file_returns_persisted_record_without_changing_source(
    store_with_entity: duckdb.DuckDBPyConnection, sample_file: Path
) -> None:
    """Register a nonempty file once and preserve its original bytes and mtime."""
    original = sample_file.read_bytes()
    original_mtime = sample_file.stat().st_mtime_ns
    expected_hash = hashlib.sha256(original).hexdigest()

    record = register_file(store_with_entity, "ENT_T", sample_file, "tb")

    assert isinstance(record, RegisteredFile)
    assert record.file_id.startswith("F_")
    assert len(record.file_id) == 18
    assert record.file_id == "F_" + expected_hash[:16]
    assert record.sha256 == expected_hash
    assert record.entity_id == "ENT_T"
    assert record.kind == "tb"
    assert isinstance(record.imported_at, datetime)
    assert record.row_count is None
    assert record.page_count is None
    assert record.notes is None
    assert get_file(store_with_entity, record.file_id) == record
    assert store_with_entity.execute("SELECT count(*) FROM source_file").fetchone() == (1,)
    assert sample_file.read_bytes() == original
    assert sample_file.stat().st_mtime_ns == original_mtime


def test_register_file_persists_optional_metadata(
    store_with_entity: duckdb.DuckDBPyConnection, sample_file: Path
) -> None:
    """Persist caller-supplied metadata in the initial source-file record."""
    record = register_file(
        store_with_entity,
        "ENT_T",
        sample_file,
        "tb",
        row_count=120,
        page_count=None,
        notes="SIMULATED",
    )

    assert record.row_count == 120
    assert record.page_count is None
    assert record.notes == "SIMULATED"
    assert get_file(store_with_entity, record.file_id) == record
    assert store_with_entity.execute("SELECT row_count FROM source_file").fetchone() == (120,)


def test_same_content_with_different_filename_is_rejected(
    store_with_entity: duckdb.DuckDBPyConnection, sample_file: Path
) -> None:
    """Reject duplicate bytes with the original record and useful diagnostics."""
    first = register_file(store_with_entity, "ENT_T", sample_file, "tb")
    copy = sample_file.with_name("tb_copy.csv")
    shutil.copyfile(sample_file, copy)
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (sample_file, copy)}

    with pytest.raises(DuplicateFileError) as caught:
        register_file(store_with_entity, "ENT_T", copy, "tb")

    assert caught.value.existing == first
    assert caught.value.existing.file_id == first.file_id
    assert isinstance(caught.value, IntakeError)
    message = str(caught.value)
    assert first.file_id in message
    assert first.path in message
    assert str(first.imported_at) in message
    assert store_with_entity.execute("SELECT count(*) FROM source_file").fetchone() == (1,)
    for path, (contents, mtime) in before.items():
        assert path.read_bytes() == contents
        assert path.stat().st_mtime_ns == mtime


def test_different_content_produces_distinct_hashes_and_ids(
    store_with_entity: duckdb.DuckDBPyConnection, sample_file: Path
) -> None:
    """Register distinct content independently and accept zero metadata counts."""
    first = register_file(store_with_entity, "ENT_T", sample_file, "tb")
    second_path = sample_file.with_name("gl_SIMULATED.csv")
    second_path.write_text("account,debit,credit\n1001,20,0\n", encoding="utf-8")

    second = register_file(store_with_entity, "ENT_T", second_path, "gl", row_count=0, page_count=0)

    assert first.sha256 != second.sha256
    assert first.file_id != second.file_id
    assert second.kind == "gl"
    assert second.row_count == second.page_count == 0
    assert store_with_entity.execute("SELECT count(*) FROM source_file").fetchone() == (2,)


def test_invalid_kind_is_rejected_before_counts_and_path(
    store_with_entity: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    """List every legal kind before considering invalid counts or a missing file."""
    assert FILE_KINDS == ("tb", "gl", "ar_aging", "ap_aging", "bank", "statement", "sales")
    with pytest.raises(InvalidFileKindError) as caught:
        register_file(
            store_with_entity, "UNKNOWN", tmp_path / "missing.csv", "ledger", row_count=-1
        )

    assert isinstance(caught.value, IntakeError)
    assert all(kind in str(caught.value) for kind in FILE_KINDS)
    assert store_with_entity.execute("SELECT count(*) FROM source_file").fetchone() == (0,)


def test_missing_file_is_rejected_before_unknown_entity(
    store_with_entity: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    """Report the caller's missing path without inserting a source-file row."""
    missing = tmp_path / "missing.csv"
    with pytest.raises(MissingSourceFileError) as caught:
        register_file(store_with_entity, "UNKNOWN", missing, "tb")

    assert isinstance(caught.value, IntakeError)
    assert missing.as_posix() in str(caught.value)
    assert store_with_entity.execute("SELECT count(*) FROM source_file").fetchone() == (0,)


def test_directory_is_rejected_before_unknown_entity(
    store_with_entity: duckdb.DuckDBPyConnection, sample_file: Path
) -> None:
    """Reject a directory with its supplied path before checking the entity."""
    directory = sample_file.parent
    with pytest.raises(NotAFileError) as caught:
        register_file(store_with_entity, "UNKNOWN", directory, "tb")

    assert isinstance(caught.value, IntakeError)
    assert directory.as_posix() in str(caught.value)
    assert store_with_entity.execute("SELECT count(*) FROM source_file").fetchone() == (0,)


def test_empty_file_is_rejected_before_unknown_entity(
    store_with_entity: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    """Reject an empty regular file before validating its entity."""
    empty = tmp_path / "empty.csv"
    empty.write_bytes(b"")
    with pytest.raises(EmptySourceFileError) as caught:
        register_file(store_with_entity, "UNKNOWN", empty, "tb")

    assert isinstance(caught.value, IntakeError)
    assert empty.as_posix() in str(caught.value)
    assert store_with_entity.execute("SELECT count(*) FROM source_file").fetchone() == (0,)


def test_unknown_entity_is_rejected_before_hashing(
    seeded_store: duckdb.DuckDBPyConnection, sample_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raise a readable entity error before performing any content hashing."""

    def unexpected_hash(path: Path, chunk_size: int = 65536) -> str:
        """Fail if hashing runs before entity validation."""
        pytest.fail("Unknown entities must be rejected before hashing")

    monkeypatch.setattr(registry, "compute_sha256", unexpected_hash)
    with pytest.raises(UnknownEntityError) as caught:
        register_file(seeded_store, "ENT_T", sample_file, "tb")

    assert isinstance(caught.value, IntakeError)
    assert "ENT_T" in str(caught.value)
    assert seeded_store.execute("SELECT count(*) FROM source_file").fetchone() == (0,)


def test_invalid_counts_are_rejected_before_path_validation(
    store_with_entity: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    """Reject negative, boolean, and noninteger counts without partial inserts."""
    for field in ("row_count", "page_count"):
        for value in (-1, True, False, 1.5, "1"):
            with pytest.raises(IntakeError) as caught:
                register_file(
                    store_with_entity,
                    "UNKNOWN",
                    tmp_path / "missing.csv",
                    "tb",
                    **{field: value},
                )
            assert type(caught.value) is IntakeError
            assert field in str(caught.value)
            assert store_with_entity.execute("SELECT count(*) FROM source_file").fetchone() == (0,)


def test_streaming_sha256_matches_reference_across_chunk_sizes(
    seeded_store: duckdb.DuckDBPyConnection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hash a multichunk file correctly without read_bytes and reject empty reads."""
    path = tmp_path / "large.bin"
    path.write_bytes((bytes(range(256)) * 782)[:200_001])
    assert path.stat().st_size == 200_001
    expected = hashlib.sha256(path.read_bytes()).hexdigest()

    def unexpected_read_bytes(self: Path) -> bytes:
        """Fail if the hash implementation attempts a whole-file read."""
        pytest.fail("compute_sha256 must use streaming reads")

    monkeypatch.setattr(Path, "read_bytes", unexpected_read_bytes)
    assert compute_sha256(path) == compute_sha256(path, chunk_size=7) == expected
    for chunk_size in (0, -1):
        with pytest.raises(ValueError):
            compute_sha256(path, chunk_size=chunk_size)
        assert seeded_store.execute("SELECT count(*) FROM source_file").fetchone() == (0,)


def test_relative_path_preserves_privacy(
    store_with_entity: duckdb.DuckDBPyConnection,
    sample_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep relative input paths free of the temporary directory's components."""
    monkeypatch.chdir(tmp_path)
    relative = Path("raw/tb_SIMULATED.csv")
    assert relative.read_bytes() == sample_file.read_bytes()

    record = register_file(store_with_entity, "ENT_T", relative, "tb")

    assert record.path == "raw/tb_SIMULATED.csv"
    assert not record.path.startswith("/")
    assert not set(tmp_path.parts[1:]).intersection(Path(record.path).parts)
    assert store_with_entity.execute("SELECT path FROM source_file").fetchone() == (
        "raw/tb_SIMULATED.csv",
    )


def test_absolute_path_preserves_parent_components(
    store_with_entity: duckdb.DuckDBPyConnection, sample_file: Path, tmp_path: Path
) -> None:
    """Store absolute paths verbatim instead of normalizing parent references."""
    supplied_path = tmp_path / "raw" / ".." / "raw" / sample_file.name

    record = register_file(store_with_entity, "ENT_T", supplied_path, "tb")

    assert record.path == supplied_path.as_posix()
    assert "/raw/../raw/" in record.path
    assert store_with_entity.execute("SELECT path FROM source_file").fetchone() == (
        supplied_path.as_posix(),
    )


def test_stored_path_override_and_lookup_helpers(
    store_with_entity: duckdb.DuckDBPyConnection, sample_file: Path
) -> None:
    """Use the explicit storage label and support successful and missing lookups."""
    record = register_file(
        store_with_entity, "ENT_T", sample_file, "tb", stored_path="vdr/01/tb.csv"
    )

    assert record.path == "vdr/01/tb.csv"
    assert store_with_entity.execute("SELECT path FROM source_file").fetchone() == (
        "vdr/01/tb.csv",
    )
    assert find_by_sha256(store_with_entity, record.sha256) == record
    assert find_by_sha256(store_with_entity, "0" * 64) is None
    assert get_file(store_with_entity, record.file_id) == record
    assert get_file(store_with_entity, "F_MISSING") is None
