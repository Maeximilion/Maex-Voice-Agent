"""The recording of a call as the database knows it (legal check L2, T-11.3).

The conversation core asks and decides (`api/agent/recording.py`); this writes
the row that names the file, and the line writes the file: `voice` for a call the
AI takes (`telephony/recording.py`), Asterisk for a team leg in mode `shadow`
(`deploy/asterisk/conf/extensions.conf`, which builds the same name). The row is
committed before a byte is recorded, so no file exists that the deletion job
cannot find (`jobs/retention.py`).
"""

import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from api.core.time import utcnow
from api.models import Call, Recording

# The volume `voice`, Asterisk and the dispatcher share (docker-compose.yml). A
# path and no setting: the dialplan names it too.
FOLDER = Path("/recordings")


def path_of(file_name: str) -> Path:
    """Inside the folder whatever the row says."""
    return FOLDER / Path(file_name).name


def delete_file(file_name: str) -> None:
    """Gone for good; a file that was never written or is gone already is no
    error. Raises `OSError` when it stays, also in a process without the volume
    (`python -m api.jobs.retention` in `api`): its row must not go for a file
    this process cannot see."""
    if not FOLDER.is_dir():
        raise NotADirectoryError(str(FOLDER))
    path_of(file_name).unlink(missing_ok=True)


def open_recording(
    session: Session,
    tenant_id: uuid.UUID,
    call_id: uuid.UUID,
    now: datetime | None = None,
) -> str:
    """Writes the row and commits; returns the name of the file. By the id of
    the call on the line, as the dialplan knows it (`${UNIQUEID}`)."""
    call = session.get_one(Call, call_id)
    file_name = f"{call.external_session_id}.wav"
    session.add(
        Recording(
            tenant_id=tenant_id,
            call_id=call_id,
            file_name=file_name,
            started_at=now or utcnow(),
        )
    )
    session.commit()
    return file_name


def withdraw_recording(
    session: Session,
    tenant_id: uuid.UUID,
    call_id: uuid.UUID,
    now: datetime | None = None,
) -> None:
    """The next pass of the deletion job takes the row, and the file if it is
    still there. Commits."""
    session.execute(
        update(Recording)
        .where(
            Recording.tenant_id == tenant_id,
            Recording.call_id == call_id,
            Recording.withdrawn_at.is_(None),
        )
        .values(withdrawn_at=now or utcnow())
    )
    session.commit()


def call_with_running_recording(
    session: Session, tenant_id: uuid.UUID, session_id: str
) -> uuid.UUID | None:
    """The call on the line under `session_id`, if it has a recording that was
    not withdrawn."""
    return session.scalar(
        select(Call.id)
        .join(Recording, Recording.call_id == Call.id)
        .where(
            Call.tenant_id == tenant_id,
            Call.external_session_id == session_id,
            Recording.withdrawn_at.is_(None),
        )
    )
