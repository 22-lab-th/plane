import argparse
import base64
import hashlib
import json
import ssl
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from authlib.jose import JsonWebKey, JsonWebToken


class OIDCState:
    issuer = ""
    client_id = "plane-e2e"
    client_secret = "e2e-secret"
    codes = {}
    signing_key = JsonWebKey.generate_key(
        "RSA", 2048, is_private=True, options={"kid": "plane-e2e-key"}
    )


class OIDCHandler(BaseHTTPRequestHandler):
    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/health":
            return self._json(200, {"status": "ok"})
        if parsed.path == "/.well-known/openid-configuration":
            return self._json(
                200,
                {
                    "issuer": OIDCState.issuer,
                    "authorization_endpoint": f"{OIDCState.issuer}/authorize",
                    "token_endpoint": f"{OIDCState.issuer}/token",
                    "jwks_uri": f"{OIDCState.issuer}/jwks",
                    "end_session_endpoint": f"{OIDCState.issuer}/logout",
                    "response_types_supported": ["code"],
                    "token_endpoint_auth_methods_supported": ["client_secret_basic"],
                    "id_token_signing_alg_values_supported": ["RS256"],
                },
            )
        if parsed.path == "/jwks":
            return self._json(
                200, {"keys": [OIDCState.signing_key.as_dict(is_private=False)]}
            )
        if parsed.path == "/authorize":
            required = ("client_id", "redirect_uri", "state", "nonce", "code_challenge")
            if (
                any(not query.get(name) for name in required)
                or query["client_id"][0] != OIDCState.client_id
            ):
                return self._json(400, {"error": "invalid_request"})
            code = (
                base64.urlsafe_b64encode(
                    hashlib.sha256(query["state"][0].encode()).digest()
                )
                .decode()
                .rstrip("=")
            )
            OIDCState.codes[code] = {
                "nonce": query["nonce"][0],
                "code_challenge": query["code_challenge"][0],
                "redirect_uri": query["redirect_uri"][0],
            }
            return self._redirect(
                f"{query['redirect_uri'][0]}?{urlencode({'code': code, 'state': query['state'][0]})}"
            )
        if parsed.path == "/logout":
            redirect_uri = query.get("post_logout_redirect_uri", [None])[0]
            return (
                self._redirect(redirect_uri)
                if redirect_uri
                else self._json(200, {"status": "logged_out"})
            )
        return self._json(404, {"error": "not_found"})

    def do_POST(self):
        if urlparse(self.path).path != "/token":
            return self._json(404, {"error": "not_found"})
        expected_auth = base64.b64encode(
            f"{OIDCState.client_id}:{OIDCState.client_secret}".encode()
        ).decode()
        if self.headers.get("Authorization") != f"Basic {expected_auth}":
            return self._json(401, {"error": "invalid_client"})
        length = int(self.headers.get("Content-Length", "0"))
        form = parse_qs(self.rfile.read(length).decode())
        code = form.get("code", [""])[0]
        transaction = OIDCState.codes.pop(code, None)
        verifier = form.get("code_verifier", [""])[0]
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        if transaction is None or challenge != transaction["code_challenge"]:
            return self._json(400, {"error": "invalid_grant"})
        now = int(time.time())
        token = (
            JsonWebToken(["RS256"])
            .encode(
                {"alg": "RS256", "kid": "plane-e2e-key"},
                {
                    "iss": OIDCState.issuer,
                    "sub": "e2e-user-subject",
                    "aud": OIDCState.client_id,
                    "exp": now + 300,
                    "iat": now,
                    "nonce": transaction["nonce"],
                    "email": "sso.e2e@example.com",
                    "email_verified": True,
                    "name": "SSO E2E User",
                    "groups": ["plane-users"],
                },
                OIDCState.signing_key,
            )
            .decode()
        )
        return self._json(
            200, {"id_token": token, "token_type": "Bearer", "expires_in": 300}
        )

    def log_message(self, format, *args):
        print(f"mock-idp: {format % args}", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--port", type=int, default=9443)
    args = parser.parse_args()
    OIDCState.issuer = f"https://127.0.0.1:{args.port}"
    server = ThreadingHTTPServer(("127.0.0.1", args.port), OIDCHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.cert, args.key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
