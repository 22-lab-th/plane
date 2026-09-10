# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import json
import base64
import binascii
from urllib.parse import urlsplit

import requests
from authlib.jose import JsonWebKey, JsonWebToken
from authlib.jose.errors import JoseError as AuthlibJoseError
from authlib.oidc.core import CodeIDToken
from joserfc.errors import JoseError as JoseRFCError
from django.core.cache import cache
from django.conf import settings

from plane.utils.ip_address import resolve_and_validate
from plane.utils.url_security import pinned_fetch
from plane.utils.exception_logger import log_exception

MAX_OIDC_DOCUMENT_BYTES = 1024 * 1024
OIDC_CACHE_SECONDS = 10 * 60
REQUIRED_METADATA_URLS = ("authorization_endpoint", "token_endpoint", "jwks_uri")
ALLOWED_ID_TOKEN_ALGORITHMS = {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "EdDSA"}


class OIDCConfigurationError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _cache_get(key):
    try:
        return cache.get(key)
    except Exception as exc:
        log_exception(exc)
        return None


def _cache_set(key, value):
    try:
        cache.set(key, value, OIDC_CACHE_SECONDS)
    except Exception as exc:
        log_exception(exc)


def _validate_https_url(value, label):
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise OIDCConfigurationError("invalid_url", f"{label} is not a valid URL.") from exc
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise OIDCConfigurationError("invalid_url", f"{label} must be an HTTPS URL without credentials.")
    if parsed.fragment:
        raise OIDCConfigurationError("invalid_url", f"{label} must not contain a fragment.")
    try:
        resolve_and_validate(parsed.hostname, allowed_ips=settings.OIDC_ALLOWED_IPS)
    except ValueError as exc:
        raise OIDCConfigurationError("connection_failed", f"{label} does not resolve to a permitted address.") from exc
    return port


def _read_bounded_json(response, label):
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > MAX_OIDC_DOCUMENT_BYTES:
                raise OIDCConfigurationError("response_too_large", f"{label} response is too large.")
        except ValueError as exc:
            raise OIDCConfigurationError("invalid_response", f"{label} returned an invalid response.") from exc

    body = bytearray()
    for chunk in response.iter_content(chunk_size=16384):
        body.extend(chunk)
        if len(body) > MAX_OIDC_DOCUMENT_BYTES:
            raise OIDCConfigurationError("response_too_large", f"{label} response is too large.")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OIDCConfigurationError("invalid_json", f"{label} did not return valid JSON.") from exc
    if not isinstance(payload, dict):
        raise OIDCConfigurationError("invalid_json", f"{label} must return a JSON object.")
    return payload


def _safe_json_fetch(url, label, method="GET", **request_kwargs):
    _validate_https_url(url, label)
    try:
        response = pinned_fetch(
            method,
            url,
            allowed_ips=settings.OIDC_ALLOWED_IPS,
            timeout=(3.05, 10),
            verify=settings.OIDC_CA_BUNDLE or True,
            stream=True,
            **request_kwargs,
        )
        try:
            if 300 <= response.status_code < 400:
                raise OIDCConfigurationError("redirect_rejected", f"{label} must not redirect.")
            if response.status_code != 200:
                raise OIDCConfigurationError("upstream_error", f"{label} returned an unsuccessful status.")
            return _read_bounded_json(response, label)
        finally:
            response.close()
    except OIDCConfigurationError:
        raise
    except (ValueError, requests.RequestException) as exc:
        raise OIDCConfigurationError("connection_failed", f"Could not securely fetch {label}.") from exc


class OIDCProviderClient:
    def __init__(self, provider):
        self.provider = provider

    @property
    def discovery_url(self):
        issuer = self.provider.issuer_url
        parsed = urlsplit(issuer)
        if parsed.query or parsed.fragment:
            raise OIDCConfigurationError("invalid_url", "OIDC issuer must not contain a query or fragment.")
        return f"{issuer}/.well-known/openid-configuration"

    def _cache_key(self, document):
        return f"sso:oidc:{self.provider.id}:{self.provider.updated_at.timestamp()}:{document}"

    def get_discovery_document(self, refresh=False):
        cache_key = self._cache_key("discovery")
        if not refresh:
            cached = _cache_get(cache_key)
            if cached is not None:
                return cached

        metadata = _safe_json_fetch(self.discovery_url, "OIDC discovery")
        expected_issuer = self.provider.issuer_url
        if metadata.get("issuer") != expected_issuer:
            raise OIDCConfigurationError(
                "issuer_mismatch", "OIDC discovery issuer does not match the configured issuer."
            )
        for field in REQUIRED_METADATA_URLS:
            endpoint = metadata.get(field)
            if not isinstance(endpoint, str) or not endpoint:
                raise OIDCConfigurationError("missing_capability", f"OIDC discovery is missing {field}.")
            _validate_https_url(endpoint, field)
        end_session_endpoint = metadata.get("end_session_endpoint")
        if end_session_endpoint is not None:
            if not isinstance(end_session_endpoint, str) or not end_session_endpoint:
                raise OIDCConfigurationError("invalid_url", "end_session_endpoint must be a non-empty URL.")
            _validate_https_url(end_session_endpoint, "end_session_endpoint")
        response_types = metadata.get("response_types_supported", [])
        if "code" not in response_types:
            raise OIDCConfigurationError(
                "missing_capability", "OIDC provider does not advertise authorization code flow."
            )
        _cache_set(cache_key, metadata)
        return metadata

    def get_jwks(self, metadata=None, refresh=False):
        metadata = metadata or self.get_discovery_document(refresh=refresh)
        cache_key = self._cache_key("jwks")
        if not refresh:
            cached = _cache_get(cache_key)
            if cached is not None:
                return cached
        jwks = _safe_json_fetch(metadata["jwks_uri"], "OIDC JWKS")
        if not isinstance(jwks.get("keys"), list) or not jwks["keys"]:
            raise OIDCConfigurationError("invalid_jwks", "OIDC JWKS does not contain signing keys.")
        _cache_set(cache_key, jwks)
        return jwks

    def test_configuration(self):
        metadata = self.get_discovery_document(refresh=True)
        self.get_jwks(metadata=metadata, refresh=True)
        return metadata

    def exchange_code(self, code, code_verifier, redirect_uri):
        metadata = self.get_discovery_document()
        authentication_methods = metadata.get("token_endpoint_auth_methods_supported", ["client_secret_basic"])
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self.provider.client_id,
            "code_verifier": code_verifier,
        }
        headers = {"Accept": "application/json"}
        client_secret = self.provider.get_client_secret()
        if "client_secret_basic" in authentication_methods:
            credentials = f"{self.provider.client_id}:{client_secret}".encode()
            headers["Authorization"] = f"Basic {base64.b64encode(credentials).decode()}"
        elif "client_secret_post" in authentication_methods:
            data["client_secret"] = client_secret
        else:
            raise OIDCConfigurationError(
                "unsupported_client_auth", "OIDC provider does not support a configured client authentication method."
            )
        token_data = _safe_json_fetch(
            metadata["token_endpoint"],
            "OIDC token endpoint",
            method="POST",
            headers=headers,
            data=data,
        )
        if not isinstance(token_data.get("id_token"), str):
            raise OIDCConfigurationError("missing_id_token", "OIDC token response did not contain an ID token.")
        return token_data, metadata

    def _get_token_header(self, token):
        if not isinstance(token, str) or len(token) > 128 * 1024:
            raise OIDCConfigurationError("invalid_id_token", "OIDC ID token is invalid.")
        try:
            encoded_header = token.split(".", 1)[0]
            padding = "=" * (-len(encoded_header) % 4)
            header = json.loads(base64.urlsafe_b64decode(encoded_header + padding))
        except (binascii.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OIDCConfigurationError("invalid_id_token", "OIDC ID token header is invalid.") from exc
        if not isinstance(header, dict):
            raise OIDCConfigurationError("invalid_id_token", "OIDC ID token header is invalid.")
        return header

    def _find_signing_key(self, jwks, kid):
        try:
            key_set = JsonWebKey.import_key_set(jwks)
        except (AuthlibJoseError, JoseRFCError, ValueError, TypeError, KeyError) as exc:
            raise OIDCConfigurationError("invalid_jwks", "OIDC JWKS contains invalid signing keys.") from exc
        try:
            return key_set.find_by_kid(kid)
        except ValueError as exc:
            if any(isinstance(item, dict) and item.get("kid") == kid for item in jwks.get("keys", [])):
                raise OIDCConfigurationError("invalid_jwks", "OIDC JWKS contains an invalid signing key.") from exc
            return None

    def validate_id_token(self, token_data, metadata, nonce):
        token = token_data["id_token"]
        header = self._get_token_header(token)
        algorithm = header.get("alg")
        advertised_algorithms = set(metadata.get("id_token_signing_alg_values_supported", ["RS256"]))
        if algorithm not in ALLOWED_ID_TOKEN_ALGORITHMS or algorithm not in advertised_algorithms:
            raise OIDCConfigurationError("invalid_algorithm", "OIDC ID token uses an unsupported algorithm.")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise OIDCConfigurationError("invalid_id_token", "OIDC ID token has no signing key identifier.")

        jwks = self.get_jwks(metadata=metadata)
        key = self._find_signing_key(jwks, kid)
        if key is None:
            jwks = self.get_jwks(metadata=metadata, refresh=True)
            key = self._find_signing_key(jwks, kid)
        if key is None:
            raise OIDCConfigurationError("unknown_signing_key", "OIDC ID token signing key is unknown.")

        decoder = JsonWebToken([algorithm])
        try:
            claims = decoder.decode(
                token,
                key,
                claims_cls=CodeIDToken,
                claims_options={
                    "iss": {"essential": True, "value": self.provider.issuer_url},
                    "aud": {"essential": True, "value": self.provider.client_id},
                    "exp": {"essential": True},
                    "iat": {"essential": True},
                },
                claims_params={
                    "client_id": self.provider.client_id,
                    "nonce": nonce,
                    "access_token": token_data.get("access_token"),
                },
            )
            claims.validate(leeway=60)
        except (AuthlibJoseError, JoseRFCError, ValueError, TypeError) as exc:
            raise OIDCConfigurationError("invalid_id_token", "OIDC ID token validation failed.") from exc
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject or len(subject) > 512:
            raise OIDCConfigurationError("invalid_subject", "OIDC ID token subject is invalid.")
        return dict(claims)
