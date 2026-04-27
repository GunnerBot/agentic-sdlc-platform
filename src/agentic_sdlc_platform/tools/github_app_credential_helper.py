import sys
from collections.abc import Mapping
from pathlib import Path

import httpx

from agentic_sdlc_platform.adapters.github_app import _github_app_jwt
from agentic_sdlc_platform.core.config import Settings


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else "get"
    if action != "get":
        return 0

    credential = _parse_git_credential(sys.stdin.read())
    settings = Settings()
    response = credential_response(credential, settings=settings)
    if response:
        sys.stdout.write(response)
    return 0


def credential_response(credential: Mapping[str, str], *, settings: Settings) -> str:
    if not _should_handle(credential, settings=settings):
        return ""

    token = _installation_token(settings)
    return f"username=x-access-token\npassword={token}\n\n"


def _should_handle(credential: Mapping[str, str], *, settings: Settings) -> bool:
    if not settings.github_app_git_credential_enabled:
        return False
    if credential.get("protocol") != "https":
        return False

    host = credential.get("host")
    if host not in _csv(settings.github_app_git_credential_hosts):
        return False

    owners = _csv(settings.github_app_git_credential_allowed_owners)
    if not owners:
        return True

    path = credential.get("path") or ""
    owner = path.split("/", 1)[0]
    return owner in owners


def _installation_token(settings: Settings) -> str:
    if not settings.github_app_installation_id:
        raise RuntimeError("ASDLC_GITHUB_APP_INSTALLATION_ID is not configured")
    if not settings.github_app_id:
        raise RuntimeError("ASDLC_GITHUB_APP_ID is not configured")

    jwt = _github_app_jwt(
        app_id=settings.github_app_id,
        private_key_pem=_private_key_pem(settings),
    )
    with httpx.Client(
        base_url=settings.github_app_api_base_url,
        timeout=settings.github_app_timeout_seconds,
    ) as client:
        response = client.post(
            f"/app/installations/{settings.github_app_installation_id}/access_tokens",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {jwt}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        response.raise_for_status()
    token = response.json().get("token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("github app installation token response missing token")
    return token


def _private_key_pem(settings: Settings) -> str:
    if settings.github_app_private_key:
        return settings.github_app_private_key.replace("\\n", "\n")
    if settings.github_app_private_key_path:
        return Path(settings.github_app_private_key_path).read_text()
    raise RuntimeError("github app private key is not configured")


def _parse_git_credential(raw: str) -> dict[str, str]:
    credential: dict[str, str] = {}
    for line in raw.splitlines():
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        credential[key] = value
    return credential


def _csv(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


if __name__ == "__main__":
    raise SystemExit(main())
