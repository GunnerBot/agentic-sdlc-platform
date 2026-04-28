import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from agentic_sdlc_platform.glue.dag_decomposer import Subtask
from agentic_sdlc_platform.glue.task_event_normalizer import NormalizedTaskEvent

FIGMA_URL_RE = re.compile(r"https?://(?:www\.)?figma\.com/[^\s)>\]]+", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s)>\]]+")
MARKDOWN_SIGNAL_RE = re.compile(r"(?im)^\s{0,3}#{1,6}\s+|^\s*[-*]\s+")
IMAGE_EXTENSIONS = {".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}
TEXT_EXTENSIONS = {".md", ".markdown", ".txt"}


@dataclass(frozen=True)
class TextSource:
    kind: str
    title: str
    text: str

    def to_metadata(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "title": self.title,
            "length": len(self.text),
        }


@dataclass(frozen=True)
class DesignAsset:
    kind: str
    title: str
    url: str | None = None
    content_type: str | None = None

    def to_metadata(self) -> dict[str, object]:
        metadata: dict[str, object] = {
            "kind": self.kind,
            "title": self.title,
        }
        if self.url:
            metadata["url"] = self.url
        if self.content_type:
            metadata["content_type"] = self.content_type
        return metadata


@dataclass(frozen=True)
class DesignReference:
    kind: str
    title: str
    url: str
    content_type: str | None = None


@dataclass(frozen=True)
class RepoMatch:
    repo: str
    reason: str

    def to_metadata(self) -> dict[str, object]:
        return {"repo": self.repo, "reason": self.reason}


@dataclass(frozen=True)
class RepoScope:
    scope: str
    repos: tuple[RepoMatch, ...] = field(default_factory=tuple)
    unknown_repos: tuple[str, ...] = field(default_factory=tuple)

    def to_metadata(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "repos": [repo.to_metadata() for repo in self.repos],
            "unknown_repos": list(self.unknown_repos),
        }


@dataclass(frozen=True)
class SpecIngestionBundle:
    source: str
    text_sources: tuple[TextSource, ...]
    design_assets: tuple[DesignAsset, ...]
    repo_scope: RepoScope

    @property
    def selected_repos(self) -> tuple[str, ...]:
        return tuple(match.repo for match in self.repo_scope.repos)

    def to_metadata(self) -> dict[str, object]:
        return {
            "source": self.source,
            "text_sources": [source.to_metadata() for source in self.text_sources],
            "design_assets": [asset.to_metadata() for asset in self.design_assets],
            "repo_scope": self.repo_scope.to_metadata(),
            "asset_count": len(self.design_assets),
        }

    def to_artifact_content(self, task_event: NormalizedTaskEvent) -> dict[str, object]:
        return {
            "source": self.source,
            "task": {
                "source": task_event.source,
                "external_id": task_event.external_id,
                "issue_id": task_event.issue_id,
                "title": task_event.title,
                "url": task_event.url,
                "body": task_event.body,
            },
            "text_sources": [
                {
                    "kind": source.kind,
                    "title": source.title,
                    "text": source.text,
                    "length": len(source.text),
                }
                for source in self.text_sources
            ],
            "design_assets": [asset.to_metadata() for asset in self.design_assets],
            "repo_scope": self.repo_scope.to_metadata(),
            "prompt_suffix": self.prompt_suffix(),
        }

    def prompt_suffix(self) -> str:
        lines = ["", "Ingested Linear spec context:"]
        if self.selected_repos:
            lines.append(f"Repos: {', '.join(self.selected_repos)}")
        if self.repo_scope.unknown_repos:
            lines.append(f"Unknown repo mentions: {', '.join(self.repo_scope.unknown_repos)}")
        if self.design_assets:
            lines.append("Design assets:")
            for asset in self.design_assets:
                descriptor = asset.title
                if asset.url:
                    descriptor = f"{descriptor} ({asset.url})"
                lines.append(f"- {descriptor}")
        return "\n".join(lines)

    def to_repo_subtasks(self) -> list[Subtask]:
        return [
            Subtask(
                id=f"scope_{_node_key(match.repo)}",
                title=f"Scope and implement {match.repo} changes",
                repo=match.repo,
            )
            for match in self.repo_scope.repos
        ]


def ingest_linear_spec(
    payload: dict[str, object],
    task_event: NormalizedTaskEvent,
    registered_repos: list[object],
) -> SpecIngestionBundle | None:
    text_sources = _linear_text_sources(payload, task_event)
    design_assets = _linear_design_assets(payload, text_sources)
    repo_scope = _repo_scope(text_sources, registered_repos)

    has_spec_signal = any(_looks_like_spec(source.text) for source in text_sources)
    if (
        not has_spec_signal
        and not design_assets
        and not repo_scope.repos
        and not repo_scope.unknown_repos
    ):
        return None

    return SpecIngestionBundle(
        source="linear",
        text_sources=tuple(text_sources),
        design_assets=tuple(design_assets),
        repo_scope=repo_scope,
    )


def linear_document_urls(
    payload: dict[str, object],
    task_event: NormalizedTaskEvent,
) -> list[str]:
    seen = set()
    urls = []
    for source in _linear_text_sources(payload, task_event):
        for url in URL_RE.findall(source.text):
            normalized = url.rstrip(".,")
            if normalized not in seen and _is_supported_doc_url(normalized):
                seen.add(normalized)
                urls.append(normalized)
    return urls


def linear_design_urls(
    payload: dict[str, object],
    task_event: NormalizedTaskEvent,
) -> list[str]:
    return [reference.url for reference in linear_design_references(payload, task_event)]


def linear_design_references(
    payload: dict[str, object],
    task_event: NormalizedTaskEvent,
) -> list[DesignReference]:
    seen = set()
    references = []
    text_sources = _linear_text_sources(payload, task_event)
    for source in text_sources:
        for url in FIGMA_URL_RE.findall(source.text):
            normalized = url.rstrip(".,")
            if normalized not in seen:
                seen.add(normalized)
                references.append(
                    DesignReference(kind="figma", title="Figma link", url=normalized)
                )
    for attachment in _linear_attachments(payload):
        title = _attachment_title(attachment)
        url = _str_value(attachment.get("url") or attachment.get("sourceUrl"))
        content_type = _str_value(attachment.get("contentType") or attachment.get("mimeType"))
        if not url:
            continue
        normalized = url.rstrip(".,")
        if _is_figma_url(normalized):
            if normalized not in seen:
                seen.add(normalized)
                references.append(
                    DesignReference(
                        kind="figma",
                        title=title,
                        url=normalized,
                        content_type=content_type,
                    )
                )
        elif _is_image_attachment(title, content_type) and normalized not in seen:
            seen.add(normalized)
            references.append(
                DesignReference(
                    kind="image",
                    title=title,
                    url=normalized,
                    content_type=content_type,
                )
            )
    return references


def _linear_text_sources(
    payload: dict[str, object],
    task_event: NormalizedTaskEvent,
) -> list[TextSource]:
    sources = []
    if task_event.body:
        sources.append(
            TextSource(
                kind="description",
                title="Linear description",
                text=task_event.body,
            )
        )
    for attachment in _linear_attachments(payload):
        text = _str_value(
            attachment.get("content")
            or attachment.get("body")
            or attachment.get("text")
            or attachment.get("description")
        )
        title = _attachment_title(attachment)
        content_type = _str_value(attachment.get("contentType") or attachment.get("mimeType"))
        if text and _is_text_attachment(title, content_type):
            sources.append(TextSource(kind="attachment", title=title, text=text))
    for comment in _linear_comments(payload):
        text = _str_value(comment.get("body") or comment.get("text"))
        if text:
            title = _str_value(comment.get("id")) or "Linear comment"
            sources.append(TextSource(kind="comment", title=title, text=text))
    return sources


def _linear_design_assets(
    payload: dict[str, object],
    text_sources: list[TextSource],
) -> list[DesignAsset]:
    assets: list[DesignAsset] = []
    seen: set[tuple[str, str | None]] = set()
    for attachment in _linear_attachments(payload):
        if _bool_value(_dict_value(attachment.get("metadata")).get("hydrated_design_context")):
            continue
        title = _attachment_title(attachment)
        url = _str_value(attachment.get("url") or attachment.get("sourceUrl"))
        content_type = _str_value(attachment.get("contentType") or attachment.get("mimeType"))
        kind = None
        if url and _is_figma_url(url):
            kind = "figma"
        elif _is_image_attachment(title, content_type):
            kind = "image"
        if kind:
            key = (title, url)
            if key not in seen:
                seen.add(key)
                assets.append(
                    DesignAsset(kind=kind, title=title, url=url, content_type=content_type)
                )

    for source in text_sources:
        for url in FIGMA_URL_RE.findall(source.text):
            key = ("Figma link", url)
            if key not in seen:
                seen.add(key)
                assets.append(DesignAsset(kind="figma", title="Figma link", url=url))
    return assets


def _repo_scope(text_sources: list[TextSource], registered_repos: list[object]) -> RepoScope:
    text = "\n\n".join(source.text for source in text_sources)
    matches: list[RepoMatch] = []
    seen: set[str] = set()
    for repo in registered_repos:
        repo_name = _str_attr(repo, "name")
        if not repo_name:
            continue
        aliases = _repo_aliases(repo)
        if _contains_any_alias(text, aliases):
            seen.add(repo_name)
            matches.append(RepoMatch(repo=repo_name, reason="mentioned_in_spec"))

    unknown = tuple(
        sorted(
            {
                mention
                for mention in _repo_section_mentions(text)
                if mention not in seen
                and not any(mention in _repo_aliases(repo) for repo in registered_repos)
            }
        )
    )
    if len(matches) == 1:
        scope = "single_repo"
    elif len(matches) > 1:
        scope = "multi_repo"
    elif unknown:
        scope = "needs_clarification"
    else:
        scope = "unspecified"
    return RepoScope(scope=scope, repos=tuple(matches), unknown_repos=unknown)


def _linear_attachments(payload: dict[str, object]) -> list[dict[str, object]]:
    data = _dict_value(payload.get("data"))
    candidates = [
        payload.get("attachments"),
        data.get("attachments"),
        data.get("documents"),
        data.get("files"),
    ]
    attachments: list[dict[str, object]] = []
    for candidate in candidates:
        attachments.extend(_extract_nodes(candidate))
    return attachments


def _linear_comments(payload: dict[str, object]) -> list[dict[str, object]]:
    data = _dict_value(payload.get("data"))
    return _extract_nodes(data.get("comments"))


def _extract_nodes(value: object) -> list[dict[str, object]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        nodes = value.get("nodes")
        if isinstance(nodes, list):
            return [item for item in nodes if isinstance(item, dict)]
        return [value]
    return []


def _repo_aliases(repo: object) -> set[str]:
    name = _str_attr(repo, "name")
    aliases = {name} if name else set()
    clone_url = _str_attr(repo, "clone_url")
    if clone_url:
        parsed = urlparse(clone_url)
        path = parsed.path.strip("/")
        if path.endswith(".git"):
            path = path[:-4]
        if path:
            aliases.add(path)
    for alias in list(aliases):
        if "/" in alias:
            aliases.add(alias.split("/")[-1])
    return {alias.lower() for alias in aliases if alias}


def _contains_any_alias(text: str, aliases: set[str]) -> bool:
    lowered = text.lower()
    return any(_contains_alias(lowered, alias) for alias in aliases)


def _contains_alias(lowered_text: str, alias: str) -> bool:
    if "/" in alias:
        return alias in lowered_text
    pattern = rf"(?<![a-z0-9_.-]){re.escape(alias)}(?![a-z0-9_.-])"
    return re.search(pattern, lowered_text) is not None


def _repo_section_mentions(text: str) -> set[str]:
    mentions: set[str] = set()
    in_repo_section = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        heading = line.lower().strip("#: ")
        if line.startswith("#"):
            in_repo_section = "repo" in heading or "repository" in heading
            continue
        if not in_repo_section and not line.lower().startswith(("repo:", "repository:")):
            continue
        normalized = line
        normalized = re.sub(r"(?i)^[-*]\s*", "", normalized).strip()
        normalized = re.sub(r"(?i)^(repo|repository|repositories):\s*", "", normalized).strip()
        normalized = normalized.strip("` ")
        if _looks_like_repo_name(normalized):
            mentions.add(normalized.lower())
    return mentions


def _looks_like_repo_name(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9_.-]+(?:/[a-z0-9_.-]+)?", value.lower()))


def _looks_like_spec(text: str) -> bool:
    return bool(MARKDOWN_SIGNAL_RE.search(text)) and (
        "repo" in text.lower()
        or "design" in text.lower()
        or "figma" in text.lower()
        or "acceptance" in text.lower()
    )


def _attachment_title(attachment: dict[str, object]) -> str:
    return (
        _str_value(attachment.get("title"))
        or _str_value(attachment.get("name"))
        or _str_value(attachment.get("filename"))
        or "Linear attachment"
    )


def _is_text_attachment(title: str, content_type: str | None) -> bool:
    lowered_type = (content_type or "").lower()
    lowered_title = title.lower()
    return (
        lowered_type.startswith("text/")
        or lowered_type in {"text/markdown", "application/markdown"}
        or any(lowered_title.endswith(extension) for extension in TEXT_EXTENSIONS)
    )


def _is_image_attachment(title: str, content_type: str | None) -> bool:
    lowered_type = (content_type or "").lower()
    lowered_title = title.lower()
    return lowered_type.startswith("image/") or any(
        lowered_title.endswith(extension) for extension in IMAGE_EXTENSIONS
    )


def _is_figma_url(url: str) -> bool:
    return FIGMA_URL_RE.match(url) is not None


def _is_supported_doc_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    return (
        host.endswith("notion.so")
        or host.endswith("notion.site")
        or host == "docs.google.com"
    )


def _node_key(repo: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", repo.lower()).strip("_") or "repo"


def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _bool_value(value: object) -> bool:
    return value if isinstance(value, bool) else False


def _str_value(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _str_attr(value: object, name: str) -> str | None:
    attr = getattr(value, name, None)
    return attr if isinstance(attr, str) and attr else None
