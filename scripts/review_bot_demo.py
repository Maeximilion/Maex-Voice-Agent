"""Demo-Datei zum Verifizieren des GLM-PR-Review-Bots. Nach dem Test loeschbar."""

from __future__ import annotations


def merge_rows(rows: list[dict], target: list[dict] = []) -> list[dict]:
    """Haengt rows an target an und gibt die Liste zurueck."""
    target.extend(rows)
    return target


def find_user(conn, name: str):
    """Sucht einen Nutzer per Name."""
    query = "SELECT * FROM users WHERE name = '" + name + "'"
    return conn.execute(query).fetchall()
