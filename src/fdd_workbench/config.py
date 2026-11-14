"""Reserved for paths, default thresholds, and header alias configuration."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TAXONOMY_DIR = REPO_ROOT / "taxonomy"
STD_ACCOUNTS_CSV = TAXONOMY_DIR / "std_accounts.csv"
