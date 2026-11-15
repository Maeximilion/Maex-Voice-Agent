"""Probe (review #207, area G): the orphan pass of #734 never deletes outside
the recordings folder: a symlink to an outside file loses the link only, a
subfolder is left alone, and the purge still counts sensibly."""

import os

from api.jobs.retention import purge
from api.tests.test_jobs_retention import (  # noqa: F401
    DAY,
    NOW,
    folder,
    session,
    tenant_id,
)


def _old(path):
    at = (NOW - 40 * DAY).timestamp()
    os.utime(path, (at, at), follow_symlinks=False)


def test_a_link_out_of_the_folder_takes_nothing_outside(
    session, tenant_id, folder, tmp_path_factory
):
    outside_dir = tmp_path_factory.mktemp("outside")
    outside = outside_dir / "precious.txt"
    outside.write_text("keep")
    _old(outside)
    link = folder / "link.wav"
    link.symlink_to(outside)
    _old(link)
    sub = folder / "sub"
    sub.mkdir()
    inner = sub / "inner.wav"
    inner.write_bytes(b"x")
    _old(inner)
    dangling = folder / "dangling.wav"
    dangling.symlink_to(outside_dir / "missing")

    purge(session, NOW)

    assert outside.read_text() == "keep"
    assert not link.is_symlink()  # the link goes, its target stays
    assert inner.exists()
    assert dangling.is_symlink()  # is_file() false: left alone, no crash
