# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`folder_conflict` maps a database integrity error onto a stable code (T-105 F-3).

No database and no HTTP: the failure is constructed, so it belongs with the other
unit tests. It lived in ``tests/contract/app/test_file_folders.py`` marked ``unit``
until the T-106 verification pointed out that a ``-m contract`` run over that file
silently skipped it - the marker was right, the location was not.
"""

# Django imports
from django.db import IntegrityError

# Third party imports
import pytest

# Module imports
from plane.app.views.file.folders import NAME_CONSTRAINT_NAME, folder_conflict


@pytest.mark.unit
class TestFolderConflictMapping:
    """F-3: an integrity error is reported as the constraint it actually hit."""

    @staticmethod
    def _integrity_error(constraint_name):
        class _Diagnostics:
            pass

        class _Cause(Exception):
            pass

        cause = _Cause()
        cause.diag = _Diagnostics()
        cause.diag.constraint_name = constraint_name

        try:
            raise IntegrityError("boom") from cause
        except IntegrityError as exc:
            return exc

    def test_the_name_constraint_maps_to_a_name_conflict(self):
        error = folder_conflict(self._integrity_error(NAME_CONSTRAINT_NAME), name_normalized="specs")

        assert error.code == "folder_name_conflict"
        assert error.status_code == 409
        assert error.details["name_normalized"] == "specs"

    def test_a_depth_constraint_maps_to_the_depth_code(self):
        error = folder_conflict(self._integrity_error("file_folders_depth_check"), name_normalized="specs")

        assert error.code == "depth_limit_exceeded"
        assert error.status_code == 409

    def test_an_unidentified_constraint_maps_to_the_generic_code(self):
        error = folder_conflict(self._integrity_error("some_other_constraint"), name_normalized="specs")

        assert error.code == "folder_conflict"
        assert error.status_code == 409
        assert error.details["constraint"] == "some_other_constraint"

    def test_a_missing_constraint_name_is_still_reported(self):
        error = folder_conflict(IntegrityError("boom"), name_normalized="specs")

        assert error.code == "folder_conflict"
