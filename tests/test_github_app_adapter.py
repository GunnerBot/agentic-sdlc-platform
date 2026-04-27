import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from agentic_sdlc_platform.adapters.github_app import GitHubAppSourceControl
from agentic_sdlc_platform.core.config import Settings


async def test_github_app_lists_installation_repositories_with_installation_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/app/installations/installation-1/access_tokens":
            assert request.headers["authorization"].startswith("Bearer ")
            return httpx.Response(status_code=201, json={"token": "installation-token"})
        if request.url.path == "/installation/repositories":
            assert request.headers["authorization"] == "Bearer installation-token"
            return httpx.Response(
                status_code=200,
                json={
                    "repositories": [
                        {
                            "name": "agentic-sdlc-platform",
                            "full_name": "GunnerBot/agentic-sdlc-platform",
                            "clone_url": "https://github.com/GunnerBot/agentic-sdlc-platform.git",
                            "html_url": "https://github.com/GunnerBot/agentic-sdlc-platform",
                            "default_branch": "main",
                            "private": True,
                            "owner": {"login": "GunnerBot"},
                            "permissions": {"contents": True, "pull_requests": False},
                        }
                    ]
                },
            )
        return httpx.Response(status_code=404)

    installation = await GitHubAppSourceControl(
        Settings(
            github_app_id="123456",
            github_app_installation_id="installation-1",
            github_app_private_key=_private_key_pem(),
            github_app_api_base_url="https://github.local",
        ),
        transport=httpx.MockTransport(handler),
    ).list_installation_repositories()

    assert [request.url.path for request in requests] == [
        "/app/installations/installation-1/access_tokens",
        "/installation/repositories",
    ]
    assert installation.account == "GunnerBot"
    assert installation.repositories[0].full_name == "GunnerBot/agentic-sdlc-platform"
    assert installation.repositories[0].permissions == {
        "contents": True,
        "pull_requests": False,
    }


def _private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
