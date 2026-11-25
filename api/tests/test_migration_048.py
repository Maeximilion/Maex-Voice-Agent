"""Migration 048: the table `eval_runs`, one row per eval run (docs/08 §3,
T-5.1), up and down, twice up does no harm, and its checks hold."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config

COLUMNS = {
    "id",
    "at",
    "model",
    "prompt_version",
    "suite",
    "tags",
    "passed",
    "failed",
    "accuracy",
    "verdict",
    "details",
    "git_sha",
}

INSERT = text(
    "INSERT INTO eval_runs (at, model, prompt_version, suite, passed, failed, "
    "accuracy, verdict, details) VALUES (now(), 'scripted', 'v3', 'cases', "
    ":passed, :failed, :accuracy, :verdict, '{}')"
)


@pytest.fixture
def engine(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "047")
    engine = create_engine(scratch_db_url)
    yield engine
    engine.dispose()


def _tables(engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def test_up_adds_the_table_and_down_drops_it(engine, scratch_db_url):
    assert "eval_runs" not in _tables(engine)

    command.upgrade(_config(scratch_db_url), "048")
    columns = {c["name"] for c in inspect(engine).get_columns("eval_runs")}
    assert columns == COLUMNS

    command.downgrade(_config(scratch_db_url), "047")
    assert "eval_runs" not in _tables(engine)


def test_down_and_up_again_gives_the_same_table(engine, scratch_db_url):
    command.upgrade(_config(scratch_db_url), "048")
    command.downgrade(_config(scratch_db_url), "047")
    command.upgrade(_config(scratch_db_url), "048")
    columns = {c["name"] for c in inspect(engine).get_columns("eval_runs")}
    assert columns == COLUMNS


def test_a_valid_row_goes_in_with_its_defaults(engine, scratch_db_url):
    command.upgrade(_config(scratch_db_url), "048")
    with engine.begin() as conn:
        conn.execute(
            INSERT,
            {"passed": 3, "failed": 1, "accuracy": 0.75, "verdict": "bestanden"},
        )
        row = conn.execute(text("SELECT id, tags, git_sha FROM eval_runs")).one()
    assert row.id is not None
    assert row.tags == []
    assert row.git_sha is None


@pytest.mark.parametrize(
    "values",
    [
        {"passed": -1, "failed": 0, "accuracy": 0, "verdict": "bestanden"},
        {"passed": 0, "failed": -1, "accuracy": 0, "verdict": "bestanden"},
        {"passed": 1, "failed": 0, "accuracy": 1.5, "verdict": "bestanden"},
        {"passed": 1, "failed": 0, "accuracy": -0.1, "verdict": "bestanden"},
        {"passed": 1, "failed": 0, "accuracy": 1, "verdict": "gruen"},
    ],
    ids=["passed", "failed", "accuracy_high", "accuracy_low", "verdict"],
)
def test_the_checks_refuse_a_broken_row(engine, scratch_db_url, values):
    command.upgrade(_config(scratch_db_url), "048")
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(INSERT, values)
