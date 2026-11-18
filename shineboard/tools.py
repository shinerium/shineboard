# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright 2026 The Shinerium Authors
# Part of ShineBoard, the Shinerium sessions' board.

"""The MCP tools the sessions call.

Each returns compact text: a session reads only what it asked for. The tools'
descriptions are in every session's context, so they are short.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

import anyio
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from . import board, db

SessionArg = Annotated[str, Field(description="Your session's prefix, such as COM or FHS")]

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
SET = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)


class Store:
    """Runs a board function with a connection of its own, off the event loop."""

    def __init__(self, path: str):
        self.path = path

    async def run(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        def call() -> Any:
            with db.opened(self.path) as conn:
                return fn(conn, *args, **kwargs)

        try:
            return await anyio.to_thread.run_sync(call)
        except board.BoardError as e:
            raise ToolError(str(e)) from e


def _client() -> str:
    token = get_access_token()
    return token.client_id if token is not None else ""


def register(server: MCPServer, store: Store) -> None:
    @server.tool(
        description=(
            "Start every session here: what waits for your session, the notices to all since your last brief, "
            "the answers to your messages, your tasks and new decisions. A few KB; then read what you need."
        ),
        annotations=SET,
        structured_output=False,
    )
    async def board_brief(session: SessionArg) -> str:
        return await store.run(board.brief, session)

    @server.tool(
        description="Read messages in full: the body and every recipient's part of the status, with its note.",
        annotations=READ,
        structured_output=False,
    )
    async def board_read(ids: Annotated[list[int], Field(description="Message numbers, such as [412, 415]; at most 10")]) -> str:
        return await store.run(board.read, ids)

    @server.tool(
        description=(
            "Post a message from your session. Each session named in `to` gets an open part to answer; `all` is "
            "a notice to every session (no answer needed, or in force until `all_until`). The title is one line; "
            "the body is the notice as on the old board: what, its sources, Needs:, Touched outside my area:, For <session>:."
        ),
        annotations=WRITE,
        structured_output=False,
    )
    async def board_post(
        sender: SessionArg,
        to: Annotated[list[str], Field(description="Recipients: session prefixes, and/or all")],
        title: Annotated[str, Field(description="One line, what the message is about")],
        body: Annotated[str, Field(description="The message (Markdown allowed); point to docs rather than copy them")],
        reply_to: Annotated[int | None, Field(description="The message this answers, if any")] = None,
        all_until: Annotated[str, Field(description="For a notice to all that stays in force: until what, such as 'the next genesis'")] = "",
    ) -> str:
        return await store.run(board.post, sender, to, title, body, reply_to, all_until, client=_client())

    @server.tool(
        description=(
            "Set your session's part of a message's status. States: noted, done, answered, declined, blocked, "
            "waiting, open, no-answer. The note says what you did and where (doc and section). The sender may "
            "withdraw its message (state=withdrawn) or keep the part of all (part='all': in-force, no-answer, done)."
        ),
        annotations=SET,
        structured_output=False,
    )
    async def board_set_part(
        id: Annotated[int, Field(description="The message number")],
        session: SessionArg,
        state: Annotated[str, Field(description="The new state")],
        note: Annotated[str, Field(description="What you did, briefly, with where it is recorded")] = "",
        part: Annotated[str, Field(description="Leave empty for your own part; 'all' for the sender's part of all")] = "",
    ) -> str:
        return await store.run(board.set_part, id, session, state, note, part, client=_client())

    @server.tool(
        description=(
            "Search every message, old ones too, by words (title, body and the parts' notes), or list them by "
            "sender, recipient, state and date. Newest first, or by relevance with words."
        ),
        annotations=READ,
        structured_output=False,
    )
    async def board_find(
        query: Annotated[str, Field(description="Words to find, such as 'ShineKey repository'; empty to list")] = "",
        sender: Annotated[str, Field(description="Only messages from this session")] = "",
        to: Annotated[str, Field(description="Only messages to this session (or all)")] = "",
        state: Annotated[str, Field(description="Only messages with a part in this state (with `to`: that session's part)")] = "",
        since: Annotated[str, Field(description="From this date, YYYY-MM-DD")] = "",
        until: Annotated[str, Field(description="To this date, YYYY-MM-DD")] = "",
        active_only: Annotated[bool, Field(description="Only messages still open, in force or changed in the last 7 days")] = False,
        limit: Annotated[int, Field(description="At most this many, 1 to 50")] = 20,
    ) -> str:
        return await store.run(board.find, query, sender, to, state, since, until, active_only, limit)

    @server.tool(
        description="List tasks (work items): a session's, or all; open ones unless you ask for others.",
        annotations=READ,
        structured_output=False,
    )
    async def board_tasks(
        session: Annotated[str, Field(description="Only this session's tasks; empty for all")] = "",
        status: Annotated[str, Field(description="open, in-progress, blocked, done or dropped")] = "",
        include_closed: Annotated[bool, Field(description="Also done and dropped tasks")] = False,
    ) -> str:
        return await store.run(board.tasks, session, status, include_closed)

    @server.tool(
        description="Create a task of your session, or update one of yours by its id or ref (only the fields you give change).",
        annotations=WRITE,
        structured_output=False,
    )
    async def board_set_task(
        session: SessionArg,
        title: Annotated[str, Field(description="One line; needed for a new task")] = "",
        status: Annotated[str, Field(description="open, in-progress, blocked, done or dropped")] = "",
        ref: Annotated[str, Field(description="The work item's ID, such as COM-12, if it has one")] = "",
        id: Annotated[int | None, Field(description="The task's number, to update it")] = None,
        detail: Annotated[str | None, Field(description="A few lines: what is left, where the work is recorded")] = None,
        message_id: Annotated[int | None, Field(description="The message the task comes from")] = None,
    ) -> str:
        return await store.run(board.set_task, session, title, status, ref, id, detail, message_id, client=_client())

    @server.tool(
        description="The decisions recorded on the board (the user's, mostly): search by words, by the areas they concern or by date.",
        annotations=READ,
        structured_output=False,
    )
    async def board_decisions(
        query: Annotated[str, Field(description="Words to find; empty for the newest")] = "",
        area: Annotated[str, Field(description="Only decisions that concern this session")] = "",
        since: Annotated[str, Field(description="Decided on or after this date, YYYY-MM-DD")] = "",
        include_old: Annotated[bool, Field(description="Also superseded and revoked ones")] = False,
    ) -> str:
        return await store.run(board.decisions, query, area, since, include_old)

    @server.tool(
        description=(
            "Record a decision under a short key, with the date it was made and the doc that records it. Recording "
            "a key again replaces it; `supersedes` marks an older decision superseded."
        ),
        annotations=WRITE,
        structured_output=False,
    )
    async def board_record_decision(
        session: SessionArg,
        key: Annotated[str, Field(description="A short name, such as security-contact")],
        title: Annotated[str, Field(description="One line")],
        decision: Annotated[str, Field(description="What was decided, in a few sentences")],
        decided_on: Annotated[str, Field(description="YYYY-MM-DD")],
        areas: Annotated[list[str] | None, Field(description="The sessions it concerns")] = None,
        home: Annotated[str, Field(description="Where it is recorded, such as license/LicensingApproach.md section 6")] = "",
        decided_by: Annotated[str, Field(description="Who decided: user unless otherwise")] = "user",
        supersedes: Annotated[str, Field(description="The key of a decision this one replaces")] = "",
    ) -> str:
        return await store.run(
            board.record_decision, session, key, title, decision, decided_on, areas, home, decided_by, supersedes, client=_client()
        )

    @server.tool(
        description="An overview: for each session, how many parts wait for it and how many of its messages others have open.",
        annotations=READ,
        structured_output=False,
    )
    async def board_overview() -> str:
        return await store.run(board.overview)
