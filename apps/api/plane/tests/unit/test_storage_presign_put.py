# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Unit tests for the S3-compatible presigned PUT helper (`S3Storage`).

Signing is a local operation, so these tests exercise the real botocore
signer with fake credentials and never touch the network (R-NFR-8: the same
adapter serves MinIO locally and R2 in production).
"""

# Python imports
import os
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

# Third party imports
import pytest
from botocore.exceptions import ClientError

# Module imports
from plane.settings.storage import S3Storage

INTERNAL_ENDPOINT = "http://test-minio:9000"
PUBLIC_ENDPOINT = "http://objects.example.com:9000"
BUCKET = "plane-files-test"
CONTENT_TYPE = "application/pdf"
OBJECT_KEY = (
    "workspace/acme/projects/CBUTR-demo/issues/CBUTR-11/0193f0a1-6b7c-7d21-9f4a-2c5b8e0d1a44/v1/customer-deliverables.pdf"
)
OTHER_KEY = (
    "workspace/acme/projects/CBUTR-demo/issues/CBUTR-11/0193f0a1-6b7c-7d21-9f4a-2c5b8e0d1a44/v2/customer-deliverables.pdf"
)

CREDENTIAL_ENV = {
    "AWS_ACCESS_KEY_ID": "test-access-key",
    "AWS_SECRET_ACCESS_KEY": "test-secret-key",
    "AWS_S3_BUCKET_NAME": BUCKET,
    "AWS_S3_ENDPOINT_URL": INTERNAL_ENDPOINT,
    "AWS_REGION": "us-east-1",
    "SIGNED_URL_EXPIRATION": "900",
}


def make_storage(extra_env=None):
    """Build an adapter with fake credentials; no request is sent by signing."""
    with patch.dict(os.environ, {**CREDENTIAL_ENV, **(extra_env or {})}, clear=True):
        return S3Storage()


@pytest.mark.unit
class TestGeneratePresignedPut:
    """The presigned PUT contract consumed by the upload endpoint."""

    def test_returns_a_put_instruction_for_the_exact_key(self):
        result = make_storage().generate_presigned_put(OBJECT_KEY, CONTENT_TYPE)

        assert set(result) == {"url", "method", "headers", "expires_in"}
        assert result["method"] == "PUT"
        assert result["headers"] == {"Content-Type": CONTENT_TYPE}
        assert result["expires_in"] == 900

    def test_url_binds_the_object_key_and_bucket(self):
        result = make_storage().generate_presigned_put(OBJECT_KEY, CONTENT_TYPE)
        url = urlsplit(result["url"])

        assert url.scheme == "http"
        assert url.hostname == "test-minio"
        assert OBJECT_KEY in url.path
        assert BUCKET in url.path or BUCKET in url.netloc

    def test_url_carries_a_signature_and_the_content_type(self):
        url = make_storage().generate_presigned_put(OBJECT_KEY, CONTENT_TYPE)["url"]
        query = parse_qs(urlsplit(url).query)

        assert query["X-Amz-Signature"][0]
        assert query["X-Amz-Credential"][0].startswith("test-access-key/")
        assert query["X-Amz-Expires"][0] == "900"
        # Content-Type is part of the signed header set, so the provider rejects
        # a PUT that declares a different type with SignatureDoesNotMatch.
        assert "content-type" in query["X-Amz-SignedHeaders"][0].lower()

    def test_content_type_is_bound_to_the_signature(self):
        storage = make_storage()

        pdf_signature = parse_qs(
            urlsplit(storage.generate_presigned_put(OBJECT_KEY, "application/pdf")["url"]).query
        )["X-Amz-Signature"][0]
        text_signature = parse_qs(
            urlsplit(storage.generate_presigned_put(OBJECT_KEY, "text/plain")["url"]).query
        )["X-Amz-Signature"][0]

        assert pdf_signature != text_signature

    def test_url_differs_when_the_key_changes(self):
        storage = make_storage()

        first = urlsplit(storage.generate_presigned_put(OBJECT_KEY, CONTENT_TYPE)["url"])
        second = urlsplit(storage.generate_presigned_put(OTHER_KEY, CONTENT_TYPE)["url"])

        assert first.path.endswith("/v1/customer-deliverables.pdf")
        assert second.path.endswith("/v2/customer-deliverables.pdf")
        assert first.path != second.path

    def test_explicit_expiry_overrides_the_default(self):
        result = make_storage().generate_presigned_put(OBJECT_KEY, CONTENT_TYPE, expires_in=60)
        query = parse_qs(urlsplit(result["url"]).query)

        assert result["expires_in"] == 60
        assert query["X-Amz-Expires"][0] == "60"

    def test_signs_against_the_browser_facing_endpoint_when_minio_is_split(self):
        """Keep the existing internal/browser endpoint split used by POST and GET."""
        storage = make_storage(
            extra_env={
                "USE_MINIO": "1",
                "MINIO_PUBLIC_ENDPOINT_URL": PUBLIC_ENDPOINT,
            }
        )
        # The internal client is built with the internal endpoint, ...
        assert storage.s3_client.meta.endpoint_url == INTERNAL_ENDPOINT
        # ... while the presigned URL must be reachable by the browser.
        url = urlsplit(storage.generate_presigned_put(OBJECT_KEY, CONTENT_TYPE)["url"])

        assert url.hostname.endswith("objects.example.com")

    def test_signing_failure_returns_none_instead_of_an_unsigned_url(self, monkeypatch):
        storage = make_storage()
        failing_client = Mock()
        failing_client.generate_presigned_url.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "PutObject"
        )
        monkeypatch.setattr(storage, "presign_s3_client", failing_client)

        assert storage.generate_presigned_put(OBJECT_KEY, CONTENT_TYPE) is None

        args, kwargs = failing_client.generate_presigned_url.call_args
        assert args[0] == "put_object"
        assert kwargs["Params"]["Key"] == OBJECT_KEY
        assert kwargs["Params"]["ContentType"] == CONTENT_TYPE
        assert kwargs["HttpMethod"] == "PUT"
