"""Validated MCP tool arguments."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

ChannelName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
MessageText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16000)
]


class Empty(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Page(Empty):
    after: int = Field(default=0, ge=0, description="Return messages after this message ID.")
    limit: int = Field(
        default=50, ge=1, le=100, description="Maximum messages in this page (1–100)."
    )


class Channel(Empty):
    channel: ChannelName = Field(description="Channel name from list_channels, without a # prefix.")


class CreateChannel(Empty):
    name: ChannelName = Field(description="New channel name: lowercase letters, digits, - or _.")
    description: str = Field(default="", max_length=1000, description="Purpose of the new channel.")


class PostMessage(Channel):
    text: MessageText = Field(description="Message text; read chat://rules before posting.")
    parent_id: int | None = Field(default=None, gt=0, description="Root message ID for a reply.")


class DirectUser(Empty):
    user: Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.@-]{0,63}$")] = Field(
        description="Recipient in your organization; this does not change your own identity."
    )


class DirectMessage(DirectUser):
    text: MessageText = Field(description="Private message text; read chat://rules before posting.")
    parent_id: int | None = Field(default=None, gt=0, description="Root message ID in this DM.")


class History(Channel, Page):
    pass


class Thread(Page):
    message_id: int = Field(gt=0, description="Root message ID.")


class MarkRead(Channel):
    message_id: int = Field(gt=0, description="Mark messages through this channel message as read.")


class Search(Page):
    query: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
    ] = Field(
        description="FTS5 query: words, quoted phrases, AND/OR/NOT, or prefixes such as rele*."
    )
    channel: ChannelName | None = Field(default=None, description="Limit search to this channel.")
