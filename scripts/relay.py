#!/usr/bin/env python3
"""Match posts from parallel Claude Code sessions for local review.

  <data>/feed.jsonl        shared feed (one JSON object per line)
  <data>/state/<sid>.json  per-session cursor, review inbox and notice state
  <data>/errors.log        swallowed hook errors (the hooks never block a tool)

<data> = $RELAY_ROOT, else $CLAUDE_PLUGIN_DATA, else ~/.session-relay.

Posting: Stop shares the host-provided final message, and the CLI can post by hand.
Receiving: PostToolUse matches peer posts, saves them to a local review inbox, and
returns only a notice to the user. Stdlib only, local only, no network.
"""
import fcntl
import hashlib
import json
import math
import os
import re
import sys
import time
from contextlib import contextmanager

if __name__ == "__main__":
    # Isolated Python omits the script directory from its import path.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import relay_storage as storage

ROOT = storage.data_root()
FEED_NAME = "feed.jsonl"
FEED = os.path.join(ROOT, FEED_NAME)
STATE_NAME = "state"
LOCK_NAME = ".feed.lock"
MAX_STATE_BYTES = 512_000

# Tuned on fixtures in tests/test_relay.py; see README.md.
THRESHOLD = float(os.environ.get("RELAY_THRESHOLD", "0.12"))
MIN_SHARED = 2              # at least two distinctive shared terms
MIN_IDF_SHARED = 4.5        # a shared term counts only if this distinctive (in ~2% of posts or fewer)
MAX_AGE_S = 6 * 3600        # ignore posts older than this
COOLDOWN_S = 300           # at most one matching notice per session per five minutes
MAX_POST_CHARS = 700
MAX_FEED_LINE_BYTES = 16_384
MAX_SESSION_CHARS = 128
MAX_TIMESTAMP = 253_402_300_799  # last second of the year 9999
FEED_MAX_BYTES = min(16_000_000, max(1, int(os.environ.get("RELAY_FEED_MAX_BYTES", "4000000"))))
FEED_TAIL_BYTES = max(MAX_FEED_LINE_BYTES + 128, FEED_MAX_BYTES)
HOOK_FEED_READ_BYTES = min(256_000, FEED_TAIL_BYTES)
HOOK_FEED_READ_LINES = 256
GENERATION_HEADER = re.compile(rb'\{"relay_generation":"([0-9a-f]{32})"\}\n')
POST_FIELD_LIMITS = {"id": 96, "session": MAX_SESSION_CHARS, "title": 120, "source": 32,
                     "ref": 256, "text": MAX_POST_CHARS}


STOP = set("""
a about above after again against all also am an and any are as at be because been before being below
between both but by can could did do does doing done down during each few for from further had has have
having he her here hers him his how i if in into is it its itself just me more most my no nor not now of
off on once only or other our ours out over own same she should so some such than that the their them
then there these they this those through to too under until up very was we were what when where which
while who whom why will with would you your yours yes ok okay one two three get got make made still yet
let lets need needs new now via per not nothing something anything thing things way also already
""".split())
# Everyday software-work chatter: common in any dev session, rare in the docstring baseline,
# so without this list two unrelated sessions "match" on words like readme / release / test.
STOP |= set("""
test tests testing tested readme release releases released fix fixed fixes fixing bug bugs failing
failed fails fail update updated updates file files code commit commits branch merge merged build
builds built deploy deployed done change changes changed issue issues todo docs doc note notes run
runs ran running check checks checked work works worked working pass passes passed review reviewed
added add adds remove removed version step steps task tasks session sessions look looked
""".split())
TOKEN = re.compile(r"[a-z0-9][a-z0-9_\-\./]*[a-z0-9]|[a-z0-9]")


def tokens(text):
    out = []
    for t in TOKEN.findall((text or "").lower()):
        t = t.strip("./-_")
        if len(t) < 3 and not t.isdigit():
            continue
        if t in STOP or t.isdigit() and len(t) < 3:
            continue
        out.append(t)
    return out


# ---------------------------------------------------------------- feed

def valid_post(p):
    """Validate the persisted fields shared by reception, maintenance and terminal display."""
    if not isinstance(p, dict) or not all(p.get(k) for k in ("id", "text", "session")):
        return False
    if not all(isinstance(p.get(k, ""), str) and len(p.get(k, "")) <= limit
               for k, limit in POST_FIELD_LIMITS.items()):
        return False
    try:
        ts = float(p.get("ts"))
        # JSON can contain escaped lone surrogates which UTF-8 consumers cannot encode.
        for k in POST_FIELD_LIMITS:
            p.get(k, "").encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        return False
    if isinstance(p.get("ts"), bool) or not 0 <= ts <= MAX_TIMESTAMP:
        return False
    p["ts"] = ts
    return True


def encode_post(post):
    record = {k: post[k] for k in (*POST_FIELD_LIMITS, "ts") if k in post}
    return (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")


def read_records(feed, offset, budget, skipping=False, line_limit=None):
    """Read a bounded batch, returning a resumable position even across oversized lines."""
    feed.seek(offset)
    remaining = budget
    posts = []
    while remaining > 0 and (line_limit is None or line_limit > 0):
        raw = feed.readline(min(remaining, MAX_FEED_LINE_BYTES + 1))
        if not raw:
            break
        complete = raw.endswith(b"\n")
        oversized = len(raw) > MAX_FEED_LINE_BYTES
        if not complete and not skipping and not oversized:
            # Retry a normal partial line from its start when more input is available.
            break
        offset += len(raw)
        remaining -= len(raw)
        if line_limit is not None:
            line_limit -= 1
        if skipping or oversized:
            skipping = not complete
            continue
        try:
            record = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):
            continue
        if valid_post(record):
            posts.append(record)
    return posts, offset, skipping


def tail_start(feed, size):
    """Find the window start and whether it lies inside a partial record."""
    offset = max(0, size - FEED_TAIL_BYTES)
    if not offset:
        return 0, False
    feed.seek(offset - 1)
    return offset, feed.read(1) != b"\n"


def feed_generation(feed, info):
    # Compaction writes an explicit marker. A feed from an older release is append-only
    # until the next compaction, so its device and inode suffice for that generation.
    header = GENERATION_HEADER.fullmatch(feed.readline(128))
    marker = header[1].decode("ascii") if header else ""
    return [info.st_dev, info.st_ino, marker]


@contextmanager
def open_feed():
    with storage.directory(ROOT, private=True) as root:
        with os.fdopen(storage.open_file(root, FEED_NAME, os.O_RDONLY), "rb") as feed:
            yield feed


def recent_records(feed, offset=None):
    size = os.fstat(feed.fileno()).st_size
    start, skipping = tail_start(feed, size) if offset is None else (offset, False)
    posts, end, _ = read_records(feed, start, min(size - start, FEED_TAIL_BYTES), skipping)
    return posts, end


def read_feed(offset=None):
    """Read a bounded range. By default use the latest window for CLI consumers."""
    try:
        with open_feed() as feed:
            return recent_records(feed, offset)
    except FileNotFoundError:
        return [], 0


@contextmanager
def feed_writer(root):
    flags = os.O_WRONLY | os.O_CREAT
    with os.fdopen(storage.open_file(root, LOCK_NAME, flags), "wb") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        named_lock = os.stat(LOCK_NAME, dir_fd=root, follow_symlinks=False)
        if not storage.same_file(named_lock, os.fstat(lock.fileno())):
            raise PermissionError("relay feed lock changed while acquiring it")
        yield


def terminal_text(value, limit=None):
    """Render a peer field on one line using only printable characters."""
    printable = "".join(ch if ch.isprintable() else " " for ch in value)
    return " ".join(printable.split())[:limit]


def post(text, session="manual", title="", source="manual", ref=""):
    text = (text or "").strip()
    if not text:
        raise ValueError("empty post")
    rec = {"ts": time.time(), "session": session, "title": title[:120], "source": source,
           "ref": ref, "text": text[:MAX_POST_CHARS]}
    if not isinstance(session, str):
        raise ValueError("invalid session")
    rec["id"] = "%s-%d-%s" % (session[:8], time.time_ns(), os.urandom(4).hex())
    if not valid_post(rec):
        raise ValueError("invalid post metadata")
    line = encode_post(rec)
    with storage.directory(ROOT, create=True, private=True) as root, feed_writer(root):
        fd = storage.open_file(root, FEED_NAME, os.O_RDWR | os.O_APPEND | os.O_CREAT)
        with os.fdopen(fd, "a+b") as feed:
            if feed.seek(0, os.SEEK_END):
                feed.seek(-1, os.SEEK_END)
                if feed.read(1) != b"\n":
                    # Isolate a partial line left by an interrupted writer.
                    line = b"\n" + line
            feed.write(line)
            feed.flush()
            size = os.fstat(feed.fileno()).st_size
        if size > FEED_MAX_BYTES:
            try:
                compact_feed_locked(root)
            except OSError:
                # The append has succeeded. The next publisher can retry compaction.
                storage.log_error(ROOT, "compact_feed")
    return rec


def compact_feed():
    """Retain recent posts and publish a new generation under the writer lock."""
    with storage.directory(ROOT, private=True) as root, feed_writer(root):
        compact_feed_locked(root)


def compact_feed_locked(root):
    with os.fdopen(storage.open_file(root, FEED_NAME, os.O_RDONLY), "rb") as feed:
        posts, _ = recent_records(feed)
    cutoff = time.time() - MAX_AGE_S
    recent = [p for p in posts if p["ts"] >= cutoff]
    header = ('{"relay_generation":"%s"}\n' % os.urandom(16).hex()).encode("ascii")
    target = int(FEED_MAX_BYTES * 0.8)
    keep, total = [], len(header)
    for post in reversed(recent or posts):
        line = encode_post(post)
        if keep and total + len(line) > target:
            break
        keep.append(line)
        total += len(line)
    with storage.replacement(root, FEED_NAME, "wb") as f:
        f.write(header)
        f.writelines(reversed(keep))


# ---------------------------------------------------------------- scoring

CODE_DIR = os.path.dirname(os.path.abspath(__file__))
_BASE = None


def base_df():
    """Frozen word frequencies (base_df.json, Python standard-library docstrings) so IDF is
    sane while the live feed is still small. Missing file -> empty baseline."""
    global _BASE
    if _BASE is None:
        try:
            with open(os.path.join(CODE_DIR, "base_df.json")) as f:
                b = json.load(f)
            _BASE = (int(b["n"]), dict(b["df"]))
        except (OSError, ValueError, KeyError, TypeError):
            _BASE = (0, {})
    return _BASE


def idf_table(docs, with_base=False):
    n, df = (base_df()[0], dict(base_df()[1])) if with_base else (0, {})
    n += len(docs)
    for d in docs:
        for t in set(tokens(d)):
            df[t] = df.get(t, 0) + 1
    return n, df


def idf(t, n, df):
    return math.log((n + 1) / (df.get(t, 0) + 1)) + 1.0


def vec(text, n, df):
    tf = {}
    for t in tokens(text):
        tf[t] = tf.get(t, 0) + 1
    return {t: (1 + math.log(c)) * idf(t, n, df) for t, c in tf.items()}


def score(task_text, post_text, n, df):
    a, b = vec(task_text, n, df), vec(post_text, n, df)
    if not a or not b:
        return 0.0, []
    shared = [t for t in a if t in b and idf(t, n, df) >= MIN_IDF_SHARED]
    if len(shared) < MIN_SHARED:
        return 0.0, shared
    dot = sum(a[t] * b[t] for t in a if t in b)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    shared.sort(key=lambda t: -a[t] * b[t])
    return dot / (na * nb), shared


# ---------------------------------------------------------------- state

def session_id_is_valid(sid):
    return isinstance(sid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sid) is not None


def state_name(sid):
    if not session_id_is_valid(sid):
        raise ValueError("invalid relay session ID")
    return sid + ".json"


def load_state(sid):
    name = state_name(sid)
    try:
        with storage.directory(ROOT, private=True) as root:
            with storage.child_directory(root, STATE_NAME, private=True) as state:
                fd = storage.open_file(state, name, os.O_RDONLY)
                with os.fdopen(fd, "rb") as f:
                    data = f.read(MAX_STATE_BYTES + 1)
        if len(data) <= MAX_STATE_BYTES:
            result = json.loads(data)
            if isinstance(result, dict):
                return result
    except (FileNotFoundError, ValueError, RecursionError):
        pass
    return {}


def save_state(sid, st):
    name = state_name(sid)
    with storage.directory(ROOT, create=True, private=True) as root:
        with storage.child_directory(root, STATE_NAME, create=True, private=True) as state:
            with storage.replacement(state, name, "w") as f:
                json.dump(st, f)


# ---------------------------------------------------------------- transcript

def first_prompt(feed):
    """First human prompt (the session’s task), read from the start."""
    feed.seek(0)
    for i, raw in enumerate(feed):
        if i > 5000:
            break
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        if r.get("type") == "user" and isinstance((r.get("message") or {}).get("content"), str):
            return r["message"]["content"][:4000]
    return ""


FIRST_SCAN_BYTES = 20_000_000   # a first scan of a huge transcript starts this far from the end


def scan(transcript, st):
    """Incrementally read new transcript lines since st['tscan'] and keep, in state:
    the last 4 human prompts, the last 5 own wrap_up recaps (id, text) and the title.
    Only lines that can matter are JSON-parsed."""
    prompts = st.get("prompts", [])
    recaps = st.get("recaps", [])
    with storage.open_transcript(transcript) as f:
        size = os.fstat(f.fileno()).st_size
        off = st.get("tscan")
        reset = type(off) is not int or not 0 <= off <= size
        if reset:
            off = max(0, size - FIRST_SCAN_BYTES)
        if "first" not in st:
            st["first"] = first_prompt(f)
        f.seek(off)
        if off and reset:
            f.readline()                                  # skip a partial first line
        pos = f.tell()
        for raw in f:
            if not raw.endswith(b"\n"):
                break                                     # writer mid-append: read it next time
            pos += len(raw)
            if not (b'"user"' in raw or b"custom-title" in raw or b"wrap_up" in raw):
                continue
            try:
                r = json.loads(raw)
            except ValueError:
                continue
            t = r.get("type")
            if t == "custom-title":
                st["title"] = str(r.get("customTitle") or r.get("title") or "")[:200]
            elif t == "user":
                c = (r.get("message") or {}).get("content")
                if isinstance(c, str) and c.strip() and not c.lstrip().startswith("<"):
                    prompts.append(c[:2000])
            elif t == "assistant":
                for c in (r.get("message") or {}).get("content") or []:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "tool_use" and str(c.get("name", "")).endswith("wrap_up"):
                        rc = str((c.get("input") or {}).get("recap", "")).strip()
                        if rc:
                            recaps.append([c.get("id") or "", rc[:MAX_POST_CHARS]])
    st["tscan"] = pos
    st["prompts"] = prompts[-4:]
    st["recaps"] = recaps[-5:]
    return st


def task_text(transcript, st):
    """This session's current task: first prompt + title + last four human prompts + last two
    own wrap_up recaps. Tool output is left out (it is mostly noise)."""
    scan(transcript, st)
    title = st.get("title", "")
    parts = [st.get("first", ""), title] + st.get("prompts", []) + [r[1] for r in st.get("recaps", [])[-2:]]
    return "\n".join(p for p in parts if p), title


# ---------------------------------------------------------------- hooks

def hook_stop(event):
    sid = event.get("session_id")
    text = event.get("last_assistant_message")
    if not session_id_is_valid(sid) or not isinstance(text, str) or not text.strip():
        return None
    # Claude supplies final text in the Stop event. The transcript can lag the event.
    ref = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
    st = load_state(sid)
    if st.get("last_stop") != ref:
        post(text, session=sid, title=st.get("title", ""), source="stop", ref=ref)
        st["last_stop"] = ref
        save_state(sid, st)
    return None


def eligible_queue(st, now):
    queue = st.get("queue")
    if not isinstance(queue, list):
        return []
    return [p for p in queue if valid_post(p) and
            isinstance(p.get("s"), (int, float)) and 0 <= p["s"] <= 1 and
            now - p["ts"] <= MAX_AGE_S]


def hook_post_tool(event, now=None):
    now = time.time() if now is None else now
    if os.environ.get("RELAY_OFF") == "1":
        return None
    sid = event.get("session_id")
    tp = event.get("transcript_path")
    if not session_id_is_valid(sid) or not isinstance(tp, str) or not tp:
        return None
    st = load_state(sid)
    queue = eligible_queue(st, now)
    pending = st.get("notice_pending", bool(queue))
    try:
        with open_feed() as feed:
            info = os.fstat(feed.fileno())
            generation = feed_generation(feed, info)
            off = st.get("off")
            if st.get("feed_generation") != generation or type(off) is not int or not 0 <= off <= info.st_size:
                off, skipping = tail_start(feed, info.st_size)
            else:
                skipping = st.get("feed_skipping", False)
            budget = min(info.st_size - off, HOOK_FEED_READ_BYTES)
            new, end, skipping = read_records(feed, off, budget, skipping, HOOK_FEED_READ_LINES)
    except FileNotFoundError:
        return None

    previous_cursor = (st.get("feed_generation"), st.get("off"), st.get("feed_skipping"))
    if (generation, end, skipping) == previous_cursor and not pending:
        return None
    st.update(feed_generation=generation, off=end, feed_skipping=skipping)
    others = [p for p in new if p["session"] != sid and now - p["ts"] <= MAX_AGE_S]
    if others:
        ttext, _ = task_text(tp, st)
        n, df = idf_table([p["text"] for p in new] + [ttext], with_base=True)
        history = st.get("matched_ids", st.get("injected", []))
        seen = set(history) | {p["id"] for p in queue}
        for p in others:
            if p["id"] in seen:
                continue
            similarity, _ = score(ttext, p["text"], n, df)
            if similarity >= THRESHOLD:
                queue.append(dict(p, s=round(similarity, 3)))
                seen.add(p["id"])
                history.append(p["id"])
                pending = True
        st["matched_ids"] = history[-2048:]

    queue.sort(key=lambda p: (-p["s"], -p["ts"]))
    st["queue"] = queue[:20]
    notice = None
    if queue and pending and now - st.get("last_notice", 0) >= COOLDOWN_S:
        notice = {"systemMessage": "Session Relay has matching posts for review. In a separate terminal, "
                  "run relay.py inbox --session " + sid + " using the plugin's path."}
        st["last_notice"] = now
        pending = False
    st["notice_pending"] = bool(queue) and pending
    save_state(sid, st)
    return notice


def run_hook(mode, stdin=None):
    """Entry point for relay_hook.py. Never raises, never blocks: errors are logged."""
    try:
        raw = (stdin or sys.stdin).read()
        event = json.loads(raw) if raw.strip() else {}
        if not isinstance(event, dict):
            return 0
        out = hook_post_tool(event) if mode == "post-tool" else hook_stop(event) if mode == "stop" else None
        if out:
            sys.stdout.write(json.dumps(out))
            sys.stdout.flush()
    except Exception:
        storage.log_error(ROOT, "hook " + mode)
    return 0


# ---------------------------------------------------------------- CLI

def print_posts(posts, text_limit=200):
    for p in posts:
        print("%s  %-8s  %s  %s" % (time.strftime("%d %b %H:%M", time.localtime(p["ts"])),
                                    terminal_text(p["session"], 8), terminal_text(p.get("title", ""), 40),
                                    terminal_text(p["text"], text_limit)))


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description="cross-session idea relay")
    sub = ap.add_subparsers(dest="cmd")
    session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    publisher = sub.add_parser("post")
    publisher.add_argument("text")
    publisher.add_argument("--session", default=session or "manual")
    publisher.add_argument("--title", default="")
    tail = sub.add_parser("tail")
    tail.add_argument("-n", type=int, default=10)
    inbox = sub.add_parser("inbox")
    inbox.add_argument("--session", default=session)
    scorer = sub.add_parser("score")
    scorer.add_argument("task")
    scorer.add_argument("post")
    a = ap.parse_args(argv)
    if a.cmd == "post":
        r = post(a.text, session=a.session, title=a.title)
        print("posted", terminal_text(r["id"]))
    elif a.cmd == "tail":
        posts, _ = read_feed()
        print_posts(posts[-a.n:] if a.n > 0 else [])
    elif a.cmd == "inbox":
        if not session_id_is_valid(a.session):
            inbox.error("provide a valid session ID with --session")
        print_posts(eligible_queue(load_state(a.session), time.time()), text_limit=MAX_POST_CHARS)
    elif a.cmd == "score":
        posts, _ = read_feed()
        corpus = [x["text"] for x in posts[-3000:]] + [a.task, a.post]
        n, df = idf_table(corpus, with_base=True)
        sc, shared = score(a.task, a.post, n, df)
        print("score %.3f  threshold %.3f  shared %s" % (sc, THRESHOLD, shared[:8]))
    else:
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
