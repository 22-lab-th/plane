# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import json
import ipaddress
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from authlib.jose import JsonWebKey, JsonWebToken
from django.core.cache import cache
from django.test import override_settings

from plane.authentication.provider.oidc import OIDCConfigurationError, OIDCProviderClient
from plane.authentication.provider.oidc.client import MAX_OIDC_DOCUMENT_BYTES, _cache_get, _cache_set


def _provider(issuer="https://id.example.com"):
    return SimpleNamespace(id=uuid4(), issuer_url=issuer, updated_at=SimpleNamespace(timestamp=lambda: 1))


def _response(payload, status=200, headers=None):
    response = MagicMock()
    response.status_code = status
    body = json.dumps(payload).encode()
    response.headers = headers or {"Content-Length": str(len(body))}
    response.iter_content.return_value = [body]
    return response


def _metadata(**changes):
    data = {
        "issuer": "https://id.example.com",
        "authorization_endpoint": "https://id.example.com/authorize",
        "token_endpoint": "https://id.example.com/token",
        "jwks_uri": "https://id.example.com/jwks",
        "response_types_supported": ["code"],
    }
    data.update(changes)
    return data


@patch("plane.authentication.provider.oidc.client.resolve_and_validate")
@patch("plane.authentication.provider.oidc.client.pinned_fetch")
def test_configuration_fetches_valid_discovery_and_jwks(mock_fetch, mock_resolve):
    mock_resolve.return_value = ["93.184.216.34"]
    mock_fetch.side_effect = [_response(_metadata()), _response({"keys": [{"kid": "one", "kty": "RSA"}]})]

    metadata = OIDCProviderClient(_provider()).test_configuration()

    assert metadata["issuer"] == "https://id.example.com"
    assert mock_fetch.call_count == 2
    assert all(call.kwargs["stream"] is True for call in mock_fetch.call_args_list)


@pytest.mark.parametrize(
    "issuer",
    [
        "https://login.microsoftonline.com/tenant-id/v2.0",
        "https://example.okta.com/oauth2/default",
        "https://keycloak.example.com/realms/plane",
    ],
)
@patch("plane.authentication.provider.oidc.client.resolve_and_validate", return_value=["93.184.216.34"])
@patch("plane.authentication.provider.oidc.client.pinned_fetch")
def test_configuration_accepts_representative_provider_metadata(mock_fetch, mock_resolve, issuer):
    metadata = {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/authorize",
        "token_endpoint": f"{issuer}/token",
        "jwks_uri": f"{issuer}/keys",
        "end_session_endpoint": f"{issuer}/logout",
        "response_types_supported": ["code"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        "id_token_signing_alg_values_supported": ["RS256"],
    }
    mock_fetch.side_effect = [_response(metadata), _response({"keys": [{"kid": "one", "kty": "RSA"}]})]

    result = OIDCProviderClient(_provider(issuer)).test_configuration()

    assert result["issuer"] == issuer


@patch("plane.authentication.provider.oidc.client.resolve_and_validate", return_value=["93.184.216.34"])
@patch("plane.authentication.provider.oidc.client.pinned_fetch")
def test_configuration_rejects_issuer_mismatch(mock_fetch, mock_resolve):
    mock_fetch.return_value = _response(_metadata(issuer="https://attacker.example.com"))

    with pytest.raises(OIDCConfigurationError, match="does not match") as error:
        OIDCProviderClient(_provider()).test_configuration()

    assert error.value.code == "issuer_mismatch"


@patch("plane.authentication.provider.oidc.client.resolve_and_validate")
def test_configuration_rejects_private_issuer(mock_resolve):
    mock_resolve.side_effect = ValueError("private/internal")

    with pytest.raises(OIDCConfigurationError) as error:
        OIDCProviderClient(_provider("https://127.0.0.1")).test_configuration()

    assert error.value.code == "connection_failed"


@override_settings(
    OIDC_ALLOWED_IPS=[ipaddress.ip_network("127.0.0.1/32")],
    OIDC_CA_BUNDLE="/etc/plane/private-idp-ca.pem",
)
@patch("plane.authentication.provider.oidc.client.resolve_and_validate", return_value=["127.0.0.1"])
@patch("plane.authentication.provider.oidc.client.pinned_fetch")
def test_configuration_allows_explicitly_trusted_private_idp(mock_fetch, mock_resolve):
    issuer = "https://127.0.0.1:9443"
    mock_fetch.side_effect = [
        _response(
            {
                "issuer": issuer,
                "authorization_endpoint": f"{issuer}/authorize",
                "token_endpoint": f"{issuer}/token",
                "jwks_uri": f"{issuer}/jwks",
                "response_types_supported": ["code"],
            }
        ),
        _response({"keys": [{"kid": "one", "kty": "RSA"}]}),
    ]

    OIDCProviderClient(_provider(issuer)).test_configuration()

    assert mock_resolve.call_args.kwargs["allowed_ips"] == [ipaddress.ip_network("127.0.0.1/32")]
    assert mock_fetch.call_args.kwargs["allowed_ips"] == [ipaddress.ip_network("127.0.0.1/32")]
    assert mock_fetch.call_args.kwargs["verify"] == "/etc/plane/private-idp-ca.pem"


def test_configuration_rejects_non_https_issuer():
    with pytest.raises(OIDCConfigurationError) as error:
        OIDCProviderClient(_provider("http://id.example.com")).test_configuration()

    assert error.value.code == "invalid_url"


@pytest.mark.parametrize(
    ("issuer", "code"),
    [
        ("https://user:password@id.example.com", "invalid_url"),
        ("https://id.example.com/#fragment", "invalid_url"),
        ("https://id.example.com/?tenant=one", "invalid_url"),
        ("https://id.example.com:invalid", "invalid_url"),
    ],
)
@patch("plane.authentication.provider.oidc.client.resolve_and_validate", return_value=["93.184.216.34"])
def test_configuration_rejects_malformed_issuer(mock_resolve, issuer, code):
    with pytest.raises(OIDCConfigurationError) as error:
        OIDCProviderClient(_provider(issuer)).test_configuration()

    assert error.value.code == code


@patch("plane.authentication.provider.oidc.client.resolve_and_validate", return_value=["93.184.216.34"])
@patch("plane.authentication.provider.oidc.client.pinned_fetch")
def test_configuration_rejects_oversized_response(mock_fetch, mock_resolve):
    mock_fetch.return_value = _response({}, headers={"Content-Length": str(1024 * 1024 + 1)})

    with pytest.raises(OIDCConfigurationError) as error:
        OIDCProviderClient(_provider()).test_configuration()

    assert error.value.code == "response_too_large"


@patch("plane.authentication.provider.oidc.client.resolve_and_validate", return_value=["93.184.216.34"])
@patch("plane.authentication.provider.oidc.client.pinned_fetch")
def test_configuration_rejects_redirect(mock_fetch, mock_resolve):
    mock_fetch.return_value = _response({}, status=302)

    with pytest.raises(OIDCConfigurationError) as error:
        OIDCProviderClient(_provider()).test_configuration()

    assert error.value.code == "redirect_rejected"


@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        (_response({}, status=503), "upstream_error"),
        (_response({}, headers={"Content-Length": "invalid"}), "invalid_response"),
        (_response(["not-an-object"]), "invalid_json"),
    ],
)
@patch("plane.authentication.provider.oidc.client.resolve_and_validate", return_value=["93.184.216.34"])
@patch("plane.authentication.provider.oidc.client.pinned_fetch")
def test_configuration_categorizes_invalid_responses(mock_fetch, mock_resolve, response, expected_code):
    mock_fetch.return_value = response

    with pytest.raises(OIDCConfigurationError) as error:
        OIDCProviderClient(_provider()).test_configuration()

    assert error.value.code == expected_code
    response.close.assert_called_once()


@patch("plane.authentication.provider.oidc.client.resolve_and_validate", return_value=["93.184.216.34"])
@patch("plane.authentication.provider.oidc.client.pinned_fetch")
def test_configuration_rejects_streamed_response_over_limit(mock_fetch, mock_resolve):
    response = _response({}, headers={})
    response.iter_content.return_value = [b"x" * (MAX_OIDC_DOCUMENT_BYTES + 1)]
    mock_fetch.return_value = response

    with pytest.raises(OIDCConfigurationError) as error:
        OIDCProviderClient(_provider()).test_configuration()

    assert error.value.code == "response_too_large"


@pytest.mark.parametrize(
    ("changes", "expected_code"),
    [
        ({"jwks_uri": None}, "missing_capability"),
        ({"response_types_supported": ["token"]}, "missing_capability"),
        ({"end_session_endpoint": ""}, "invalid_url"),
    ],
)
@patch("plane.authentication.provider.oidc.client.resolve_and_validate", return_value=["93.184.216.34"])
@patch("plane.authentication.provider.oidc.client.pinned_fetch")
def test_configuration_requires_oidc_capabilities(mock_fetch, mock_resolve, changes, expected_code):
    mock_fetch.return_value = _response(_metadata(**changes))

    with pytest.raises(OIDCConfigurationError) as error:
        OIDCProviderClient(_provider()).test_configuration()

    assert error.value.code == expected_code


def test_oidc_cache_is_fail_soft():
    with patch.object(cache, "get", side_effect=RuntimeError("cache unavailable")):
        assert _cache_get("missing") is None
    with patch.object(cache, "set", side_effect=RuntimeError("cache unavailable")):
        assert _cache_set("key", {"safe": True}) is None


def test_discovery_and_jwks_use_cached_documents():
    provider = _provider()
    client = OIDCProviderClient(provider)
    metadata = _metadata()
    jwks = {"keys": [{"kid": "one"}]}
    with patch("plane.authentication.provider.oidc.client._cache_get", side_effect=[metadata, jwks]):
        assert client.get_discovery_document() == metadata
        assert client.get_jwks(metadata=metadata) == jwks


@patch("plane.authentication.provider.oidc.client._safe_json_fetch")
def test_jwks_requires_non_empty_key_list(mock_fetch):
    mock_fetch.return_value = {"keys": []}

    with pytest.raises(OIDCConfigurationError) as error:
        OIDCProviderClient(_provider()).get_jwks(metadata=_metadata(), refresh=True)

    assert error.value.code == "invalid_jwks"


@pytest.mark.parametrize(
    ("methods", "expected_secret_location"),
    [(["client_secret_basic"], "header"), (["client_secret_post"], "body")],
)
@patch("plane.authentication.provider.oidc.client._safe_json_fetch")
def test_code_exchange_supports_standard_client_auth_methods(mock_fetch, methods, expected_secret_location):
    provider = _provider()
    provider.client_id = "plane"
    provider.get_client_secret = MagicMock(return_value="secret")
    client = OIDCProviderClient(provider)
    client.get_discovery_document = MagicMock(return_value=_metadata(token_endpoint_auth_methods_supported=methods))
    mock_fetch.return_value = {"id_token": "signed-token"}

    token_data, _ = client.exchange_code("code", "verifier", "https://plane.example.com/auth/sso/callback/")

    assert token_data["id_token"] == "signed-token"
    request = mock_fetch.call_args
    if expected_secret_location == "header":
        assert request.kwargs["headers"]["Authorization"].startswith("Basic ")
        assert "client_secret" not in request.kwargs["data"]
    else:
        assert request.kwargs["data"]["client_secret"] == "secret"


@pytest.mark.parametrize(
    ("methods", "token_data", "expected_code"),
    [
        (["private_key_jwt"], {"id_token": "token"}, "unsupported_client_auth"),
        (["client_secret_basic"], {"access_token": "token"}, "missing_id_token"),
    ],
)
@patch("plane.authentication.provider.oidc.client._safe_json_fetch")
def test_code_exchange_rejects_unsupported_or_incomplete_response(mock_fetch, methods, token_data, expected_code):
    provider = _provider()
    provider.client_id = "plane"
    provider.get_client_secret = MagicMock(return_value="secret")
    client = OIDCProviderClient(provider)
    client.get_discovery_document = MagicMock(return_value=_metadata(token_endpoint_auth_methods_supported=methods))
    mock_fetch.return_value = token_data

    with pytest.raises(OIDCConfigurationError) as error:
        client.exchange_code("code", "verifier", "https://plane.example.com/auth/sso/callback/")

    assert error.value.code == expected_code


def _signed_token(provider, *, nonce="expected-nonce", audience="plane", algorithm="RS256"):
    key = JsonWebKey.generate_key("RSA", 2048, is_private=True, options={"kid": "key-one"})
    now = int(time.time())
    token = JsonWebToken([algorithm]).encode(
        {"alg": algorithm, "kid": "key-one"},
        {
            "iss": provider.issuer_url,
            "sub": "stable-subject",
            "aud": audience,
            "exp": now + 300,
            "iat": now,
            "nonce": nonce,
            "email": "member@example.com",
            "email_verified": True,
        },
        key,
    )
    return token.decode(), {"keys": [key.as_dict(is_private=False)]}


def test_id_token_validation_accepts_expected_security_claims():
    provider = _provider()
    provider.client_id = "plane"
    client = OIDCProviderClient(provider)
    token, jwks = _signed_token(provider)
    client.get_jwks = MagicMock(return_value=jwks)

    claims = client.validate_id_token(
        {"id_token": token},
        {"id_token_signing_alg_values_supported": ["RS256"]},
        "expected-nonce",
    )

    assert claims["sub"] == "stable-subject"


@pytest.mark.parametrize(
    ("token_nonce", "audience"),
    [("wrong-nonce", "plane"), ("expected-nonce", "another-client")],
)
def test_id_token_validation_rejects_nonce_or_audience_mismatch(token_nonce, audience):
    provider = _provider()
    provider.client_id = "plane"
    client = OIDCProviderClient(provider)
    token, jwks = _signed_token(provider, nonce=token_nonce, audience=audience)
    client.get_jwks = MagicMock(return_value=jwks)

    with pytest.raises(OIDCConfigurationError) as error:
        client.validate_id_token(
            {"id_token": token},
            {"id_token_signing_alg_values_supported": ["RS256"]},
            "expected-nonce",
        )

    assert error.value.code == "invalid_id_token"


def test_id_token_validation_refreshes_unknown_signing_key_once():
    provider = _provider()
    provider.client_id = "plane"
    client = OIDCProviderClient(provider)
    token, jwks = _signed_token(provider)
    other_key = JsonWebKey.generate_key("RSA", 2048, is_private=True, options={"kid": "other"})
    client.get_jwks = MagicMock(side_effect=[{"keys": [other_key.as_dict(is_private=False)]}, jwks])

    claims = client.validate_id_token(
        {"id_token": token},
        {"id_token_signing_alg_values_supported": ["RS256"]},
        "expected-nonce",
    )

    assert claims["sub"] == "stable-subject"
    assert client.get_jwks.call_count == 2


def test_id_token_validation_rejects_malformed_jwks_as_configuration_error():
    provider = _provider()
    provider.client_id = "plane"
    client = OIDCProviderClient(provider)
    token, _ = _signed_token(provider)
    client.get_jwks = MagicMock(return_value={"keys": [{"kid": "key-one", "kty": "RSA"}]})

    with pytest.raises(OIDCConfigurationError) as error:
        client.validate_id_token(
            {"id_token": token},
            {"id_token_signing_alg_values_supported": ["RS256"]},
            "expected-nonce",
        )

    assert error.value.code == "invalid_jwks"


@pytest.mark.parametrize(
    ("token", "expected_code"),
    [
        (None, "invalid_id_token"),
        ("x" * (128 * 1024 + 1), "invalid_id_token"),
        ("not-json.payload.signature", "invalid_id_token"),
    ],
)
def test_id_token_validation_rejects_invalid_headers(token, expected_code):
    provider = _provider()
    provider.client_id = "plane"

    with pytest.raises(OIDCConfigurationError) as error:
        OIDCProviderClient(provider).validate_id_token(
            {"id_token": token},
            {"id_token_signing_alg_values_supported": ["RS256"]},
            "nonce",
        )

    assert error.value.code == expected_code


def test_id_token_validation_rejects_unadvertised_algorithm():
    provider = _provider()
    provider.client_id = "plane"
    token, jwks = _signed_token(provider)
    client = OIDCProviderClient(provider)
    client.get_jwks = MagicMock(return_value=jwks)

    with pytest.raises(OIDCConfigurationError) as error:
        client.validate_id_token(
            {"id_token": token},
            {"id_token_signing_alg_values_supported": ["ES256"]},
            "expected-nonce",
        )

    assert error.value.code == "invalid_algorithm"


def test_id_token_validation_rejects_unknown_key_after_refresh():
    provider = _provider()
    provider.client_id = "plane"
    token, _ = _signed_token(provider)
    other_key = JsonWebKey.generate_key("RSA", 2048, is_private=True, options={"kid": "other"})
    client = OIDCProviderClient(provider)
    client.get_jwks = MagicMock(return_value={"keys": [other_key.as_dict(is_private=False)]})

    with pytest.raises(OIDCConfigurationError) as error:
        client.validate_id_token(
            {"id_token": token},
            {"id_token_signing_alg_values_supported": ["RS256"]},
            "expected-nonce",
        )

    assert error.value.code == "unknown_signing_key"
    assert client.get_jwks.call_count == 2
