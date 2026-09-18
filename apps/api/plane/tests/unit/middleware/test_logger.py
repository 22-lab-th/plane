# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""
Unit tests for APITokenLogMiddleware.

Covers the credential-hygiene guarantees of the external API request logger:
- the raw API key is never persisted (a non-reversible hash is stored instead)
- sensitive request headers are redacted before being logged
"""

import hashlib
import hmac
from unittest.mock import Mock, patch

import pytest
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponse
from django.test import RequestFactory

from plane.middleware.logger import APITokenLogMiddleware, RequestLoggerMiddleware


@pytest.fixture
def request_factory():
    return RequestFactory()


@pytest.fixture
def middleware():
    return APITokenLogMiddleware(Mock(return_value=HttpResponse(b"{}")))


@pytest.mark.unit
class TestAPITokenLogMiddleware:
    API_KEY = "plane_api_supersecretvalue"
    AUTHORIZATION = "Bearer secret-bearer-token"
    COOKIE = "sessionid=secret-session-value"

    def _captured_log_data(self, middleware, request_factory):
        request = request_factory.get(
            "/api/v1/workspaces/",
            HTTP_X_API_KEY=self.API_KEY,
            HTTP_AUTHORIZATION=self.AUTHORIZATION,
            HTTP_COOKIE=self.COOKIE,
        )
        request.user = AnonymousUser()
        response = HttpResponse(b"{}")
        with patch("plane.middleware.logger.process_logs") as process_logs:
            middleware.process_request(request, response, request_body=b"")
            assert process_logs.delay.called
            return process_logs.delay.call_args.kwargs["log_data"]

    def test_token_identifier_is_hashed_not_plaintext(self, middleware, request_factory):
        log_data = self._captured_log_data(middleware, request_factory)

        expected_hash = hmac.new(
            settings.SECRET_KEY.encode(), self.API_KEY.encode(), hashlib.sha256
        ).hexdigest()
        assert log_data["token_identifier"] == expected_hash
        assert self.API_KEY not in log_data["token_identifier"]

    def test_sensitive_headers_are_redacted(self, middleware, request_factory):
        log_data = self._captured_log_data(middleware, request_factory)

        # None of the sensitive header values may appear in the logged headers.
        assert self.API_KEY not in log_data["headers"]
        assert self.AUTHORIZATION not in log_data["headers"]
        assert self.COOKIE not in log_data["headers"]
        assert "[REDACTED]" in log_data["headers"]

    def test_no_log_without_api_key(self, middleware, request_factory):
        request = request_factory.get("/api/v1/workspaces/")
        request.user = AnonymousUser()
        with patch("plane.middleware.logger.process_logs") as process_logs:
            middleware.process_request(request, HttpResponse(b"{}"), request_body=b"")
            assert not process_logs.delay.called


@pytest.mark.unit
def test_request_logger_omits_sensitive_query_string(request_factory, caplog):
    request = request_factory.get(
        "/api/instances/sso/providers/test-callback/",
        {"code": "single-use-authorization-code", "state": "oidc-state"},
    )
    request.user = AnonymousUser()
    middleware = RequestLoggerMiddleware(Mock(return_value=HttpResponse(status=302)))

    with caplog.at_level("INFO", logger="plane.api.request"):
        middleware(request)

    assert "GET /api/instances/sso/providers/test-callback/ 302" in caplog.text
    assert "single-use-authorization-code" not in caplog.text
    assert "oidc-state" not in caplog.text


@pytest.mark.unit
def test_api_token_logger_omits_sensitive_query_string(middleware, request_factory):
    request = request_factory.get(
        "/api/instances/sso/providers/test-callback/",
        {"code": "single-use-authorization-code", "state": "oidc-state"},
        HTTP_X_API_KEY=TestAPITokenLogMiddleware.API_KEY,
    )
    request.user = AnonymousUser()

    with patch("plane.middleware.logger.process_logs") as process_logs:
        middleware.process_request(request, HttpResponse(b"{}"), request_body=b"")

    log_data = process_logs.delay.call_args.kwargs["log_data"]
    serialized_log_data = str(log_data)
    assert "query_params" not in log_data
    assert "single-use-authorization-code" not in serialized_log_data
    assert "oidc-state" not in serialized_log_data
