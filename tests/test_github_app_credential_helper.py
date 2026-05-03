from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.tools import github_app_credential_helper


def test_github_app_credential_helper_ignores_non_github_hosts() -> None:
    response = github_app_credential_helper.credential_response(
        {
            "protocol": "https",
            "host": "example.com",
            "path": "acme-corp/erp-service.git",
        },
        settings=Settings(github_app_git_credential_enabled=True),
    )

    assert response == ""


def test_github_app_credential_helper_ignores_disallowed_owner() -> None:
    response = github_app_credential_helper.credential_response(
        {
            "protocol": "https",
            "host": "github.com",
            "path": "GunnerBot/agentic-sdlc-platform.git",
        },
        settings=Settings(
            github_app_git_credential_enabled=True,
            github_app_git_credential_allowed_owners="acme-corp",
        ),
    )

    assert response == ""


def test_github_app_credential_helper_returns_installation_credentials(monkeypatch) -> None:
    monkeypatch.setattr(
        github_app_credential_helper,
        "_installation_token",
        lambda settings: "installation-token",
    )

    response = github_app_credential_helper.credential_response(
        {
            "protocol": "https",
            "host": "github.com",
            "path": "acme-corp/erp-service.git",
        },
        settings=Settings(
            github_app_git_credential_enabled=True,
            github_app_git_credential_allowed_owners="acme-corp",
        ),
    )

    assert response == "username=x-access-token\npassword=installation-token\n\n"
