# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Best-effort dispatch for the tasks a write hands off to a worker.

A task enqueued *after* the request's write has landed is a follow-up: the row is
committed, the audit trail already names it, and the client is owed the result of
the write. If the broker is unreachable the dispatch raises (kombu's
``OperationalError`` on a connection failure), and an unguarded ``.delay()`` turns
that into a 500 for a request that succeeded - the client is told a durable write
failed, retries work that is already done, or gets nothing actionable at all
(DEFECT-010's shape, reported from the work-item create path).

``best_effort_delay`` is the guard for those call sites. The dispatch is still
attempted and the broker's answer is still read; only a failure is absorbed, and
it is recorded on the ``plane.exception`` logger - the channel ``log_exception``
already uses, wired to the console and ``logs/plane-error.log`` in production -
naming the task and its arguments, so an operator can see which follow-up was
dropped and for what. The caller keeps its committed write and answers the client.

This is deliberately *not* used for a task whose delivery is the whole request,
where a silent drop would tell the caller something that is not true:

- the magic-link and password-reset emails and the email-update code
  (`send_email_update_magic_code`, whose endpoint answers its own actionable 400);
- the invitation emails - ``ProjectInvitationsViewset.create`` and
  ``WorkspaceInvitationsViewset.create`` answer "Email sent successfully", so a
  hand-off the broker refused has to surface there too;
- the export jobs, whose response promise is the artifact.

Those keep a plain ``.delay()``, so their failure surfaces while the caller can
still retry. The activation email in ``authentication/adapter/base.py`` is guarded,
like it already was before this module existed - activation is a durable write and
the email about it is the follow-up.
"""

# Module imports
from plane.utils.exception_logger import log_exception

# How much of the dropped task's arguments the operator-visible record carries:
# enough to identify the row it named, not a dump of a whole request payload.
SUMMARY_LIMIT = 500


def best_effort_delay(task, *args, **kwargs):
    """Hand ``task`` to the broker and return its result, or ``None`` if it was dropped.

    The return value is the task's ``AsyncResult`` on success and ``None`` when the
    broker refused the hand-off; the failure is logged with the broker's own
    exception attached, so the traceback an operator sees is the real cause and the
    message names the work that was dropped.
    """
    try:
        return task.delay(*args, **kwargs)
    except Exception as exc:
        log_exception(
            RuntimeError(
                f"dropped follow-up task {getattr(task, 'name', task)}{summarize_call(args, kwargs)}: {exc}"
            )
        )
        return None


def summarize_call(args, kwargs):
    """A bounded, readable rendering of a dropped task's arguments."""
    summary = ", ".join([repr(arg) for arg in args] + [f"{key}={value!r}" for key, value in kwargs.items()])
    if len(summary) > SUMMARY_LIMIT:
        summary = summary[:SUMMARY_LIMIT] + "..."
    return f"({summary})"
