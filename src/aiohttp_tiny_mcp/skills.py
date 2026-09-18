"""Publish a snapshot of local Agent Skills through the MCP Skills extension."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel

from .core import Failure, FailureKind, Rejected
from .extensions import Extension
from .models import CacheableResult, ListParams, Model, ReadResourceParams

SKILLS_EXTENSION = "io.modelcontextprotocol/skills"
MAX_RESOURCES = 512
MAX_BYTES = 16 * 1024 * 1024


class SkillResource(Model):
    uri: str
    digest: str
    size: int


class Skill(Model):
    uri: str
    frontmatter: dict[str, Any]
    resources: list[SkillResource]


class ListSkillsResult(CacheableResult):
    skills: list[Skill]
    next_cursor: str | None = None


class GetSkillResult(CacheableResult):
    skill: Skill


class Nothing(BaseModel):
    pass


def file_handler(content: bytes) -> Callable[..., Awaitable[str | bytes]]:
    try:
        value: str | bytes = content.decode("utf-8")
    except UnicodeDecodeError:
        value = content

    async def read(args: Nothing) -> str | bytes:
        return value

    return read


def frontmatter(content: bytes, directory_name: str) -> dict[str, Any]:
    try:
        import yaml
    except ImportError:
        raise ImportError("install aiohttp-tiny-mcp[skills] to load skill directories") from None

    lines = content.decode("utf-8").splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("SKILL.md must start with YAML frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise ValueError("SKILL.md frontmatter has no closing delimiter") from None
    try:
        value = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as error:
        raise ValueError(f"invalid SKILL.md frontmatter: {error}") from error
    if not isinstance(value, dict):
        raise ValueError("SKILL.md frontmatter must be an object")
    # JSON round-tripping rejects YAML-only values and cyclic aliases. Comparing
    # the result also rejects non-string mapping keys instead of changing them.
    try:
        serialized = json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError, RecursionError):
        raise ValueError("SKILL.md frontmatter must contain JSON-compatible values") from None
    if serialized != value:
        raise ValueError("SKILL.md frontmatter must use string mapping keys")
    name = value.get("name")
    if (
        not isinstance(name, str)
        or not 1 <= len(name) <= 64
        or name != directory_name
        or name.startswith("-")
        or name.endswith("-")
        or "--" in name
        or any(not (char == "-" or char.isdigit() or char.islower()) for char in name)
    ):
        raise ValueError(
            "skill name must match its directory and use lowercase letters/digits/hyphens"
        )
    description = value.get("description")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise ValueError("skill description must contain 1 to 1024 characters")
    for field in ("license", "compatibility", "allowed-tools"):
        if field in value and not isinstance(value[field], str):
            raise ValueError(f"skill {field} must be a string")
    if "compatibility" in value and not 1 <= len(value["compatibility"]) <= 500:
        raise ValueError("skill compatibility must contain 1 to 500 characters")
    if "metadata" in value and (
        not isinstance(value["metadata"], dict)
        or any(not isinstance(item, str) for item in value["metadata"].values())
    ):
        raise ValueError("skill metadata must map strings to strings")
    return value


class Skills(Extension):
    """Serve immutable skill files and manifests without executing scripts.

    Load files before starting the server. Recreate the extension and registry
    to publish changed files, so manifests and resource bytes remain consistent.
    """

    @classmethod
    def from_directory(cls, directory: str | Path, *, page_size: int = 100) -> Skills:
        """Load one skill directory or a tree containing skill directories.

        Requires the ``skills`` extra. Symlinks and special files are rejected.
        Each skill is limited to 512 files and 16 MiB, including nested skills.
        """
        if page_size < 1:
            raise ValueError("page_size must be positive")
        root = Path(directory).resolve(strict=True)
        if not root.is_dir():
            raise ValueError(f"not a skill directory: {root}")
        files: list[Path] = []

        def walk_error(error: OSError) -> None:
            raise error

        for parent, dirs, names in os.walk(root, onerror=walk_error):
            for name in dirs + names:
                path = Path(parent) / name
                if path.is_symlink():
                    raise ValueError(f"skill directories must not contain symlinks: {path}")
            for name in names:
                path = Path(parent) / name
                if not path.is_file():
                    raise ValueError(f"skill directories must contain regular files: {path}")
                files.append(path)
        definitions = sorted(path for path in files if path.name == "SKILL.md")
        if not definitions:
            raise ValueError(f"no SKILL.md files found in {root}")
        base = root.parent if root / "SKILL.md" in definitions else root
        extension = cls(SKILLS_EXTENSION)
        contents: dict[Path, bytes] = {}
        entries: dict[str, Skill] = {}

        def uri(path: Path) -> str:
            return "skill://" + quote(path.relative_to(base).as_posix(), safe="/")

        for definition in definitions:
            members = sorted(path for path in files if definition.parent in path.parents)
            if len(members) > MAX_RESOURCES:
                raise ValueError(f"skill exceeds {MAX_RESOURCES} files: {definition.parent}")
            size = 0
            manifest = []
            for path in members:
                if path not in contents:
                    with path.open("rb") as stream:
                        contents[path] = stream.read(MAX_BYTES + 1)
                content = contents[path]
                size += len(content)
                if size > MAX_BYTES:
                    raise ValueError(f"skill exceeds {MAX_BYTES} bytes: {definition.parent}")
                manifest.append(
                    SkillResource(
                        uri=uri(path),
                        digest="sha256:" + hashlib.sha256(content).hexdigest(),
                        size=len(content),
                    )
                )
            entries[uri(definition)] = Skill(
                uri=uri(definition),
                frontmatter=frontmatter(contents[definition], definition.parent.name),
                resources=manifest,
            )

        ordered = sorted(entries)

        @extension.method("skills/list")
        async def list_skills(args: ListParams) -> ListSkillsResult:
            start = 0
            if args.cursor is not None:
                try:
                    start = ordered.index(args.cursor) + 1
                except ValueError:
                    raise Rejected(
                        Failure(FailureKind.INVALID_PARAMS, "invalid skill cursor")
                    ) from None
            selected = ordered[start : start + page_size]
            more = start + len(selected) < len(ordered)
            return ListSkillsResult(
                skills=[entries[key] for key in selected],
                next_cursor=selected[-1] if more else None,
            )

        @extension.method("skills/get")
        async def get_skill(args: ReadResourceParams) -> GetSkillResult:
            entry = entries.get(args.uri)
            if entry is None:
                raise Rejected(Failure(FailureKind.INVALID_PARAMS, f"unknown skill: {args.uri}"))
            return GetSkillResult(skill=entry)

        for path, content in sorted(contents.items()):
            extension.resource(
                uri(path),
                file_handler(content),
                name=path.relative_to(base).as_posix(),
                mime_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            )
        return extension
