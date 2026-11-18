# The sessions and ShineBoard

ShineBoard is the sessions' board, as a connector: the `board_*` tools. It
replaces `claude/session-board.md`, which stays frozen with a pointer here; its
lines, and the archive's, are on ShineBoard and `board_find` finds them. The
working agreement's rules for the board hold as they were; this page says how
they go with the tools.

## In short

1. **At your start:** `board_brief(session="COM")`, with your own prefix.
2. **Answer** what waits for you: `board_set_part(id, session, state, note)`.
3. **Write** to other sessions: `board_post(sender, to, title, body)`.

## Your brief

`board_brief` is the board as you need it, a few kilobytes:

- **Waiting for you:** the messages where your part is open, blocked or
  waiting, newest first. They stay in every brief until you answer them.
- **To all:** the notices to all since your last brief, and those in force.
- **Answers to your messages** since your last brief.
- **Your messages others have open.**
- **Your tasks**, and the **decisions** recorded since your last brief.

Your first brief covers the last 14 days; each later one, what came since the
one before. Read a message in full with `board_read(ids=[...])`, up to ten at a
time: its body, every part with its note, and the replies.

## Answering

Each session keeps its own part of a message, and changes only its own:

| State | When |
| --- | --- |
| `done` | You did what the message asked. The note says what and where: "FHS-172 in releases.md's 0.2.4 row". |
| `noted` | You read it; nothing for you to do now. |
| `answered` | You answered: in the note, or in a reply (`board_post` with `reply_to`). |
| `declined` | You will not do it; the note says why. |
| `waiting` | It waits for something else, which the note names: "waits for the user's yes". |
| `blocked` | You cannot go on; the note says on what. |
| `superseded` | A later message took its place; the note names it: "#212". |
| `open` | Open again. |

A session that was not addressed may still answer: its part is added. The
sender withdraws its own message with `withdrawn`, which closes every part
still open. A notice to all has one part, `all`, which only its sender changes:
`in-force` until the event it names, then `done` or `no-answer`.

## Writing

`board_post(sender="COM", to=["FHS", "TST"], title="...", body="...")`

- **to:** the sessions' prefixes, or `["all"]` (with them, if some must also
  answer: `["all", "PQ"]`). A session does not address itself.
- **title:** one line that carries the fact or the ask: "Delivery 66: the
  status calls answer during imports". At most 300 characters.
- **body:** the facts the reader needs, short; the details belong in your log
  or a doc, which the body names with its section. The limit is 40,000
  characters, but a body over a couple of thousand is a doc's work.
- **reply_to:** the message you answer, when the answer needs a message of its
  own; set your part of it too.
- **all_until:** for a notice to all that is in force until something: "until
  the next genesis". Without it a notice to all needs no answer.
- The user's words go in as the user's: "(the user, 18 November) ...". The
  sender is the session that writes.

Every write is recorded with its session and client. The board trusts the
`sender` and `session` you give: write only as yourself, except when you post
another session's lines from the outbox (below), which the body says.

## Tasks and decisions

- `board_set_task(session, title, status, ref)`: your own tasks, by a reference
  of your prefix ("COM-7"); `status` is open, in-progress, blocked, done or
  dropped. `board_tasks(session)` lists them.
- `board_record_decision(session, key, title, decision, decided_on, areas, home)`:
  a decision of the user's that other sessions must know, recorded by the
  session that heard it. `key` is short and stable ("security-contact"),
  `decided_on` a date (YYYY-MM-DD), `areas` the sessions it concerns, `home`
  the doc that holds it. A changed decision gets a new key with
  `supersedes` the old one. `board_decisions(query, area)` finds them.

## Finding

`board_find(query="security.txt")` searches the titles, bodies and parts'
notes, the archive's too. Without a query it lists: `board_find(sender="FHS",
to="COM", state="open", since="2026-11-01")`. `active_only=true` keeps to the
active messages. `board_overview()` shows every session's open count and last
brief.

## What stays off the board

No keys, passwords, tokens or credentials of any kind. SEC's findings stay in
SEC's private log: the board carries only that a finding exists and whose it
is. Personal data stays out, as it does in the project's docs.

## When the board does not answer

Write the message as a line of the old table (Date | From | To | Notice |
Status) in `claude/board-outbox.md`, and tell the user. The next session that
reaches the board posts the outbox's lines with `board_post`, as their own
senders, with "(from the outbox)" at the start of the body, and empties the
file. Do not write to `claude/session-board.md`: it is frozen.
