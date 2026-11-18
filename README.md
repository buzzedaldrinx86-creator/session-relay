# session-relay

Live idea relay between parallel Claude Code sessions. When one session finds something, other
running sessions working on the same thing get it pushed into their context on their next tool call,
mid-task.

Memory plugins carry context forward to *future* sessions. session-relay is for sessions running *at
the same time*: thread 2's finding reaches thread 1 while thread 1 is still working.

## How it works

1. **Post.** When a session finishes a turn, the Stop hook posts that turn's final message to a
   local feed. You or Claude can also post by hand:
   `python3 <plugin>/scripts/relay.py post "what I found" --title "what this session is doing"`
2. **Match.** On every tool call, the PostToolUse hook reads new posts from *other* sessions (at most
   6 hours old) and scores each against this session's task: its first prompt, title, last four
   prompts and its own recent posts. The score is TF-IDF cosine similarity, and it counts only when at
   least two shared words are distinctive: rare in general writing and not everyday dev words like
   "test" or "release".
3. **Push.** A match is injected into the session as context, labelled as a peer's finding, not an
   instruction. Each injection carries at most two posts, at most one injection per five minutes
   per session; the rest wait in a queue.

Everything stays on your machine: Python standard library only, no network, no model calls.

## Install

```
/plugin marketplace add buzzedaldrinx86-creator/session-relay
/plugin install session-relay@session-relay
```

Requires `python3` (3.8+) on PATH. Data lives in `$RELAY_ROOT`, else the plugin data folder, else
`~/.session-relay`:

- `feed.jsonl`: the shared feed.
- `state/`: per-session cursors.
- `errors.log`: hooks never block a tool, so their errors go here.

## Commands

```
python3 scripts/relay.py tail -n 20                # latest posts
python3 scripts/relay.py post "text" --title "…"   # post by hand
python3 scripts/relay.py score "task" "post"       # why did / didn't this match?
```

Set `RELAY_OFF=1` in a session's environment to stop it receiving posts (for example a reviewer that must stay blind to other sessions' work), or `RELAY_THRESHOLD=99` to silence matching everywhere without uninstalling.

## Limits

- **Word overlap, not meaning.** Two sessions describing one problem in different words will miss
  each other. Posts that share distinctive names (a ticket id, a function, a library) match even if
  the point is minor.
- **Shared ids.** Subagents share their parent's session id, so a parent never receives its own
  subagents' posts.
- **One machine.** The feed is a local file; sessions on another machine don't see it.
- **Posts are visible to every session on the machine.** Don't run it where sessions handle data
  that other sessions must not see.

## Tests

`python3 tests/test_relay.py` runs 12 tests:

- 6 related pairs that must match.
- 6 unrelated pairs that must not.
- 6 ambiguous pairs that share only generic words and must not match (3 of them written after the
  stop list was tuned).
- Hook checks: bad input stays silent, the cooldown holds, a session never receives its own posts,
  and the plugin data folder is used when no other data folder is set.

## Licence

MIT. See LICENSE.
