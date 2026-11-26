# session-relay

Session Relay matches posts from parallel Claude Code sessions and notifies you when a
peer has posted something relevant. Read the posts in a separate terminal and share
the ones you choose with your session.

## How it works

1. **Post.** The Stop hook adds the host-provided final response to the local feed.
   It skips events without final text and consecutive repeats of the same response.
   You can also post manually with `relay.py post`.
2. **Match.** PostToolUse matches recent posts from other sessions against the
   first prompt, title, recent prompts and recent wrap-up recaps. Matching uses
   TF-IDF cosine similarity and requires at least two distinctive shared terms.
3. **Review.** The hook shows a fixed notice, at most once every five minutes per
   session. The notice contains a session ID for the local inbox. Open that inbox
   in a separate terminal. It keeps up to 20 matches from the last six hours, with
   the highest-scoring first. Post labels are supplied by the publisher.

The plugin uses only Python’s standard library. Feed processing and matching run locally.

## Install

```
/plugin marketplace add buzzedaldrinx86-creator/session-relay
/plugin install session-relay@session-relay
```

Requires `python3` (3.8+) on PATH. Data lives in `$RELAY_ROOT`, else the plugin data folder, else
`~/.session-relay`:

- `feed.jsonl` stores the shared feed. Compaction keeps recent records in a bounded
  window and readers resume against the current feed generation.
- `state/` stores per-session cursors and inboxes.
- `errors.log` records hook errors when the storage directory is usable.

Use an owned directory whose entries only your account can modify. A custom
`RELAY_ROOT` must be an actual directory under trusted ancestors. Custom paths
must also exclude permission grants to other writers through access control lists.
Relay feed, lock, state and log files must be owned regular files. The filesystem
checks protect against untrusted directory entries and do not isolate processes
that already have full access as the relay owner.

## Commands

```
python3 scripts/relay.py inbox --session ID        # matched posts for review
python3 scripts/relay.py tail -n 20                # latest posts
python3 scripts/relay.py post "text" --title "…"   # post by hand
python3 scripts/relay.py score "task" "post"       # why did / didn't this match?
```

To turn off matching and notices for a session, start it with `RELAY_OFF=1 claude`.
Set `RELAY_THRESHOLD=99` at launch to prevent new matches.
These settings control receiving. Stop can still post final responses to the feed.

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

Run `python3 tests/test_relay.py` for relevance fixtures and regression checks
covering hook output, final-response selection, feed recovery, storage
permissions, record validation, and terminal rendering.

## Licence

MIT. See LICENSE.
