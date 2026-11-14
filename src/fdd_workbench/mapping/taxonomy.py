"""Validate the standard account taxonomy and insert immutable account definitions."""

import csv
from pathlib import Path
from typing import Literal, Self

import duckdb
from pydantic import BaseModel, ConfigDict, PrivateAttr, ValidationError, model_validator

_COLUMNS = (
    "std_code",
    "name",
    "name_zh",
    "statement",
    "category",
    "normal_sign",
    "is_ebitda_component",
    "is_nwc_component",
    "is_net_debt_component",
)
_BOOLEAN_COLUMNS = _COLUMNS[6:]
_EBITDA_CATEGORIES = frozenset({"revenue", "cogs", "opex_sm", "opex_ga", "opex_rd"})
_NON_EBITDA_IS_CATEGORIES = frozenset({"da", "interest", "tax", "non_op"})
_IS_CATEGORIES = _EBITDA_CATEGORIES | _NON_EBITDA_IS_CATEGORIES
_BS_CATEGORIES = frozenset(
    {"nwc_ar", "nwc_inventory", "nwc_ap", "nwc_other", "cash", "debt", "debt_like", "equity"}
)
_NET_DEBT_CATEGORIES = frozenset({"cash", "debt", "debt_like"})
_CATEGORY_SIGNS = {
    "revenue": -1,
    "cogs": 1,
    "opex_sm": 1,
    "opex_ga": 1,
    "opex_rd": 1,
    "da": 1,
    "tax": 1,
    "nwc_ar": 1,
    "nwc_inventory": 1,
    "nwc_ap": -1,
    "cash": 1,
    "debt": -1,
    "debt_like": -1,
    "equity": -1,
}


class TaxonomyError(ValueError):
    """A taxonomy row, file, or stored definition failed validation."""


class StdAccountRow(BaseModel):
    """One account definition using the canonical schema's category vocabulary."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    std_code: str
    name: str
    name_zh: str
    statement: Literal["IS", "BS", "CF"]
    category: Literal[
        "revenue",
        "cogs",
        "opex_sm",
        "opex_ga",
        "opex_rd",
        "da",
        "interest",
        "tax",
        "non_op",
        "nwc_ar",
        "nwc_inventory",
        "nwc_ap",
        "nwc_other",
        "cash",
        "debt",
        "debt_like",
        "equity",
        "other",
    ]
    normal_sign: Literal[1, -1]
    is_ebitda_component: bool
    is_nwc_component: bool
    is_net_debt_component: bool

    _csv_line_number: int | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def validate_account_rules(self) -> Self:
        """Keep statement placement and bridge flags consistent with category."""
        for field_name in ("std_code", "name", "name_zh"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be empty")
        if self.category in _NON_EBITDA_IS_CATEGORIES and self.is_ebitda_component:
            raise ValueError(f"category {self.category} must not be an EBITDA component")
        if self.is_ebitda_component and (
            self.statement != "IS" or self.category not in _EBITDA_CATEGORIES
        ):
            raise ValueError("an EBITDA component must have statement IS and an operating category")
        if self.is_nwc_component and not self.category.startswith("nwc_"):
            raise ValueError("an NWC component must have an nwc_ category")
        if self.is_net_debt_component and self.category not in _NET_DEBT_CATEGORIES:
            raise ValueError("a net debt component must have category cash, debt, or debt_like")
        if self.category in _IS_CATEGORIES and self.statement != "IS":
            raise ValueError(f"category {self.category} requires statement IS")
        if self.category in _BS_CATEGORIES and self.statement != "BS":
            raise ValueError(f"category {self.category} requires statement BS")
        return self


def _error(line_number: int, std_code: str, message: str) -> TaxonomyError:
    return TaxonomyError(f"line {line_number}, std_code={std_code}: {message}")


def _validate_file_rules(rows: list[StdAccountRow]) -> None:
    seen: set[str] = set()
    for index, row in enumerate(rows, start=2):
        line_number = row._csv_line_number or index
        if row.std_code in seen:
            raise _error(line_number, row.std_code, "duplicate std_code")
        seen.add(row.std_code)
        expected_sign = _CATEGORY_SIGNS.get(row.category)
        if expected_sign is not None and row.normal_sign != expected_sign:
            raise _error(
                line_number,
                row.std_code,
                f"category {row.category} requires normal_sign={expected_sign}",
            )


def load_std_accounts(csv_path: Path) -> list[StdAccountRow]:
    """Read UTF-8 CSV, preserving row locations in every validation error."""
    rows: list[StdAccountRow] = []
    line_number = 1
    std_code = "<header>"
    try:
        with Path(csv_path).open(encoding="utf-8", newline="") as csv_file:
            reader = csv.DictReader(csv_file, strict=True)
            if reader.fieldnames != list(_COLUMNS):
                raise _error(1, std_code, f"CSV header must be {','.join(_COLUMNS)}")
            while True:
                line_number = reader.line_num + 1
                std_code = "<unreadable>"
                try:
                    raw_row = next(reader)
                except StopIteration:
                    break
                line_number = reader.line_num
                std_code = raw_row.get("std_code") or "<missing>"
                if None in raw_row or any(value is None for value in raw_row.values()):
                    raise _error(line_number, std_code, "CSV row must contain exactly 9 fields")

                # Parse explicit CSV tokens before strict model validation: invalid
                # booleans and numbers must never be silently coerced or discarded.
                data = dict(raw_row)
                if raw_row["normal_sign"] not in {"1", "-1"}:
                    raise _error(line_number, std_code, "normal_sign must be 1 or -1")
                data["normal_sign"] = int(raw_row["normal_sign"])
                for column in _BOOLEAN_COLUMNS:
                    if raw_row[column] not in {"TRUE", "FALSE"}:
                        raise _error(line_number, std_code, f"{column} must be TRUE or FALSE")
                    data[column] = raw_row[column] == "TRUE"
                try:
                    row = StdAccountRow.model_validate(data)
                except ValidationError as exc:
                    raise _error(line_number, std_code, str(exc)) from exc
                row._csv_line_number = line_number
                rows.append(row)
    except (csv.Error, UnicodeError) as exc:
        raise _error(line_number, std_code, f"invalid CSV: {exc}") from exc

    _validate_file_rules(rows)
    return rows


def write_std_accounts(conn: duckdb.DuckDBPyConnection, rows: list[StdAccountRow]) -> None:
    """Insert new codes atomically, rejecting any changes to stored definitions."""
    _validate_file_rules(rows)
    columns_sql = ", ".join(_COLUMNS)
    conn.execute("BEGIN TRANSACTION")
    try:
        existing = {
            stored[0]: stored
            for stored in conn.execute(f"SELECT {columns_sql} FROM std_account").fetchall()
        }
        additions = []
        for index, row in enumerate(rows, start=2):
            values = tuple(getattr(row, column) for column in _COLUMNS)
            if row.std_code not in existing:
                additions.append(values)
            elif existing[row.std_code] != values:
                raise _error(
                    row._csv_line_number or index,
                    row.std_code,
                    f"taxonomy drift: {row.std_code}",
                )

        # Check every existing definition before inserting any new definition.
        if additions:
            placeholders = ", ".join("?" for _ in _COLUMNS)
            conn.executemany(
                f"INSERT INTO std_account ({columns_sql}) VALUES ({placeholders})", additions
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
