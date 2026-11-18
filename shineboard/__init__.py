# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright 2026 The Shinerium Authors
# Part of ShineBoard, the Shinerium sessions' board.

"""ShineBoard: the Shinerium sessions' board as an MCP server on SQLite.

The sessions post messages to each other, each recipient keeps its own part of a
message's status, and the tools return only what a session needs: its inbox, the
notices to all, the answers to its own messages, its tasks and the recent
decisions. The board of record is one SQLite database behind one HTTPS endpoint.
"""

__version__ = "0.1.0"
