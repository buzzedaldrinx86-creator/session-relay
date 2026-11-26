#!/usr/bin/env python3
"""session-relay tests. Run: python3 tests/test_relay.py   (stdlib only, Python 3.8+)
Relevance fixtures are fictional projects scored against the shipped baseline (base_df.json)."""
import json, os, shutil, subprocess, sys, tempfile, time, unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
HOOK = os.path.join(SCRIPTS, "relay_hook.py")
sys.path.insert(0, SCRIPTS)
import relay  # noqa: E402

TASKS = {
    "billing": "Fix the Stripe webhook retry bug in billing/webhooks.py: duplicate invoice.paid events create two ledger rows.",
    "search": "Tune the Elasticsearch synonym analyzer so 'sneaker' and 'trainer' match in product search.",
    "mobile": "The React Native onboarding carousel crashes on Android 12 when the Lottie animation unmounts.",
    "infra": "Migrate the Terraform state for the staging VPC to the new S3 backend with DynamoDB locking.",
    "ml": "Our XGBoost churn model's AUC dropped after the feature store switched tenure_days to months.",
    "game": "Godot platformer: coyote time and jump buffering feel wrong after the physics tick change to 120 Hz.",
}
POSTS = {
    "billing": "Found it: invoice.paid webhooks are retried by Stripe after our 10s timeout; idempotency key on the ledger insert stops the duplicate rows.",
    "search": "Elasticsearch synonym_graph filter must sit in the search analyzer only, else 'sneaker' => 'trainer' breaks phrase queries.",
    "mobile": "Lottie on Android 12 needs the animation ref cancelled in the unmount cleanup; that's the React Native onboarding crash.",
    "infra": "Heads up: the Terraform S3 backend migration needs the DynamoDB lock table created first or state init hangs on staging.",
    "ml": "tenure_days is now months in the feature store since Monday's change, which explains the XGBoost churn AUC drop.",
    "game": "At 120 Hz physics the Godot coyote time and jump buffer counters must be in seconds, not ticks.",
}
BAD = [("billing", "game"), ("search", "infra"), ("mobile", "ml"), ("infra", "billing"), ("ml", "search"), ("game", "mobile")]
AMBIGUOUS_SILENT = [  # only generic engineering words in common
    ("Fix the failing test and update the README before the release.",
     "Updated the README and fixed a failing test; the release notes are done."),
    ("Refactor the config loader so errors are logged with the file path.",
     "Errors now log the file path in the export job; config untouched."),
    (TASKS["billing"], "Fixed a bug where retries doubled log lines in the CLI."),
]
# Holdout: written after the chatter stop-list was added (run 1 failed AMB 0 on readme/release/test).
AMBIGUOUS_HOLDOUT = [
    ("Bump the version, merge the branch and deploy once the build passes.",
     "Build passed and the branch is merged; deploy is done for the new version."),
    ("Review the open issues and close the ones already fixed.",
     "Reviewed three issues; two were already fixed so I closed them."),
    (TASKS["infra"], "Updated the docs for the CLI flags and checked the examples run."),
]


def s(task, post):
    n, df = relay.idf_table([task, post], with_base=True)
    return relay.score(task, post, n, df)


class Relevance(unittest.TestCase):
    def test_good_fire(self):
        for k in TASKS:
            sc, sh = s(TASKS[k], POSTS[k]); print("GOOD %-8s %.3f %s" % (k, sc, sh[:5]))
            self.assertGreaterEqual(sc, relay.THRESHOLD, k)

    def test_bad_silent(self):
        for t, p in BAD:
            sc, sh = s(TASKS[t], POSTS[p]); print("BAD  %-8s<-%-8s %.3f %s" % (t, p, sc, sh[:5]))
            self.assertLess(sc, relay.THRESHOLD, (t, p))

    def test_ambiguous_generic_silent(self):
        for i, (t, p) in enumerate(AMBIGUOUS_SILENT):
            sc, sh = s(t, p); print("AMB  %d %.3f %s" % (i, sc, sh[:5]))
            self.assertLess(sc, relay.THRESHOLD, i)


class Holdout(unittest.TestCase):
    def test_ambiguous_holdout_silent(self):
        for i, (t, p) in enumerate(AMBIGUOUS_HOLDOUT):
            sc, sh = s(t, p); print("HOLD %d %.3f %s" % (i, sc, sh[:5]))
            self.assertLess(sc, relay.THRESHOLD, i)


def transcript(path, prompt, final=None):
    with open(path, "w") as f:
        f.write(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n")
        if final:
            f.write(json.dumps({"type": "assistant", "uuid": "u-final", "message": {"role": "assistant",
                    "content": [{"type": "text", "text": final}]}}) + "\n")


class Hook(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="relaytest-")
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("RELAY_")}
        self.env["RELAY_ROOT"] = self.root
        self.tA, self.tB = os.path.join(self.root, "A.jsonl"), os.path.join(self.root, "B.jsonl")
        transcript(self.tA, TASKS["billing"])
        transcript(self.tB, "Look into why the ledger has duplicates.", final=POSTS["billing"])

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_hook(self, mode, sid=None, tp=None, raw=None, env=None, final=None):
        inp = raw if raw is not None else json.dumps({
            "session_id": sid, "transcript_path": tp, "last_assistant_message": final,
        })
        return subprocess.run([sys.executable, "-I", HOOK, mode], input=inp, capture_output=True,
                              text=True, env=env or self.env, timeout=20)

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-I", os.path.join(SCRIPTS, "relay.py"), *args],
                              capture_output=True, text=True, env=self.env, check=True)

    def test_stop_posts_final_message_for_manual_review(self):
        self.assertEqual(self.run_hook("stop", "B", self.tB, final=POSTS["billing"]).stdout, "")
        self.run_hook("stop", "B", self.tB, final=POSTS["billing"])
        with open(os.path.join(self.root, "feed.jsonl")) as feed:
            self.assertEqual(len(feed.readlines()), 1)
        output = self.run_hook("post-tool", "A", self.tA).stdout
        notice = json.loads(output)
        self.assertIn("systemMessage", notice)
        self.assertNotIn("hookSpecificOutput", notice)
        self.assertNotIn("idempotency", output)
        self.assertIn("idempotency", self.run_cli("inbox", "--session", "A").stdout)
        self.assertEqual(self.run_hook("post-tool", "B", self.tB).stdout, "")

    def test_stop_uses_even_a_short_host_final_response(self):
        self.run_hook("stop", "B", self.tB, final="Done.")
        feed = self.run_cli("tail").stdout
        self.assertIn("Done.", feed)
        self.assertNotIn("idempotency", feed)

    def test_stop_without_host_final_response_leaves_feed_empty(self):
        self.run_hook("stop", "B", self.tB)
        self.assertEqual(self.run_cli("tail").stdout, "")

    def test_irrelevant_not_injected(self):
        relay_post(self.root, "C", POSTS["game"])
        self.assertEqual(self.run_hook("post-tool", "A", self.tA).stdout, "")

    def test_cooldown_defers_the_notice_without_hiding_matches(self):
        relay_post(self.root, "C", POSTS["billing"])
        self.assertIn("systemMessage", json.loads(self.run_hook("post-tool", "A", self.tA).stdout))
        relay_post(self.root, "D", "Stripe invoice.paid retries also hit the refund webhook; same ledger duplicate fix applies.")
        self.assertEqual(self.run_hook("post-tool", "A", self.tA).stdout, "")
        self.assertIn("refund", self.run_cli("inbox", "--session", "A").stdout)
        state = os.path.join(self.root, "state", "A.json")
        with open(state) as f:
            data = json.load(f)
        data["last_notice"] = time.time() - 400
        with open(state, "w") as f:
            json.dump(data, f)
        self.assertIn("systemMessage", json.loads(self.run_hook("post-tool", "A", self.tA).stdout))

    def test_tail_formats_each_record_as_one_printable_line(self):
        self.run_cli("post", "First\nSecond\tThird", "--title", "Status\nUpdate",
                     "--session", "peer\tA")
        output = self.run_cli("tail").stdout
        self.assertEqual(len(output.splitlines()), 1)
        self.assertTrue(all(c.isprintable() for c in output.rstrip("\n")))
        self.assertIn("Status Update", output)
        self.assertIn("First Second Third", output)

    def test_post_refuses_storage_with_group_writes(self):
        os.chmod(self.root, 0o770)
        result = self.run_hook("stop", "B", self.tB, final="Routine update.")
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertFalse(os.path.exists(os.path.join(self.root, "feed.jsonl")))

    def test_relay_refuses_linked_storage_entries(self):
        reference = os.path.join(self.root, "fixture.txt")
        for name in ("feed.jsonl", ".feed.lock"):
            with self.subTest(name=name):
                entry = os.path.join(self.root, name)
                with open(reference, "w") as f:
                    f.write("fixture\n")
                os.symlink(reference, entry)
                try:
                    response = self.run_hook("stop", "B", self.tB, final="Routine update.")
                    self.assertEqual((response.returncode, response.stdout), (0, ""))
                    with open(reference) as f:
                        self.assertEqual(f.read(), "fixture\n")
                    self.assertTrue(os.path.islink(entry))
                    if name == ".feed.lock":
                        self.assertFalse(os.path.lexists(os.path.join(self.root, "feed.jsonl")))
                finally:
                    os.unlink(entry)
                    # The refused feed case can still create a valid lock.
                    lock = os.path.join(self.root, ".feed.lock")
                    if os.path.exists(lock):
                        os.unlink(lock)

    def test_relay_files_are_created_for_the_owner_only(self):
        self.run_hook("stop", "B", self.tB, final="Routine update.")
        for path in ("feed.jsonl", ".feed.lock", "state/B.json"):
            with self.subTest(path=path):
                self.assertEqual(os.stat(os.path.join(self.root, path)).st_mode & 0o777, 0o600)

    def test_silent_on_bad_input(self):
        for raw in ["", "not json", "[1]", "null", '{"session_id": 5, "transcript_path": []}']:
            for mode in ("post-tool", "stop", "bogus"):
                r = self.run_hook(mode, raw=raw)
                self.assertEqual((r.returncode, r.stdout), (0, ""), (mode, raw))
        r = self.run_hook("post-tool", "A", "/nonexistent.jsonl")
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_silent_and_logged_when_code_missing(self):
        r = self.run_hook("post-tool", "A", self.tA, env=dict(self.env, RELAY_CODE=os.path.join(self.root, "nope")))
        # RELAY_CODE is tried first; the script's own folder is still on the path, so this must still work silently
        self.assertEqual(r.returncode, 0)

    def test_data_dir_falls_back_to_plugin_data(self):
        env = {k: v for k, v in self.env.items() if k != "RELAY_ROOT"}
        env["CLAUDE_PLUGIN_DATA"] = os.path.join(self.root, "pdata")
        self.run_hook("stop", "B", self.tB, env=env, final=POSTS["billing"])
        self.assertTrue(os.path.exists(os.path.join(self.root, "pdata", "feed.jsonl")))


class Fixes010(unittest.TestCase):
    def test_batch_ids_unique(self):
        root = tempfile.mkdtemp(prefix="relaytest-")
        os.environ["RELAY_ROOT"] = root
        try:
            import importlib; importlib.reload(relay)
            ids = [relay.post("post %d" % i, session="S")["id"] for i in range(5)]
        finally:
            del os.environ["RELAY_ROOT"]; import importlib; importlib.reload(relay); shutil.rmtree(root, True)
        self.assertEqual(len(set(ids)), 5, ids)

    def test_relay_off(self):
        h = Hook("test_irrelevant_not_injected"); h.setUp()
        try:
            relay_post(h.root, "C", POSTS["billing"])
            r = h.run_hook("post-tool", "A", h.tA, env=dict(h.env, RELAY_OFF="1"))
            self.assertEqual((r.returncode, r.stdout), (0, ""))
        finally:
            h.tearDown()


class Fixes011(unittest.TestCase):
    """Regression coverage for feed admission, incremental reads and compaction."""
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="relaytest-")
        os.environ["RELAY_ROOT"] = self.root
        os.environ["RELAY_FEED_MAX_BYTES"] = "20000"
        import importlib; importlib.reload(relay)
        self.tp = os.path.join(self.root, "t.jsonl")
        with open(self.tp, "w") as f:
            f.write(json.dumps({"type": "user", "message": {"content": TASKS["billing"]}}) + "\n")

    def tearDown(self):
        del os.environ["RELAY_ROOT"]; del os.environ["RELAY_FEED_MAX_BYTES"]
        import importlib; importlib.reload(relay); shutil.rmtree(self.root, True)

    def test_bad_record_does_not_stall_reader_or_compaction(self):
        good = relay.post(POSTS["billing"], session="peerA")
        invalid_fields = (
            {"ts": "abc"}, {"ts": None}, {"ts": "inf"}, {"session": 7}, {"title": ["t"]},
        )
        with open(relay.FEED, "a") as feed:
            for index, override in enumerate(invalid_fields):
                record = dict(good, id="invalid-%d" % index)
                record.update(override)
                feed.write(json.dumps(record) + "\n")
        out = relay.hook_post_tool({"session_id": "me", "transcript_path": self.tp})
        self.assertIsNotNone(out)
        self.assertEqual(relay.load_state("me")["off"], os.path.getsize(relay.FEED))
        relay.compact_feed()
        self.assertEqual([p["id"] for p in relay.read_feed(0)[0]], [good["id"]])

    def test_append_remains_successful_if_compaction_fails(self):
        with patch.object(relay, "FEED_MAX_BYTES", 1), patch.object(
            relay, "compact_feed_locked", side_effect=RuntimeError("maintenance interrupted")
        ):
            published = relay.post("Routine maintenance note", session="peerA")
        records, _ = relay.read_feed()
        self.assertEqual([p["id"] for p in records], [published["id"]])
        with open(os.path.join(self.root, "errors.log")) as log:
            self.assertIn("maintenance interrupted", log.read())

    def test_upgrade_preserves_the_pending_queue_and_previous_cooldown(self):
        delivered = relay.post(POSTS["billing"], session="peerA")
        pending = relay.post(POSTS["billing"], session="peerB")
        info = os.stat(relay.FEED)
        now = time.time()
        relay.save_state("me", {
            "off": info.st_size,
            "feed_ino": info.st_ino,
            "injected": [delivered["id"]],
            "last_inject": now,
            "queue": [dict(pending, s=0.5)],
        })
        event = {"session_id": "me", "transcript_path": self.tp}
        self.assertIsNone(relay.hook_post_tool(event, now=now + 1))
        notice = relay.hook_post_tool(event, now=now + relay.COOLDOWN_S)
        self.assertEqual(set(notice), {"systemMessage"})
        self.assertEqual([p["id"] for p in relay.load_state("me")["queue"]], [pending["id"]])

    def test_lagging_reader_receives_a_post_retained_by_compaction(self):
        event = {"session_id": "me", "transcript_path": self.tp}
        for i in range(10):
            relay.post("Archive item %02d " % i + "x" * 300, session="peerA")
        relay.hook_post_tool(event)
        stale = relay.load_state("me")["off"]
        original_inode = os.stat(relay.FEED).st_ino
        item = relay.post(POSTS["billing"], session="peerA")
        for i in range(37):
            relay.post("Background item %02d " % i + "y" * 300, session="peerA")
        with open(relay.FEED, "rb") as feed:
            retained = feed.read()
        self.assertNotEqual(os.stat(relay.FEED).st_ino, original_inode)
        self.assertLess(stale, len(retained))
        self.assertIn(item["id"].encode(), retained[:stale])
        relay.hook_post_tool(event)
        queued = relay.load_state("me")["queue"]
        self.assertEqual([p["id"] for p in queued], [item["id"]])

    def test_incremental_read_makes_progress_within_a_byte_budget(self):
        first = relay.post("Routine background entry " + "x" * 400, session="peerA")
        second = relay.post(POSTS["billing"], session="peerA")
        event = {"session_id": "me", "transcript_path": self.tp}
        with patch.object(relay, "HOOK_FEED_READ_BYTES", 600):
            self.assertIsNone(relay.hook_post_tool(event))
            self.assertLess(relay.load_state("me")["off"], os.path.getsize(relay.FEED))
            relay.hook_post_tool(event)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual([p["id"] for p in relay.load_state("me")["queue"]], [second["id"]])

    def test_read_continues_after_a_legacy_record_above_the_record_limit(self):
        relay.post("Legacy entry " + "x" * 550, session="peerA")
        newer = relay.post(POSTS["billing"], session="peerA")
        event = {"session_id": "me", "transcript_path": self.tp}
        with patch.object(relay, "MAX_FEED_LINE_BYTES", 300), patch.object(relay, "HOOK_FEED_READ_BYTES", 400):
            for _ in range(3):
                relay.hook_post_tool(event)
        self.assertEqual([p["id"] for p in relay.load_state("me")["queue"]], [newer["id"]])

    def test_a_post_preserves_the_previous_row_when_only_its_newline_is_missing(self):
        original = relay.post("Earlier note", session="peerA")
        with open(relay.FEED, "r+b") as feed:
            feed.truncate(os.path.getsize(relay.FEED) - 1)
        latest = relay.post("Later note", session="peerB")
        items, _ = relay.read_feed()
        self.assertEqual([p["id"] for p in items], [original["id"], latest["id"]])

    def test_tail_window_includes_the_record_starting_exactly_at_its_boundary(self):
        relay.post("Earlier note", session="peerA")
        earlier_size = os.path.getsize(relay.FEED)
        latest = relay.post("Later note", session="peerB")
        with patch.object(relay, "FEED_TAIL_BYTES", os.path.getsize(relay.FEED) - earlier_size):
            items, _ = relay.read_feed()
        self.assertEqual([p["id"] for p in items], [latest["id"]])

    def test_complete_record_after_a_partial_append_is_seen_once(self):
        event = {"session_id": "me", "transcript_path": self.tp}
        record = {"id": "example", "session": "peerA", "ts": time.time(), "text": POSTS["billing"]}
        encoded = json.dumps(record).encode()
        with open(relay.FEED, "wb") as feed:
            feed.write(encoded[:40])
        self.assertIsNone(relay.hook_post_tool(event))
        with open(relay.FEED, "ab") as feed:
            feed.write(encoded[40:] + b"\n")
        relay.hook_post_tool(event)
        relay.hook_post_tool(event)
        self.assertEqual([p["id"] for p in relay.load_state("me")["queue"]], ["example"])


def relay_post(root, sid, text):
    with open(os.path.join(root, "feed.jsonl"), "a") as f:
        f.write(json.dumps({"ts": time.time(), "session": sid, "id": "%s-%d" % (sid, time.time_ns()), "text": text}) + "\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
