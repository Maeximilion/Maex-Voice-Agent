"""scripts/kasse_to_csv.py: which file of the input folder is read."""

import pytest

from api.domain.menu.pos_dbf import DbfError
from scripts.kasse_to_csv import _find


def test_a_file_is_found_in_any_spelling(tmp_path):
    (tmp_path / "ARTIKEL.DBF").write_bytes(b"x")

    assert _find(tmp_path, "artikel.dbf").name == "ARTIKEL.DBF"


def test_a_missing_file_is_named(tmp_path):
    with pytest.raises(FileNotFoundError, match=r"artikel\.dbf"):
        _find(tmp_path, "artikel.dbf")


def test_two_spellings_of_one_file_are_refused(tmp_path):
    """Codex PR #149: on a case-sensitive file system whichever came first was
    read, and table and memo are looked up apart, so an old table could meet a
    newer memo."""
    (tmp_path / "artikel.DBF").write_bytes(b"old")
    (tmp_path / "artikel.dbf").write_bytes(b"new")

    with pytest.raises(DbfError, match=r"artikel\.DBF, artikel\.dbf"):
        _find(tmp_path, "artikel.dbf")
