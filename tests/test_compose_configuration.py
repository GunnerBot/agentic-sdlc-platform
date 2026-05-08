from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dev_agent_services_are_profile_gated_without_platform_dependency() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "  dev-agent-services:\n    profiles:\n      - dev" in compose

    platform_block = compose.split("  agentic-sdlc-platform:", maxsplit=1)[1].split(
        "\n\n  dev-agent-services:",
        maxsplit=1,
    )[0]
    depends_on_block = platform_block.split("    environment:", maxsplit=1)[0]
    assert "dev-agent-services" not in depends_on_block


def test_real_compose_overlay_does_not_reference_dev_agent_services() -> None:
    compose = (ROOT / "docker-compose.real.yml").read_text(encoding="utf-8")

    assert "dev-agent-services" not in compose
