from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import quote

import pytest

from aiohttp_tiny_mcp import ClientError, Registry
from aiohttp_tiny_mcp.skills import SKILLS_EXTENSION, Skills
from aiohttp_tiny_mcp.testing import connect, over_http


def write_skill(root, name="deploy", *, extra="", newline="\n"):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    content = (
        (
            f"---\nname: {directory.name}\ndescription: Deploy the application.\n"
            f"{extra}---\nFollow references/deployment.md.\n"
        )
        .replace("\n", newline)
        .encode()
    )
    (directory / "SKILL.md").write_bytes(content)
    return directory


@pytest.mark.parametrize("transport", [connect, over_http])
async def test_skill_manifest_matches_every_resource_and_snapshot(tmp_path, transport):
    directory = write_skill(
        tmp_path, extra="metadata:\n  owner: release-team\ncustom: [1, true]\n", newline="\r\n"
    )
    (directory / "references").mkdir()
    (directory / "references/deployment.md").write_text("Проверить сервис.\n")
    (directory / "image.bin").write_bytes(b"\x00\xff\x80")
    (directory / "scripts").mkdir()
    (directory / "scripts/deploy.py").write_text("raise RuntimeError('never execute')\n")
    registry = Registry("skills", "1")
    registry.extension(Skills.from_directory(tmp_path))
    # Reads must use the same snapshot as the digests even if disk contents change.
    original = (directory / "SKILL.md").read_bytes()
    (directory / "SKILL.md").write_text("changed after loading")
    async with transport(registry, adapter="2026-07-28") as client:
        caps = (await client.initialize())["capabilities"]
        assert caps["extensions"] == {SKILLS_EXTENSION: {}}
        assert "resources" in caps
        listed = await client.request_method("skills/list")
        assert listed["ttlMs"] == 0
        assert listed["cacheScope"] == "private"
        (entry,) = listed["skills"]
        assert entry["frontmatter"]["custom"] == [1, True]
        assert entry["frontmatter"]["metadata"] == {"owner": "release-team"}
        assert entry["uri"] == "skill://deploy/SKILL.md"
        assert len(entry["resources"]) == 4
        found = await client.request_method("skills/get", {"uri": entry["uri"]})
        assert found["skill"] == entry
        assert {item.uri for item in await client.list_resources()} == {
            item["uri"] for item in entry["resources"]
        }
        for resource in entry["resources"]:
            contents = (await client.read_resource(resource["uri"])).contents[0]
            data = (
                contents.text.encode()
                if hasattr(contents, "text")
                else base64.b64decode(contents.blob)
            )
            assert len(data) == resource["size"]
            assert "sha256:" + hashlib.sha256(data).hexdigest() == resource["digest"]
            if resource["uri"] == entry["uri"]:
                assert data == original
        for method, params in [
            ("skills/get", {}),
            ("skills/get", {"uri": "skill://deploy/scripts/deploy.py"}),
            ("skills/get", {"uri": "skill://unknown/SKILL.md"}),
            ("skills/list", {"cursor": "unknown"}),
        ]:
            with pytest.raises(ClientError) as error:
                await client.request_method(method, params)
            assert error.value.code == -32602
        with pytest.raises(ClientError):
            await client.read_resource("skill://deploy/../secret")
        with pytest.raises(ClientError):
            await client.read_resource(f"mcp-extensions://{SKILLS_EXTENSION}/manifest.json")


async def test_nested_skills_are_flat_and_paginated_with_complete_manifests(tmp_path):
    write_skill(tmp_path, "deploy")
    write_skill(tmp_path, "deploy/nested")
    write_skill(tmp_path, "team/deploy")
    registry = Registry("nested", "1")
    registry.extension(Skills.from_directory(tmp_path, page_size=1))
    async with connect(registry, adapter="2026-07-28") as client:
        entries = []
        params = {}
        while True:
            page = await client.request_method("skills/list", params)
            entries.extend(page["skills"])
            if "nextCursor" not in page:
                break
            params = {"cursor": page["nextCursor"]}
        assert [item["uri"] for item in entries] == [
            "skill://deploy/SKILL.md",
            "skill://deploy/nested/SKILL.md",
            "skill://team/deploy/SKILL.md",
        ]
        assert len(entries[0]["resources"]) == 2
        assert entries[0]["resources"][1] == entries[1]["resources"][0]


async def test_single_skill_directory_and_legacy_uri(tmp_path):
    directory = write_skill(tmp_path)
    registry = Registry("legacy", "1")
    registry.extension(Skills.from_directory(directory))
    async with connect(registry, adapter="2025-11-25") as client:
        uri = f"mcp-extensions://{SKILLS_EXTENSION}/deploy/SKILL.md"
        assert uri in {item.uri for item in await client.list_resources()}
        result = await client.read_resource(uri)
        assert result.contents[0].text == (directory / "SKILL.md").read_text()
        prefix = f"mcp-extensions://{SKILLS_EXTENSION}/"
        listing = json.loads((await client.read_resource(prefix + "skills/list")).contents[0].text)
        assert listing["skills"][0]["uri"] == uri
        assert listing["skills"][0]["resources"][0]["uri"] == uri
        params = quote(json.dumps({"uri": uri}), safe="")
        detail = await client.read_resource(prefix + "skills/get?params=" + params)
        assert json.loads(detail.contents[0].text)["skill"] == listing["skills"][0]


@pytest.mark.parametrize(
    "body",
    [
        "no frontmatter",
        "---\nname: deploy\n",
        "---\n[]\n---\n",
        "---\nname: other\ndescription: Valid\n---\n",
        "---\nname: deploy\ndescription: ''\n---\n",
        "---\nname: deploy\ndescription: Valid\ncustom: 2026-01-01\n---\n",
        "---\nname: deploy\ndescription: Valid\ncustom: {1: value}\n---\n",
        "---\nname: deploy\ndescription: Valid\nmetadata: {version: 1}\n---\n",
        "---\nname: deploy\ndescription: [invalid yaml\n---\n",
    ],
)
def test_invalid_frontmatter_is_rejected(tmp_path, body):
    directory = write_skill(tmp_path)
    (directory / "SKILL.md").write_text(body)
    with pytest.raises(ValueError):
        Skills.from_directory(tmp_path)


@pytest.mark.parametrize("directory_link", [False, True])
def test_symlinks_are_rejected(tmp_path, directory_link):
    directory = write_skill(tmp_path)
    target = tmp_path / "outside"
    if directory_link:
        target.mkdir()
    else:
        target.write_text("private")
    (directory / "link").symlink_to(target, target_is_directory=directory_link)
    with pytest.raises(ValueError, match="symlinks"):
        Skills.from_directory(directory)


def test_skill_limits_and_empty_directories(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="no SKILL.md"):
        Skills.from_directory(tmp_path)
    directory = write_skill(tmp_path)
    (directory / "extra").write_bytes(b"abc")
    monkeypatch.setattr("aiohttp_tiny_mcp.skills.MAX_RESOURCES", 1)
    with pytest.raises(ValueError, match="files"):
        Skills.from_directory(tmp_path)
    monkeypatch.setattr("aiohttp_tiny_mcp.skills.MAX_RESOURCES", 512)
    monkeypatch.setattr("aiohttp_tiny_mcp.skills.MAX_BYTES", 10)
    with pytest.raises(ValueError, match="bytes"):
        Skills.from_directory(tmp_path)
