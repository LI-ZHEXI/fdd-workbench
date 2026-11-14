"""Taxonomy conventions, validation, and exact signed-balance calculations."""

import csv
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from tests.conftest import seed_minimal_context

from fdd_workbench.config import STD_ACCOUNTS_CSV
from fdd_workbench.mapping.taxonomy import TaxonomyError, load_std_accounts, write_std_accounts


def test_load_std_accounts_has_unique_expected_rows() -> None:
    """All 67 CSV data rows load once, with the expected statement split."""
    with STD_ACCOUNTS_CSV.open(encoding="utf-8", newline="") as source:
        csv_rows = list(csv.DictReader(source))
    rows = load_std_accounts(STD_ACCOUNTS_CSV)
    assert len(rows) == len(csv_rows) == 67
    assert len({row.std_code for row in rows}) == 67
    assert sum(row.statement == "IS" for row in rows) == 24
    assert sum(row.statement == "BS" for row in rows) == 43


def test_store_taxonomy_rejects_drift_and_adds_rows_idempotently(
    seeded_store: duckdb.DuckDBPyConnection,
) -> None:
    """Taxonomy drift cannot partially insert rows; valid additions insert once."""
    assert seeded_store.execute("SELECT count(*) FROM std_account").fetchone() == (67,)
    existing = load_std_accounts(STD_ACCOUNTS_CSV)[0]
    added = existing.model_copy(update={"std_code": "IS.REV.TEST"})
    drifted = existing.model_copy(update={"name": "Unexpected changed name"})
    with pytest.raises(TaxonomyError) as caught:
        write_std_accounts(seeded_store, [added, drifted])
    assert f"taxonomy drift: {existing.std_code}" in str(caught.value)
    assert seeded_store.execute("SELECT count(*) FROM std_account").fetchone() == (67,)
    assert (
        seeded_store.execute(
            "SELECT std_code FROM std_account WHERE std_code = ?", [added.std_code]
        ).fetchone()
        is None
    )
    assert seeded_store.execute(
        "SELECT name FROM std_account WHERE std_code = ?", [existing.std_code]
    ).fetchone() == (existing.name,)

    write_std_accounts(seeded_store, [added])
    write_std_accounts(seeded_store, [added])
    assert seeded_store.execute("SELECT count(*) FROM std_account").fetchone() == (68,)
    assert seeded_store.execute(
        "SELECT count(*) FROM std_account WHERE std_code = ?", [added.std_code]
    ).fetchone() == (1,)


def test_taxonomy_business_conventions() -> None:
    """Component flags preserve the explicitly documented FDD conventions."""
    accounts = {row.std_code: row for row in load_std_accounts(STD_ACCOUNTS_CSV)}
    assert accounts["BS.CASH.UNRESTRICTED"].is_net_debt_component is True
    assert accounts["BS.CASH.RESTRICTED"].is_net_debt_component is False
    assert accounts["IS.OPEX.CREDIT_LOSS"].is_ebitda_component is True
    assert accounts["IS.NONOP.ASSET_IMPAIRMENT"].is_ebitda_component is False
    assert accounts["BS.NWC.NOTES_PAYABLE"].category == "nwc_ap"
    assert accounts["BS.NWC.NOTES_PAYABLE"].is_nwc_component is True
    for std_code in ("BS.EQUITY.CURRENT_YEAR_PROFIT", "BS.EQUITY.PROFIT_DISTRIBUTION"):
        row = accounts[std_code]
        assert row.category == "equity"
        assert row.is_ebitda_component is False
        assert row.is_nwc_component is False
        assert row.is_net_debt_component is False
    for std_code in ("IS.DA.DEPRECIATION", "IS.DA.AMORTIZATION"):
        assert accounts[std_code].is_ebitda_component is False


def test_contra_accounts_share_parent_category_and_sign() -> None:
    """Contra balances offset their parent under the same category multiplier."""
    accounts = {row.std_code: row for row in load_std_accounts(STD_ACCOUNTS_CSV)}
    contra_pairs = [
        ("BS.NWC.AR_PROVISION", "BS.NWC.AR", 1),
        ("BS.NWC.INVENTORY_PROVISION", "BS.NWC.INVENTORY", 1),
        ("BS.NCA.ACC_DEPRECIATION", "BS.NCA.PPE", 1),
        ("BS.NCA.ACC_AMORTIZATION", "BS.NCA.INTANGIBLES", 1),
        ("IS.REV.RETURNS", "IS.REV.MAIN", -1),
    ]
    for contra_code, parent_code, normal_sign in contra_pairs:
        contra, parent = accounts[contra_code], accounts[parent_code]
        assert contra.normal_sign == parent.normal_sign == normal_sign
        assert contra.category == parent.category


def test_load_std_accounts_rejects_invalid_contra_sign_and_da_flag(tmp_path: Path) -> None:
    """Tampered CSVs identify the offending line and standard account code."""
    with STD_ACCOUNTS_CSV.open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        fieldnames = reader.fieldnames
        original_rows = list(reader)
    assert fieldnames is not None
    cases = [
        ("BS.NWC.AR_PROVISION", "normal_sign", "-1", "nwc_ar"),
        ("IS.DA.DEPRECIATION", "is_ebitda_component", "TRUE", "da"),
    ]
    for std_code, field, invalid_value, error_detail in cases:
        rows = [dict(row) for row in original_rows]
        row_index = next(index for index, row in enumerate(rows) if row["std_code"] == std_code)
        rows[row_index][field] = invalid_value
        csv_path = tmp_path / f"{std_code}.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as destination:
            writer = csv.DictWriter(destination, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        with pytest.raises(TaxonomyError) as caught:
            load_std_accounts(csv_path)
        message = str(caught.value)
        assert std_code in message
        assert str(row_index + 2) in message
        assert error_detail in message


def test_real_schema_contra_balances_produce_known_decimal_totals(
    seeded_store: duckdb.DuckDBPyConnection,
) -> None:
    """Real FK-backed mappings produce net AR 90 and net revenue 950 exactly."""
    entity_id, period_id, file_id = seed_minimal_context(seeded_store)
    accounts = [
        ("1122", "应收账款", "BS.NWC.AR", Decimal("100.0000")),
        ("1231", "坏账准备", "BS.NWC.AR_PROVISION", Decimal("-10.0000")),
        ("6001", "主营业务收入", "IS.REV.MAIN", Decimal("-1000.0000")),
        ("6001.01", "销售退回", "IS.REV.RETURNS", Decimal("50.0000")),
    ]
    for row_no, (local_code, local_name, std_code, closing) in enumerate(accounts, start=1):
        seeded_store.execute(
            "INSERT INTO coa_local (entity_id, local_code, local_name) VALUES (?, ?, ?)",
            [entity_id, local_code, local_name],
        )
        seeded_store.execute(
            """
            INSERT INTO coa_map (map_id, entity_id, local_code, std_code, reviewer)
            VALUES (?, ?, ?, ?, 'test')
            """,
            [f"MAP_{row_no}", entity_id, local_code, std_code],
        )
        seeded_store.execute(
            """
            INSERT INTO trial_balance (
                tb_id, entity_id, period_id, local_code, closing, is_pre_closing, file_id, row_no
            ) VALUES (?, ?, ?, ?, ?, TRUE, ?, ?)
            """,
            [f"TB_{row_no}", entity_id, period_id, local_code, closing, file_id, row_no],
        )
    signed_total = """
        SELECT SUM(tb.closing * sa.normal_sign)
        FROM trial_balance tb
        JOIN coa_map cm ON cm.entity_id = tb.entity_id AND cm.local_code = tb.local_code
        JOIN std_account sa ON sa.std_code = cm.std_code
        WHERE sa.category = ?
    """
    ar_result = seeded_store.execute(signed_total, ["nwc_ar"]).fetchone()
    revenue_result = seeded_store.execute(signed_total, ["revenue"]).fetchone()
    assert ar_result is not None
    assert revenue_result is not None
    assert isinstance(ar_result[0], Decimal)
    assert isinstance(revenue_result[0], Decimal)
    assert ar_result[0] == Decimal("90.0000")
    assert revenue_result[0] == Decimal("950.0000")
