"""Handler test doubles for elicitation and remembered permissions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aiohttp_tiny_mcp.protocol.core import Answer, AnswerAction


class FakeHub:
    def __init__(self) -> None:
        self.published: list[tuple[str, Mapping[str, Any]]] = []

    async def publish(self, topic: str, message: Mapping[str, Any]) -> None:
        self.published.append((topic, message))


class FakeRegistry:
    def __init__(self) -> None:
        self.hub = FakeHub()


class FakeSession:
    """Dict-backed session double for handler permission checks."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    async def set(self, key: str, value: Any) -> None:
        self.values[key] = value


class FakeExchange:
    def __init__(
        self,
        *,
        answers: Mapping[str, Mapping[str, Any]] | None = None,
        action: str = "accept",
        session: FakeSession | None = None,
    ) -> None:
        self.answers = dict(answers or {})
        self.session = session
        self.action_taken = AnswerAction(action)
        self.asked: list[str] = []
        self.logged: list[tuple[str, Any]] = []
        self.progress_reports: list[tuple[float, float | None]] = []
        self.registry = FakeRegistry()

    async def ask(self, key: str, request: Mapping[str, Any], *, default: Any = None) -> Answer:
        self.asked.append(request["params"]["message"])
        return Answer(action=self.action_taken, content=self.answers.get(key, {}))

    def action(self, key: str) -> AnswerAction:
        return self.action_taken

    def answered(self, key: str) -> bool:
        return key in self.answers

    def accepted(self, key: str) -> bool:
        return self.action_taken is AnswerAction.ACCEPT

    async def log(self, level: str, data: Any, *, logger: str | None = None) -> None:
        self.logged.append((level, data))

    async def progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        self.progress_reports.append((progress, total))
