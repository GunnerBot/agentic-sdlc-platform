import base64
import json
import time
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.source_control import (
    SourceControlError,
    SourceInstallation,
    SourceRepository,
)


class GitHubAppSourceControl:
    provider = "github"

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport

    async def list_installation_repositories(self) -> SourceInstallation:
        token = await self._installation_token()
        repositories: list[object] = []
        try:
            async with httpx.AsyncClient(
                base_url=self._settings.github_app_api_base_url,
                timeout=self._settings.github_app_timeout_seconds,
                transport=self._transport,
            ) as client:
                page = 1
                per_page = 100
                while True:
                    response = await client.get(
                        "/installation/repositories",
                        headers={
                            "Accept": "application/vnd.github+json",
                            "Authorization": f"Bearer {token}",
                            "X-GitHub-Api-Version": "2022-11-28",
                        },
                        params={"per_page": per_page, "page": page},
                    )
                    response.raise_for_status()
                    payload = response.json()
                    page_repositories = payload.get("repositories")
                    if not isinstance(page_repositories, list):
                        raise SourceControlError(
                            "github app repository listing returned invalid response"
                        )
                    repositories.extend(page_repositories)
                    if len(page_repositories) < per_page:
                        break
                    page += 1
        except httpx.HTTPError as exc:
            raise SourceControlError("github app repository listing failed") from exc

        return SourceInstallation(
            provider=self.provider,
            installation_id=_required(self._settings.github_app_installation_id),
            account=_account_login(repositories),
            repositories=[
                _source_repository(repository)
                for repository in repositories
                if isinstance(repository, dict)
            ],
        )

    async def installation_token(self) -> str:
        installation_id = _required(self._settings.github_app_installation_id)
        jwt = _github_app_jwt(
            app_id=_required(self._settings.github_app_id),
            private_key_pem=self._private_key_pem(),
        )
        try:
            async with httpx.AsyncClient(
                base_url=self._settings.github_app_api_base_url,
                timeout=self._settings.github_app_timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    f"/app/installations/{installation_id}/access_tokens",
                    headers={
                        "Accept": "application/vnd.github+json",
                        "Authorization": f"Bearer {jwt}",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SourceControlError("github app installation token request failed") from exc

        payload = response.json()
        token = payload.get("token")
        if not isinstance(token, str) or not token:
            raise SourceControlError("github app installation token response missing token")
        return token

    async def _installation_token(self) -> str:
        return await self.installation_token()

    def _private_key_pem(self) -> str:
        if self._settings.github_app_private_key:
            return self._settings.github_app_private_key.replace("\\n", "\n")
        if self._settings.github_app_private_key_path:
            return Path(self._settings.github_app_private_key_path).read_text()
        raise SourceControlError("github app private key is not configured")


def _github_app_jwt(app_id: str, private_key_pem: str) -> str:
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iat": now - 60,
        "exp": now + 540,
        "iss": app_id,
    }
    signing_input = (
        _urlsafe_b64encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        + b"."
        + _urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    )
    key = serialization.load_pem_private_key(
        private_key_pem.encode("utf-8"),
        password=None,
    )
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return (signing_input + b"." + _urlsafe_b64encode(signature)).decode("ascii")


def _urlsafe_b64encode(value: bytes) -> bytes:
    return base64.urlsafe_b64encode(value).rstrip(b"=")


def _source_repository(payload: dict[str, object]) -> SourceRepository:
    return SourceRepository(
        name=_str(payload.get("name")) or "",
        full_name=_str(payload.get("full_name")) or "",
        clone_url=_str(payload.get("clone_url")),
        html_url=_str(payload.get("html_url")),
        default_branch=_str(payload.get("default_branch")) or "main",
        private=payload.get("private") is True,
        permissions={
            key: value
            for key, value in _dict(payload.get("permissions")).items()
            if isinstance(key, str) and isinstance(value, bool)
        },
    )


def _account_login(repositories: list[object]) -> str | None:
    for repository in repositories:
        owner = _dict(_dict(repository).get("owner"))
        login = _str(owner.get("login"))
        if login:
            return login
    return None


def _required(value: str | None) -> str:
    if not value:
        raise SourceControlError("github app configuration is incomplete")
    return value


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}
