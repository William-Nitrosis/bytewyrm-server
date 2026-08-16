from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

import jwt
from fastapi import Request
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientError, PyJWTError


ACCESS_JWT_HEADER = "Cf-Access-Jwt-Assertion"
TEAM_DOMAIN_ENV = "CLOUDFLARE_ACCESS_TEAM_DOMAIN"
AUDIENCE_ENV = "CLOUDFLARE_ACCESS_AUD"


@dataclass(frozen=True, slots=True)
class CloudflareAccessIdentity:
    """Verified identity carried by a Cloudflare Access application token."""

    email: str
    subject: str | None
    country: str | None


class CloudflareAccessError(Exception):
    """Base error for Cloudflare Access identity validation."""


class CloudflareAccessNotConfigured(CloudflareAccessError):
    """A token reached the app but Access validation is not configured."""


class CloudflareAccessInvalidToken(CloudflareAccessError):
    """The supplied Access JWT could not be cryptographically validated."""


@dataclass(slots=True)
class CloudflareAccessVerifier:
    team_domain: str | None
    audience: str | None
    _jwks_client: PyJWKClient | None = None

    @classmethod
    def from_environment(cls) -> CloudflareAccessVerifier:
        team_domain = os.getenv(TEAM_DOMAIN_ENV, "").strip().rstrip("/")
        audience = os.getenv(AUDIENCE_ENV, "").strip()

        if bool(team_domain) != bool(audience):
            raise RuntimeError(
                f"{TEAM_DOMAIN_ENV} and {AUDIENCE_ENV} must either both be set "
                "or both be left empty."
            )

        if not team_domain:
            return cls(team_domain=None, audience=None)

        if "://" not in team_domain:
            team_domain = f"https://{team_domain}"

        parsed = urlparse(team_domain)
        if parsed.scheme != "https" or not parsed.hostname:
            raise RuntimeError(
                f"{TEAM_DOMAIN_ENV} must be an HTTPS Cloudflare Access team domain."
            )

        # The normal Access issuer is <team-name>.cloudflareaccess.com. Keeping
        # this strict prevents a typo from silently turning another issuer into
        # a trusted identity source.
        if not parsed.hostname.endswith(".cloudflareaccess.com"):
            raise RuntimeError(
                f"{TEAM_DOMAIN_ENV} must end in .cloudflareaccess.com."
            )

        return cls(team_domain=team_domain, audience=audience)

    @property
    def configured(self) -> bool:
        return self.team_domain is not None and self.audience is not None

    def _client(self) -> PyJWKClient:
        if not self.configured or self.team_domain is None:
            raise CloudflareAccessNotConfigured(
                "Cloudflare Access identity validation is not configured"
            )

        if self._jwks_client is None:
            self._jwks_client = PyJWKClient(
                f"{self.team_domain}/cdn-cgi/access/certs"
            )
        return self._jwks_client

    def verify(self, token: str) -> CloudflareAccessIdentity:
        if not token:
            raise CloudflareAccessInvalidToken("Missing Cloudflare Access JWT")
        if not self.configured or self.team_domain is None or self.audience is None:
            raise CloudflareAccessNotConfigured(
                "Cloudflare Access identity validation is not configured"
            )

        try:
            signing_key = self._client().get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key=signing_key.key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.team_domain,
                leeway=10,
                options={
                    "require": ["aud", "exp", "iat", "iss", "nbf"],
                },
            )
        except (PyJWTError, PyJWKClientError, ValueError) as exc:
            raise CloudflareAccessInvalidToken(
                "Invalid Cloudflare Access JWT"
            ) from exc

        email = claims.get("email")
        if not isinstance(email, str) or not email.strip():
            # Service-token application JWTs do not carry a verified user email.
            # Multi-tutor authorization needs an actual human identity.
            raise CloudflareAccessInvalidToken(
                "Cloudflare Access JWT does not contain a verified user email"
            )

        subject = claims.get("sub")
        country = claims.get("country")
        return CloudflareAccessIdentity(
            email=email.strip().lower(),
            subject=subject if isinstance(subject, str) and subject else None,
            country=country if isinstance(country, str) and country else None,
        )

    def identity_from_request(
        self,
        request: Request,
    ) -> CloudflareAccessIdentity | None:
        token = request.headers.get(ACCESS_JWT_HEADER)
        if token is None:
            # Direct LAN access intentionally remains valid during the rollout.
            # No identity is inferred from unverified headers.
            return None
        return self.verify(token)


cloudflare_access = CloudflareAccessVerifier.from_environment()
