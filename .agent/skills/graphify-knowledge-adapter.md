# Skill: Graphify Knowledge Adapter

Use when adding or changing Graphify integration.

Rules:

- Treat Graphify as a knowledge graph provider behind `KnowledgeGraphPort`.
- Keep repo indexing separate from query execution.
- Return references that the caller can audit.
- Do not assume Graphify is always present; disabled mode must be deterministic.
- Define timeout and cache behavior before large-repo usage.
- Tests must cover disabled, missing-index, and configured paths.
