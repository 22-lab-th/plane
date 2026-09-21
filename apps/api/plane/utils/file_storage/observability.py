# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Structured records and counters for the project-file lifecycle (AC-31, R-NFR-5).

Two things live here and nothing else does, so every mutation path states its
event in one vocabulary instead of inventing its own log line.

**Records.** :func:`record` writes one log line to the ``plane.files`` logger
whose *structured* payload is attached as ``extra={"file_record": {...}}``. That
payload always carries the five contract keys R-NFR-5 asks for - ``event``,
``outcome``, ``workspace_id``, ``project_id``, ``file_id``, ``version_no`` - plus
whatever evidence the call site has. The contract keys are ordinary keyword
parameters, so a call site cannot shadow them: a duplicate keyword is a
``TypeError`` at the call, not a silently rewritten record. The human-readable
message is ``"<event> outcome=<outcome>"``; a collector reads the payload, a
person reads the line. Identifiers are stringified here (a
``UUID`` would not survive a JSON formatter) and ``None`` is kept for a field the
call site genuinely does not have, so a parser can tell "not applicable" from
"unknown".

The payload survives the deployment's formatter because it is a plain
``LogRecord`` attribute: production/local already configure
``pythonjsonlogger.json.JsonFormatter``, which serialises ``file_record`` as a
nested object. The logger itself is registered at ``INFO`` in
``plane/settings/local.py`` and ``plane/settings/production.py`` next to its
siblings; an unregistered logger would inherit the root level (``WARNING``) and
every record below would be silently dropped, which is the one failure this
module cannot detect for itself.

Three events in this phase: ``file.presign``, ``file.finalize`` and
``file.delete``. The outcome vocabulary of each is closed and documented next to
its constants, because "an outcome" that means whatever a call site felt like
saying is not an observable contract.

**Counters.** :func:`increment` moves one of the four counters R-NFR-5 names and
emits a ``file.counter`` record carrying the counter's name, its new value and the
context the increment was made with. The registry itself is per process and
in-memory: this phase has no metrics backend wired for the file domain (the only
OTLP plumbing in the repository is the instance-metrics task, which reports
licence telemetry rather than per-request counters), so **the log stream is the
transport**. Every increment is therefore visible to whatever collector the
deployment configures, and :func:`snapshot` exists for the tests and for a process
that wants to expose the numbers on its own health route. Two consequences are
stated rather than hidden: the value is per worker (a four-worker deployment has
four, and only the log stream adds them up), and it is not transactional - an
increment is made once the row change it describes has committed, so a crash
between the two loses the count while the audit row (the record of truth for a
single event) is already written.

The operator-facing companion to this module is
``docs/observability/project-file-lifecycle.md``: the events and outcomes, what each
counter counts, how to consume ``file_counter`` lines without sampling
``value`` as a gauge, and the scope of the presign storage-call guard.
"""

# Python imports
import logging
import threading
import uuid

#: The logger every file record and counter line is written to. One name for the
#: whole domain: a collector enables ``plane.files`` and has everything.
LOGGER_NAME = "plane.files"

#: Event ids. The value is the ``event`` field of the record.
EVENT_PRESIGN = "file.presign"
EVENT_FINALIZE = "file.finalize"
EVENT_DELETE = "file.delete"

#: ``file.presign`` outcomes: a URL was signed, or the provider could not sign one
#: and the attempt stays uploading for the sweep. A request refused *before* signing
#: (quota, permission, validation) is not a presign outcome: it never reached the
#: signer, and the quota case is carried by ``QUOTA_REJECTIONS`` and the audit row.
PRESIGN_SIGNED = "signed"
PRESIGN_STORAGE_UNAVAILABLE = "storage_unavailable"

#: ``file.finalize`` outcomes: the attempt became the active version, was stored as
#: a revision that did not displace it, failed verification, or the request found
#: the attempt already settled and replayed that stored result (R-NFR-6).
FINALIZE_ACTIVATED = "activated"
FINALIZE_SUPERSEDED = "superseded"
FINALIZE_FAILED = "failed"
FINALIZE_REPLAYED = "replayed"

#: ``file.delete`` outcomes: every object of the file is gone and the row was
#: removed, or an object survived and the file stays purgeable for the next attempt.
#: ``purged`` carries ``version_nos`` (the versions it removed) as well as
#: ``version_no``, because the latter is the file's **active pointer** and a file
#: whose only attempt was never finalised has none (``current_version_no == 0``), so
#: that one field can be ``null`` for a purge that certainly did remove a version.
DELETE_PURGED = "purged"
DELETE_PURGE_FAILED = "purge_failed"

#: The counters R-NFR-5 names.
UPLOAD_FAILURES = "upload_failures"
VERIFICATION_MISMATCHES = "verification_mismatches"
QUOTA_REJECTIONS = "quota_rejections"
SWEEP_DELETIONS = "sweep_deletions"

COUNTER_NAMES = (UPLOAD_FAILURES, VERIFICATION_MISMATCHES, QUOTA_REJECTIONS, SWEEP_DELETIONS)

#: The finalize rejection codes in which the object's **observed** properties
#: contradicted the declaration. ``object_missing`` (there is nothing to verify) and
#: ``verification_failed`` (the object's first bytes could not be read) fail an
#: upload but are not mismatches, so they move ``UPLOAD_FAILURES`` alone.
MISMATCH_CODES = frozenset({"size_mismatch", "mime_mismatch"})

#: The contract keys of a record, in the order they are written.
RECORD_KEYS = ("event", "outcome", "workspace_id", "project_id", "file_id", "version_no")

logger = logging.getLogger(LOGGER_NAME)

_lock = threading.Lock()
_counts = {name: 0 for name in COUNTER_NAMES}


def _identifier(value):
    """Return a JSON-safe identifier, or ``None`` when the call site has none."""
    if value is None:
        return None

    return str(value)


def _field(value):
    """Return a log-serialisable field: an identifier object becomes its string.

    The call sites hand over the objects they have (a ``UUID`` on a model
    instance) and the transport is a JSON formatter, so the conversion happens
    here once rather than at every call site.
    """
    return str(value) if isinstance(value, uuid.UUID) else value


def _version_no(value):
    """Return an integer version number, or ``None`` when the call site has none."""
    if value is None:
        return None

    return int(value)


def record(
    event,
    *,
    outcome,
    workspace_id=None,
    project_id=None,
    file_id=None,
    version_no=None,
    **fields,
):
    """Emit one structured lifecycle record; return the payload that was logged.

    :param event: one of the ``EVENT_*`` ids.
    :param outcome: one of that event's documented outcomes.
    :param fields: the evidence this call site has (``code``, ``object_key``,
        ``size_bytes``, …). Values must be log-serialisable scalars; the five
        contract keys are written after them and therefore always win.
    """
    payload = {key: _field(value) for key, value in fields.items()}
    payload["event"] = event
    payload["outcome"] = outcome
    payload["workspace_id"] = _identifier(workspace_id)
    payload["project_id"] = _identifier(project_id)
    payload["file_id"] = _identifier(file_id)
    payload["version_no"] = _version_no(version_no)

    logger.info("%s outcome=%s", event, outcome, extra={"file_record": payload})
    return payload


def increment(counter, **fields):
    """Move ``counter`` by one and emit the counter record; return its new value.

    Every increment is logged, so the number is observable outside this process
    even though the registry is per process. An unknown counter name is a
    programming error and raises rather than silently creating a new series.
    """
    if counter not in _counts:
        raise KeyError(f"{counter!r} is not a declared file counter: {COUNTER_NAMES}")

    with _lock:
        _counts[counter] += 1
        value = _counts[counter]

    payload = {key: _field(item) for key, item in fields.items()}
    payload["counter"] = counter
    payload["value"] = value
    logger.info("file.counter %s=%d", counter, value, extra={"file_counter": payload})
    return value


def snapshot():
    """Return a copy of the four counters, as a collector would read them."""
    with _lock:
        return dict(_counts)


def reset():
    """Return the counters to zero. Called by a test that asserts a delta, and by
    an operator who has scraped the value and wants the next window to start clean."""
    with _lock:
        for name in COUNTER_NAMES:
            _counts[name] = 0
