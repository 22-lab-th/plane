# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The recorded performance benchmarks (AC-29, R-NFR-1; AC-30, R-NFR-2; T-120).

These are benchmarks, not smoke tests, and they are written so they can **fail**:

* the thresholds are the numbers the requirements state - 400 ms p95 for a filtered
  list over 10 000 seeded rows (R-NFR-1) and 200 ms p95 for a presign (R-NFR-2) -
  not a value fitted to whatever this machine happened to measure;
* the sample count is fixed and the warm-up runs are named and discarded, so the
  p95 means something. A p95 over N samples is the nearest-rank order statistic
  ``ceil(0.95 * N)``: at N = 40 it is the third-worst sample, i.e. deliberately a
  pessimistic estimate, and one outlier cannot land below it silently;
* the guard for AC-30 is a **call counter at the botocore seam**, not a timing
  inference, and one test here injects a call to prove the guard reports it. A test
  that only timed the endpoint would pass even if it read the object first.

Each benchmark prints one machine-readable ``BENCHMARK {...}`` line, so the numbers
in the evidence record are the ones the run measured rather than a number someone
remembered. Thresholds are absolute milliseconds: a machine slower than this one
will produce larger samples and, at some point, a failing test. That is intended -
the requirement is a latency budget, and the honest statement is the measured
headroom, which is reported in the ticket rather than hidden inside the threshold.
For scale, the recorded run measured a list p95 of ~34 ms against the 400 ms budget
(≈12× headroom) and a presign p95 of ~26 ms against the 200 ms budget (≈8×), so the
assertions tolerate a machine roughly an order of magnitude slower at the same load
before they fail.

Cold-start behaviour is the reason for the warm-up runs: the first request
materialises the project's quota rows (ARCH-001 §2.8, N-02a) and pays for any
lazy import of the request path, and neither is part of steady-state latency.

The seeded list benchmark also runs ``ANALYZE`` on the two tables it fills, because
without it the planner mis-plans the listing's visibility ``EXISTS`` and the same
request measures 3790 ms instead of ~30 ms - a property of the seed, not of the
endpoint (see :func:`seed_files`, where both plans are recorded). The presign
benchmark takes the ``project_file_upload`` throttle out of the measurement: 33
samples per declared size against one project would otherwise fail with a 429 at
sample 22, and the throttle is a product behaviour tested elsewhere (AC-22).
"""

# Python imports
import json
import math
import time
from unittest import mock
from uuid import uuid4

# Django imports
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

# Third party imports
import botocore.client
import pytest
from rest_framework import status

# Module imports
from plane.db.models import FileObject, FileVersion, Project, ProjectMember, Workspace, WorkspaceMember
from plane.settings.storage import S3Storage
from plane.throttles.project_file import ProjectFileUploadThrottle

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"

#: Rows the list benchmark seeds (AC-29 names the number).
LIST_ROWS = 10_000

#: R-NFR-1 / AC-29: the filtered list's server-side p95 budget in milliseconds.
LIST_THRESHOLD_MS = 400.0

#: R-NFR-2 / AC-30: the presign p95 budget in milliseconds.
PRESIGN_THRESHOLD_MS = 200.0

#: The query parameters the list benchmark uses: a text filter, a mime filter, an
#: explicit ordering and a page, i.e. the filtered listing a client actually asks for.
LIST_QUERY = {"q": "Row", "mime": "application/pdf", "ordering": "-created", "page_size": 50}

#: Declared sizes the presign benchmark runs at. ``PROJECT_FILE_MAX_BYTES`` is
#: 26 214 400 (25 MiB) in the test settings, so the largest declaration is the
#: largest one the endpoint accepts - the declaration is metadata, so none of these
#: bytes are ever uploaded.
PRESIGN_SIZES = (1024, 1024 * 1024, 25 * 1024 * 1024)

#: Measured samples per benchmark. 40 gives a p95 at the third-worst sample and
#: keeps the run affordable (see the ticket's timing note).
LIST_SAMPLES = 40
PRESIGN_SAMPLES = 30

#: Discarded warm-up requests per benchmark, before the measured samples.
WARMUP = 3


def percentile(samples, fraction):
    """Nearest-rank percentile of ``samples``: the ``ceil(fraction * n)``-th value."""
    ordered = sorted(samples)
    rank = max(math.ceil(fraction * len(ordered)) - 1, 0)
    return ordered[rank]


def report(name, **numbers):
    """Print one machine-readable benchmark line."""
    print(f"BENCHMARK {json.dumps({'benchmark': name, **numbers}, sort_keys=True)}")


def timed_client_call(client, url, query=None):
    """Return ``(elapsed_ms, response)`` for one request through the test client."""
    started = time.perf_counter()
    response = client.get(url, query or {})
    elapsed_ms = (time.perf_counter() - started) * 1000
    return elapsed_ms, response


def assert_no_storage_calls(calls):
    """The AC-30 guard: no object call may happen while the endpoint is refusing to."""
    assert calls == [], f"the presign request called the storage API: {calls}"


class StorageCallRecorder:
    """Counts every S3 API call, at the seam every call passes through.

    ``botocore.client.BaseClient._make_api_call`` is the one method boto3's generated
    clients use to reach the API, so a wrapper here sees HEAD/GET/PUT/DELETE/LIST
    regardless of which adapter method issued them. Presigning is *not* an API call:
    ``generate_presigned_url`` signs locally and never reaches this method, which is
    exactly what makes the counter able to tell "signed" from "looked first".
    """

    def __init__(self):
        self.calls = []

    def __enter__(self):
        real = botocore.client.BaseClient._make_api_call

        def counting(client, operation_name, api_params):
            self.calls.append(operation_name)
            return real(client, operation_name, api_params)

        self._patch = mock.patch.object(botocore.client.BaseClient, "_make_api_call", counting)
        self._patch.start()
        return self

    def __exit__(self, *exc_info):
        self._patch.stop()
        return False


@pytest.fixture(autouse=True)
def storage_environment(monkeypatch):
    monkeypatch.setenv("MINIO_PUBLIC_ENDPOINT_URL", "http://test-minio:9000")
    monkeypatch.setenv("AWS_S3_ENDPOINT_URL", "http://test-minio:9000")


@pytest.fixture(autouse=True)
def sample_past_the_upload_throttle():
    """Take the presign rate limit out of the measurement.

    ``project_file_upload`` is 60/minute keyed by user + project, and the presign
    benchmark makes 33 requests per declared size against one project: the first run
    of this file failed at sample 22 with a 429. The limit is a product behaviour
    tested elsewhere (AC-22); what is being measured here is the endpoint's latency,
    so the bucket is emptied and the rate lifted for the duration.
    """
    cache.clear()
    with mock.patch.object(ProjectFileUploadThrottle, "rate", "10000/minute", create=True):
        yield
    cache.clear()


@pytest.fixture
def project(create_user):
    workspace = Workspace.objects.create(name="Benchmark Workspace", slug="benchmark-workspace", owner=create_user)
    WorkspaceMember.objects.create(workspace=workspace, member=create_user, role=20, is_active=True)
    project = Project.objects.create(name="Benchmark Project", identifier="BENM", workspace=workspace)
    ProjectMember.objects.create(
        project=project, member=create_user, workspace=workspace, role=20, is_active=True
    )
    return project


def files_url(slug, project_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/files/"


def upload_url(slug, project_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/files/initiate-upload/"


def seed_files(project, rows):
    """Seed ``rows`` listed files, in the shape the listing rule requires (AC-20).

    Two bulk inserts rather than 2 × rows saves: each file carries the verified,
    active version without which the listing would hide it, so the benchmark
    measures the real filtered query over real rows instead of a query that matches
    nothing.

    ``ANALYZE`` afterwards is part of the seed, not a convenience. A bulk load leaves
    the planner's statistics describing the table as it was *before* the load, and it
    then mis-plans the listing's visibility ``EXISTS`` as a nested loop instead of a
    hash semi-join. That was measured on this stack, on the same data and the same
    request: **3790 ms** before ``ANALYZE`` and **28–37 ms** after, with the plan
    changing from a per-row probe to a hash semi-join (both plans are in the ticket's
    evidence). A benchmark that skipped this would measure a statistics artifact of
    its own seeding and report the endpoint as an order of magnitude slower than it
    is; a deployment reaches the second plan on its own, because its rows were written
    long before they are listed.
    """
    stamp = timezone.now()
    file_ids = [uuid4() for _ in range(rows)]

    FileObject.objects.bulk_create(
        [
            FileObject(
                id=file_ids[index],
                project=project,
                workspace=project.workspace,
                name_original=f"Row {index}.pdf",
                name_display=f"Row {index}.pdf",
                name_normalized=f"row {index}.pdf",
                mime_type="application/pdf",
                extension="pdf",
                bucket="uploads",
                size_bytes=index,
                object_key=f"{uuid4()}.pdf",
                category=FileObject.Category.DOCS,
                status=FileObject.Status.ACTIVE,
                created_at=stamp,
                updated_at=stamp,
            )
            for index in range(rows)
        ],
        batch_size=1000,
    )
    FileVersion.objects.bulk_create(
        [
            FileVersion(
                id=uuid4(),
                project=project,
                workspace=project.workspace,
                file_id=file_ids[index],
                version_no=1,
                object_key=f"{uuid4()}.v1.pdf",
                bucket="uploads",
                mime_type="application/pdf",
                size_bytes=index,
                status=FileVersion.Status.ACTIVE,
                is_active=True,
                created_at=stamp,
                updated_at=stamp,
            )
            for index in range(rows)
        ],
        batch_size=1000,
    )

    # Statistics for the two tables the listing query reads (see the docstring).
    with connection.cursor() as cursor:
        cursor.execute("ANALYZE file_objects")
        cursor.execute("ANALYZE file_versions")


@pytest.mark.contract
@pytest.mark.django_db
class TestListBenchmark:
    """AC-29 / R-NFR-1: the filtered list over 10 000 rows, p95 under 400 ms."""

    def test_the_filtered_list_p95_stays_inside_the_budget_over_10000_rows(self, session_client, project):
        seed_files(project, LIST_ROWS)
        url = files_url(project.workspace.slug, project.id)

        # Warm-up: the first request materialises the quota rows and pays for the
        # request path's lazy imports. Discarded, and named so it cannot be mistaken
        # for a sample.
        for _ in range(WARMUP):
            _, warmup = timed_client_call(session_client, url, LIST_QUERY)
            assert warmup.status_code == status.HTTP_200_OK

        samples = []
        for _ in range(LIST_SAMPLES):
            elapsed_ms, response = timed_client_call(session_client, url, LIST_QUERY)
            assert response.status_code == status.HTTP_200_OK, response.data
            # The seed is what the benchmark claims to measure, and the filter really
            # matched all of it (a benchmark that pages an empty table would be fast
            # and meaningless).
            assert response.data["page"]["total_results"] == LIST_ROWS
            samples.append(elapsed_ms)

        # One instrumented request, outside the timed loop: how many queries the shape
        # costs and how much of the time is the database. A bound rather than a
        # measurement, and a generous one: any query per row would be ≥ 10 000.
        with CaptureQueriesContext(connection) as captured:
            _, instrumented = timed_client_call(session_client, url, LIST_QUERY)
        assert instrumented.status_code == status.HTTP_200_OK
        db_ms = sum(float(query["time"]) for query in captured.captured_queries) * 1000

        p50 = percentile(samples, 0.50)
        p95 = percentile(samples, 0.95)
        report(
            "file-list-10000",
            rows=LIST_ROWS,
            query=LIST_QUERY,
            samples=len(samples),
            warmup=WARMUP,
            min_ms=round(min(samples), 2),
            p50_ms=round(p50, 2),
            p95_ms=round(p95, 2),
            max_ms=round(max(samples), 2),
            threshold_ms=LIST_THRESHOLD_MS,
            queries=len(captured.captured_queries),
            db_ms=round(db_ms, 2),
        )

        assert len(captured.captured_queries) < 20
        assert p95 < LIST_THRESHOLD_MS, f"p95 {p95:.1f} ms over {len(samples)} samples exceeds {LIST_THRESHOLD_MS} ms"


@pytest.mark.contract
@pytest.mark.django_db
class TestPresignBenchmark:
    """AC-30 / R-NFR-2: presign latency and no object I/O before signing."""

    def _initiate(self, client, project, size_bytes):
        payload = {"file_name": f"B-{size_bytes}.pdf", "size_bytes": size_bytes, "mime_type": "application/pdf"}
        started = time.perf_counter()
        response = client.post(upload_url(project.workspace.slug, project.id), payload, format="json")
        return (time.perf_counter() - started) * 1000, response

    def test_presign_makes_no_storage_call_and_stays_inside_the_budget_at_every_size(
        self, session_client, project
    ):
        measurements = {}

        for size_bytes in PRESIGN_SIZES:
            with StorageCallRecorder() as recorder:
                for _ in range(WARMUP):
                    _, warmup = self._initiate(session_client, project, size_bytes)
                    assert warmup.status_code == status.HTTP_200_OK, warmup.data

                samples = []
                for _ in range(PRESIGN_SAMPLES):
                    elapsed_ms, response = self._initiate(session_client, project, size_bytes)
                    assert response.status_code == status.HTTP_200_OK, response.data
                    assert response.data["upload"]["url"].split("?")[0].endswith(response.data["file"]["object_key"])
                    samples.append(elapsed_ms)

            # The whole request made no call to the API: presigning is local signing
            # (AC-30's storage-client call counter, at the boto3 seam).
            assert_no_storage_calls(recorder.calls)

            measurements[size_bytes] = {
                "samples": len(samples),
                "warmup": WARMUP,
                "min_ms": round(min(samples), 2),
                "p50_ms": round(percentile(samples, 0.50), 2),
                "p95_ms": round(percentile(samples, 0.95), 2),
                "max_ms": round(max(samples), 2),
                "storage_calls": recorder.calls,
            }

        for size_bytes, numbers in measurements.items():
            report("file-presign", declared_bytes=size_bytes, threshold_ms=PRESIGN_THRESHOLD_MS, **numbers)
            # The requirement's threshold is a p95 (R-NFR-2), so that is what is
            # asserted. The observed maximum is reported next to it and deliberately
            # not asserted: one sample stretched by container scheduling is not a
            # latency regression, and asserting it would add flakiness without
            # adding a requirement-backed guarantee.
            assert numbers["p95_ms"] < PRESIGN_THRESHOLD_MS

        # Size-independence is asserted structurally rather than as a ratio between
        # two noisy p95s: the same number of queries for a 1 KiB declaration and for
        # the largest one the endpoint accepts. Object I/O at every size is already
        # zero above, and that is the mechanism the requirement names ("no object I/O
        # before signing"); a millisecond ratio here would be measuring scheduling
        # noise on sub-10 ms requests.
        query_counts = {}
        for size_bytes in (PRESIGN_SIZES[0], PRESIGN_SIZES[-1]):
            with CaptureQueriesContext(connection) as captured:
                _, response = self._initiate(session_client, project, size_bytes)
            assert response.status_code == status.HTTP_200_OK
            query_counts[size_bytes] = len(captured.captured_queries)

        report("file-presign-queries", queries_by_declared_bytes=query_counts)
        assert len(set(query_counts.values())) == 1

    def test_the_storage_call_guard_fails_when_a_call_is_introduced(self, session_client, project):
        """The positive control for the guard above.

        A careless implementation that looks the key up before signing must be
        visible to the counter, and the same assertion that passes for the real code
        must then fail. Without this, "no storage call" could just be a counter that
        never fires.
        """
        real_presign = S3Storage.generate_presigned_put

        def presign_after_a_lookup(self, object_name, content_type, expires_in=None):
            self.get_object_metadata(object_name)
            return real_presign(self, object_name, content_type=content_type, expires_in=expires_in)

        with StorageCallRecorder() as recorder:
            with mock.patch.object(S3Storage, "generate_presigned_put", presign_after_a_lookup):
                _, response = self._initiate(session_client, project, len(PDF_BYTES))

        assert response.status_code == status.HTTP_200_OK
        assert recorder.calls == ["HeadObject"]

        with pytest.raises(AssertionError):
            assert_no_storage_calls(recorder.calls)

    def test_the_timing_sample_is_not_zero_and_the_endpoint_really_signed(self, session_client, project):
        """A floor under the measurement: a request that failed fast is not latency."""
        elapsed_ms, response = self._initiate(session_client, project, PRESIGN_SIZES[-1])

        assert response.status_code == status.HTTP_200_OK, response.data
        assert "X-Amz-Signature=" in response.data["upload"]["url"]
        assert elapsed_ms > 0
