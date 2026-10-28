"""Fail-closed RFC 9068 operator access-token verification."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

import jwt


class OperatorTokenInvalid(Exception):
    pass


class OperatorTokenVerifier:
    def __init__(self, issuer: str, audience: str, jwks_url: str, subjects_json: str):
        for label, url in (("issuer", issuer), ("JWKS", jwks_url)):
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment):
                raise ValueError(f"OIDC {label} must be a configured HTTPS URL")
        if not 1 <= len(audience) <= 200 or any(character.isspace() for character in audience):
            raise ValueError("OIDC audience is invalid")
        try:
            subjects = json.loads(subjects_json)
        except json.JSONDecodeError as exc:
            raise ValueError("OIDC subject allowlist is invalid") from exc
        if not isinstance(subjects, dict) or not subjects:
            raise ValueError("OIDC subject allowlist must be nonempty")
        for subject, account in subjects.items():
            if (not isinstance(subject, str) or not 1 <= len(subject) <= 128
                    or not isinstance(account, dict) or set(account) != {"tenant_id", "role"}
                    or not isinstance(account["tenant_id"], str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", account["tenant_id"])
                    or account["role"] not in ("admin", "fleet_operator", "viewer")):
                raise ValueError("OIDC subject allowlist entry is invalid")
        self.issuer = issuer
        self.audience = audience
        self.subjects = subjects
        self.keys = jwt.PyJWKClient(jwks_url, cache_jwk_set=True, lifespan=300)

    def verify(self, token: str) -> tuple[str, str, str]:
        if not 1 <= len(token) <= 8192:
            raise OperatorTokenInvalid("invalid operator access token")
        try:
            header = jwt.get_unverified_header(token)
            if (header.get("alg") != "RS256" or str(header.get("typ", "")).lower()
                    not in ("at+jwt", "application/at+jwt")
                    or not isinstance(header.get("kid"), str) or not 1 <= len(header["kid"]) <= 128):
                raise OperatorTokenInvalid("invalid operator access token")
            signing_key = self.keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(token, signing_key.key, algorithms=["RS256"], issuer=self.issuer,
                                audience=self.audience, leeway=30,
                                options={"require": ["iss", "aud", "sub", "exp", "iat", "jti", "client_id"]})
            subject = claims["sub"]
            account = self.subjects.get(subject)
            now = datetime.now(timezone.utc).timestamp()
            if (account is None or not isinstance(subject, str) or not 1 <= len(subject) <= 128
                    or isinstance(claims["exp"], bool) or isinstance(claims["iat"], bool)
                    or not isinstance(claims["exp"], (int, float))
                    or not isinstance(claims["iat"], (int, float))
                    or claims["exp"] - claims["iat"] > 3600
                    or claims["iat"] > now + 30
                    or not isinstance(claims["jti"], str) or not claims["jti"]
                    or not isinstance(claims["client_id"], str) or not claims["client_id"]):
                raise OperatorTokenInvalid("invalid operator access token")
            return subject, account["tenant_id"], account["role"]
        except (jwt.PyJWTError, KeyError, TypeError, ValueError, OverflowError) as exc:
            raise OperatorTokenInvalid("invalid operator access token") from exc
