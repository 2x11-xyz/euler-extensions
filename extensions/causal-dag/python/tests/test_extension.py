"""Milestone-3 command tests: the projector and the three handlers over a
FakeHost that implements the managed-process ``Host`` surface in memory.

No live euler host is involved. The FakeHost mirrors the pieces the commands
depend on: a bounded, cursor-paged provenance feed (kind-filtered, truncating,
watermark-honest); a checkpoint store; a state directory; artifact writes that
also append the corresponding ``extension.artifact`` self-event to the feed (so
idempotency and self-drain are exercised); and a request counter so the budget
claim can be asserted, not assumed.
"""

import hashlib
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import json  # noqa: E402
import os  # noqa: E402

import causal_dag.commands as commands  # noqa: E402
from causal_dag import check, loads, structural_projection  # noqa: E402
from causal_dag.commands import catch_up, export, update  # noqa: E402
from causal_dag.projection import group_turns, project_tick  # noqa: E402


class Ctx:
    def __init__(self, host, payload):
        self.host = host
        self.input = payload


class FakeHost:
    """In-memory host: a paged provenance feed plus checkpoint/slot/artifact state."""

    def __init__(self, events, state_dir):
        self.events = list(events)
        self._state_dir = state_dir
        self.checkpoints = {}
        self.slots = {}
        self.artifacts = []
        self.requests = 0
        self.session = events[0]["session"] if events else "session-test"

    def state_dir(self):
        self.requests += 1
        return self._state_dir

    def _index_of(self, event_id):
        for i, event in enumerate(self.events):
            if event["id"] == event_id:
                return i
        return -1

    def query_provenance(self, after_event_id=None, kinds=None, limit=128,
                         scan_limit=1024, **_):
        # Mirrors the real host exactly: the watermark advances over every
        # SCANNED event (matching or not), so a truncation cursor can be the id
        # of a filtered-out event that appears in no page.
        self.requests += 1
        wanted = set(kinds or [])
        if after_event_id is None:
            start = 0
        else:
            index = self._index_of(after_event_id)
            if index < 0:  # the real host raises CursorNotFound
                raise ValueError(f"cursor not found: {after_event_id}")
            start = index + 1
        returned, truncated = [], False
        watermark = after_event_id
        for event in self.events[start:]:
            matches = not wanted or event["kind"] in wanted
            if matches and len(returned) == limit:
                truncated = True
                break
            watermark = event["id"]
            if matches:
                returned.append(event)
        return {"events": returned, "truncated": truncated,
                "watermark_event_id": watermark,
                "next_after_event_id": watermark if truncated else None,
                "applied_limit": limit, "applied_scan_limit": scan_limit,
                "scanned_events": len(self.events) - start}

    def load_checkpoint(self, name):
        self.requests += 1
        return self.checkpoints.get(name)

    def store_checkpoint(self, name, checkpoint):
        self.requests += 1
        assert set(checkpoint) == {"schema_version", "after_event_id"}
        self.checkpoints[name] = dict(checkpoint)

    def write_artifact(self, display_name, media_type, data, source_event_ids=None,
                       metadata=None):
        self.requests += 1
        event_id = f"art-{len(self.artifacts):04d}"
        sha = hashlib.sha256(data).hexdigest()
        self.artifacts.append({"display_name": display_name, "media_type": media_type,
                               "data": data, "metadata": metadata or {}})
        # Mirror the host: an artifact write appends a self-event at the head.
        self.events.append({"id": event_id, "kind": "extension.artifact", "ts": "2026-07-27T00:00:00Z",
                            "session": self.session, "payload": {"extension_id": "causal-dag"}})
        return {"persisted_event_id": event_id, "relative_path": f"ext/causal-dag/artifacts/{sha}",
                "sha256": sha, "byte_len": len(data)}

    def update_context_slot(self, slot, content):
        self.requests += 1
        assert len(content.encode("utf-8")) <= 4096
        self.slots[slot] = content
        self.events.append({"id": f"slot-{len(self.events):04d}", "kind": "context.slot.updated",
                            "ts": "2026-07-27T00:00:00Z", "session": self.session,
                            "payload": {"extension_id": "causal-dag", "slot": slot}})


def _event(event_id, kind, session="session-test"):
    return {"id": event_id, "kind": kind, "ts": "2026-07-27T00:00:00Z",
            "session": session, "payload": {}}


def _turn_events(index):
    """One model-driven turn: a boundary model.call plus an owned assistant.message."""
    return [_event(f"e-call-{index:03d}", "model.call"),
            _event(f"e-msg-{index:03d}", "assistant.message")]


def _base_events():
    events = [_event("e-start-000", "session.start"), _event("e-user-000", "user.message")]
    events += _turn_events(0) + _turn_events(1)
    events.append(_event("e-perm-000", "permission.decision"))
    return events


def _state_dir():
    return tempfile.mkdtemp(prefix="causal-dag-test-")


class TurnGroupingTest(unittest.TestCase):
    def test_boundaries_and_exclusions(self):
        page = [
            _event("u0", "user.message"),
            _event("c0", "model.call"),
            _event("a0", "assistant.message"),
            _event("perm", "permission.decision"),
            _event("t0", "tool.call"),
            _event("selfart", "extension.artifact"),
            _event("c1", "model.call"),
            _event("a1", "assistant.message"),
        ]
        turns = group_turns(page)
        # user.message opens its own turn; the model.call turn owns the
        # assistant/tool events; permission and self-events are excluded.
        self.assertEqual(turns, [
            [("u0", "user.message")],
            [("a0", "assistant.message"), ("t0", "tool.call")],
            [("a1", "assistant.message")],
        ])

    def test_empty_boundary_turn_dropped(self):
        # A model.call owning nothing but a permission event stays unowned (R13).
        page = [_event("c0", "model.call"), _event("perm", "permission.decision")]
        self.assertEqual(group_turns(page), [])


class UpdateTickTest(unittest.TestCase):
    def test_projects_check_clean_degraded_artifact(self):
        host = FakeHost(_base_events(), _state_dir())
        result = update(Ctx(host, {}))

        self.assertTrue(result["projected"])
        self.assertEqual(result["operation"], "snapshot")
        self.assertEqual(len(host.artifacts), 1)

        artifact = loads(host.artifacts[0]["data"].decode("utf-8"))
        self.assertEqual(check(artifact), [])
        self.assertTrue(artifact.projection.degraded)
        self.assertEqual(artifact.nodes[0].kind, "question")  # opens with user.message
        self.assertEqual(host.artifacts[0]["media_type"],
                         "application/vnd.euler.causal-dag.v5+json")
        # Slot written and bounded.
        self.assertIn("graph", host.slots)
        self.assertLessEqual(len(host.slots["graph"].encode("utf-8")), 4096)
        # Checkpoint advanced.
        self.assertIn("main", host.checkpoints)
        self.assertEqual(host.checkpoints["main"]["schema_version"], 1)

    def test_second_tick_without_new_events_short_circuits(self):
        host = FakeHost(_base_events(), _state_dir())
        update(Ctx(host, {}))
        first_cursor = host.checkpoints["main"]["after_event_id"]

        second = update(Ctx(host, {}))
        self.assertFalse(second["projected"])
        self.assertEqual(len(host.artifacts), 1)  # no new artifact
        # The checkpoint advanced past our own self-events (drained).
        self.assertNotEqual(host.checkpoints["main"]["after_event_id"], first_cursor)

    def test_incremental_tick_chains_predecessor(self):
        host = FakeHost(_base_events(), _state_dir())
        first = update(Ctx(host, {}))

        # New turns arrive after the first tick's cursor.
        host.events += _turn_events(2) + _turn_events(3)
        second = update(Ctx(host, {}))

        self.assertTrue(second["projected"])
        self.assertEqual(second["operation"], "incremental")
        self.assertGreater(second["node_count"], first["node_count"])

        artifact = loads(host.artifacts[-1]["data"].decode("utf-8"))
        self.assertEqual(check(artifact), [])
        self.assertEqual(artifact.construction.operation, "incremental")
        self.assertEqual(artifact.construction.predecessor_artifact_event_id,
                         first["artifact_event_id"])
        self.assertIsNotNone(artifact.construction.predecessor_watermark_event_id)


class CatchUpTest(unittest.TestCase):
    def _many_turn_host(self, turns=20):
        events = [_event("e-start-000", "session.start")]
        for i in range(turns):
            events += _turn_events(i)
        return FakeHost(events, _state_dir())

    def test_budget_respected_and_resumable(self):
        host = self._many_turn_host(turns=20)
        calls = 0
        caught_up = False
        while not caught_up and calls < 20:
            result = catch_up(Ctx(host, {"limit": 4}))
            calls += 1
            self.assertLess(result["requests_used"], 64)  # strict budget honored
            self.assertLess(host.requests, 64 * calls + 1)
            caught_up = result["caught_up"]
            if not caught_up:
                self.assertTrue(result["continue"])
        self.assertTrue(caught_up)

        # The final artifact is check-clean and covers every turn.
        artifact = loads(host.artifacts[-1]["data"].decode("utf-8"))
        self.assertEqual(check(artifact), [])
        self.assertEqual(len(artifact.nodes), 20)

    def test_idempotent_when_already_caught_up(self):
        host = self._many_turn_host(turns=6)
        while not catch_up(Ctx(host, {"limit": 4}))["caught_up"]:
            pass
        artifact_count = len(host.artifacts)
        again = catch_up(Ctx(host, {"limit": 4}))
        self.assertTrue(again["caught_up"])
        self.assertEqual(again["ticks_projected"], 0)
        self.assertEqual(len(host.artifacts), artifact_count)  # no new artifact


class ExportTest(unittest.TestCase):
    def test_renders_every_format(self):
        host = FakeHost(_base_events(), _state_dir())
        update(Ctx(host, {}))
        baseline = len(host.artifacts)

        for i, fmt in enumerate(("json", "dot", "markdown", "summary", "html")):
            result = export(Ctx(host, {"format": fmt}))
            self.assertEqual(result["format"], fmt)
            self.assertEqual(len(host.artifacts), baseline + i + 1)
            self.assertGreater(result["byte_len"], 0)

        # The json export re-parses and passes the checker.
        json_bytes = next(a["data"] for a in host.artifacts if a["media_type"].startswith(
            "application/vnd.euler.causal-dag"))
        self.assertEqual(check(loads(json_bytes.decode("utf-8"))), [])

    def test_html_view_is_honored(self):
        host = FakeHost(_base_events(), _state_dir())
        update(Ctx(host, {}))
        result = export(Ctx(host, {"format": "html", "view": "indented"}))
        self.assertEqual(result["view"], "indented")

    def test_export_without_active_state_errors(self):
        host = FakeHost(_base_events(), _state_dir())
        with self.assertRaises(ValueError) as caught:
            export(Ctx(host, {"format": "json"}))
        self.assertIn("run update first", str(caught.exception))

    def test_rejects_unknown_format(self):
        host = FakeHost(_base_events(), _state_dir())
        update(Ctx(host, {}))
        with self.assertRaises(ValueError):
            export(Ctx(host, {"format": "yaml"}))


def _rich_turn(index):
    """A model turn owning two events: an assistant.message plus a tool.result,
    so a page boundary can fall *inside* the turn (exercising pending carry)."""
    return [_event(f"e-call-{index:03d}", "model.call"),
            _event(f"e-msg-{index:03d}", "assistant.message"),
            _event(f"e-tool-{index:03d}", "tool.result")]


def _rich_log(turns=12):
    events = [_event("e-start-000", "session.start"), _event("e-user-000", "user.message")]
    for i in range(turns):
        events += _rich_turn(i)
    return events


def _drain(host, limit):
    while not catch_up(Ctx(host, {"limit": limit}))["caught_up"]:
        pass
    return loads(host.artifacts[-1]["data"].decode("utf-8"))


class PaginationInvarianceTest(unittest.TestCase):
    """FIX 2 (R1): the projected node structure is identical for any page limit —
    the open turn is carried across ticks, so where a page happens to truncate
    never changes the graph."""

    def test_same_structure_at_every_limit(self):
        base = structural_projection(_drain(FakeHost(_rich_log(), _state_dir()), 64))
        # limit 1 pages one event at a time (boundary-only pages must still cut
        # turns); 4/5/7 slice turns mid-span; 64 and 128 take the whole log in
        # one page ("unbounded" for this synthetic).
        for limit in (1, 2, 3, 4, 5, 7, 64, 128):
            got = structural_projection(_drain(FakeHost(_rich_log(), _state_dir()), limit))
            self.assertEqual(got, base, f"limit {limit} diverged")
        # 12 turns + the opening user question = 13 nodes, whatever the paging.
        self.assertEqual(len(base["nodes"]), 13)


class CrashWindowTest(unittest.TestCase):
    """The crash window between the state write and the checkpoint advance
    self-heals: the state's own cursor is authoritative, so the next tick
    resumes at the exact segmentation point instead of replaying the page."""

    def test_replayed_page_self_heals(self):
        host = FakeHost(_base_events(), _state_dir())
        update(Ctx(host, {}))
        cursor1 = host.checkpoints["main"]["after_event_id"]

        host.events += _turn_events(2) + _turn_events(3)
        second = update(Ctx(host, {}))
        node_count = second["node_count"]
        artifact_count = len(host.artifacts)

        # Simulate the crash: the artifact (active.json) advanced with tick 2,
        # but the checkpoint never got past tick 1.
        host.checkpoints["main"] = {"schema_version": 1, "after_event_id": cursor1}

        healed = update(Ctx(host, {}))  # must not raise a checker finding
        self.assertFalse(healed["projected"])
        self.assertEqual(healed["node_count"], node_count)          # unchanged
        self.assertEqual(len(host.artifacts), artifact_count)       # no new write
        # The cursor moved forward again (past the replayed page): no wedge.
        self.assertNotEqual(host.checkpoints["main"]["after_event_id"], cursor1)

    def test_partial_overlap_replay_at_larger_limit(self):
        # The retry after a crash need not use the crashed run's limit: a wider
        # page mixes replayed events with genuinely new ones. Known events are
        # stripped of ownership (keeping boundary roles), so the retry projects
        # only the new turns and converges to the clean-run structure.
        host = FakeHost(_rich_log(8), _state_dir())
        update(Ctx(host, {"limit": 4}))
        cursor1 = host.checkpoints["main"]["after_event_id"]
        update(Ctx(host, {"limit": 4}))
        host.checkpoints["main"] = {"schema_version": 1, "after_event_id": cursor1}

        _drain(host, 64)  # one page = tick-2 replays + everything new
        got = structural_projection(loads(host.artifacts[-1]["data"].decode("utf-8")))
        clean = structural_projection(_drain(FakeHost(_rich_log(8), _state_dir()), 64))
        self.assertEqual(got, clean)

    def test_crash_replay_with_mid_turn_pending(self):
        # A stale boundary in the replayed prefix must not cut the still-open
        # pending turn: the synced-cursor window drops the whole replayed
        # prefix by stream position before segmentation.
        events = [_event("e0", "user.message"), _event("e1", "model.call"),
                  _event("e2", "model.result"), _event("e3", "tool.call"),
                  _event("e4", "tool.result"), _event("e5", "model.call"),
                  _event("e6", "model.result")]
        host = FakeHost(list(events), _state_dir())
        update(Ctx(host, {"limit": 4}))
        with open(os.path.join(host._state_dir, "active.json")) as fh:
            state = json.load(fh)
        self.assertEqual([p[0] for p in state["pending"]], ["e2", "e3"])  # mid-turn
        host.checkpoints.pop("main")               # crash: checkpoint advance lost
        _drain(host, 128)
        got = structural_projection(loads(host.artifacts[-1]["data"].decode("utf-8")))
        clean = structural_projection(_drain(FakeHost(list(events), _state_dir()), 128))
        self.assertEqual(got, clean)

    def test_own_error_events_are_not_owned(self):
        # The host appends an `error` event when an extension command fails;
        # owning our own failures would pollute the graph on every retry.
        # A genuinely epistemic session error stays owned.
        events = _base_events()
        events.append({"id": "e-err-self", "kind": "error", "ts": "2026-07-27T00:00:00Z",
                       "session": "session-test",
                       "payload": {"source": "extension", "extension_id": "causal-dag",
                                   "command": "update"}})
        events.append({"id": "e-err-real", "kind": "error", "ts": "2026-07-27T00:00:00Z",
                       "session": "session-test", "payload": {"source": "tool"}})
        host = FakeHost(events, _state_dir())
        update(Ctx(host, {}))
        art = loads(host.artifacts[-1]["data"].decode("utf-8"))
        owned = {ev for n in art.nodes for t in n.turns for ev in t.event_ids}
        self.assertNotIn("e-err-self", owned)
        self.assertIn("e-err-real", owned)

    def test_tampered_pending_self_heals(self):
        # A pending entry the artifact already owns (state surgery) is dropped
        # at load instead of double-owning the event and wedging every
        # subsequent tick on the invariant checker.
        host = FakeHost(_base_events(), _state_dir())
        update(Ctx(host, {}))
        path = os.path.join(host._state_dir, "active.json")
        with open(path) as fh:
            state = json.load(fh)
        owned0 = state["artifact"]["forest"]["nodes"][0]["turns"][0]["event_ids"][0]
        state["pending"] = [[owned0, "assistant.message"]] + state["pending"]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        host.events += _turn_events(2)
        update(Ctx(host, {}))  # must not raise
        art = loads(host.artifacts[-1]["data"].decode("utf-8"))
        self.assertEqual(check(art), [])
        ids = [ev for n in art.nodes for t in n.turns for ev in t.event_ids]
        self.assertEqual(len(ids), len(set(ids)))


class ByteBudgetTest(unittest.TestCase):
    """FIX 4: the degraded spine has an honest size ceiling — a diagnosed halt,
    never a host ProtocolError."""

    def setUp(self):
        self._saved = (commands._MESSAGE_BYTE_LIMIT,
                       commands._OUTPUT_BYTE_BUDGET, commands._GROWTH_ESTIMATE)

    def tearDown(self):
        (commands._MESSAGE_BYTE_LIMIT, commands._OUTPUT_BYTE_BUDGET,
         commands._GROWTH_ESTIMATE) = self._saved

    def test_single_message_guard_halts_without_writing(self):
        commands._MESSAGE_BYTE_LIMIT = 500  # any real artifact exceeds this
        host = FakeHost(_base_events(), _state_dir())
        result = update(Ctx(host, {}))
        self.assertFalse(result["projected"])
        self.assertEqual(result["halted"], "artifact-size-limit")
        self.assertGreater(result["artifact_bytes"], 500)
        self.assertEqual(len(host.artifacts), 0)        # nothing written
        self.assertNotIn("main", host.checkpoints)      # checkpoint not advanced
        # The halt reports the DURABLE count (nothing exists yet), not the
        # refused artifact's; the refused size lives under its own key.
        self.assertEqual(result["node_count"], 0)
        self.assertGreater(result["would_be_node_count"], 0)

    def test_halted_catch_up_reports_durable_count_and_stops(self):
        host = FakeHost(_base_events(), _state_dir())
        first = update(Ctx(host, {}))
        # Cap just above the durable artifact: the next (larger) write halts.
        written = len(host.artifacts[-1]["data"])
        commands._MESSAGE_BYTE_LIMIT = commands._framed_bytes(written) + 10
        host.events += _turn_events(2) + _turn_events(3)
        result = catch_up(Ctx(host, {}))
        self.assertEqual(result["halted"], "artifact-size-limit")
        # A halt is terminal for automation: re-invoking would replay the same
        # oversized projection, so continue must be false.
        self.assertFalse(result["continue"])
        self.assertEqual(result["node_count"], first["node_count"])  # durable

    def test_catch_up_stops_on_byte_budget(self):
        commands._OUTPUT_BYTE_BUDGET = 40_000
        commands._GROWTH_ESTIMATE = 1_000
        host = FakeHost(_rich_log(turns=40), _state_dir())
        result = catch_up(Ctx(host, {"limit": 4}))
        self.assertEqual(result["stopped"], "byte-budget")
        self.assertFalse(result["caught_up"])
        self.assertTrue(result["continue"])
        self.assertLess(result["requests_used"], 64)
        # Progress was still made and the partial artifact is check-clean.
        self.assertGreater(len(host.artifacts), 0)
        self.assertEqual(check(loads(host.artifacts[-1]["data"].decode("utf-8"))), [])


class StateSelfHealTest(unittest.TestCase):
    """FIX 5: a lost or corrupt state pointer is treated as absent; with a live
    cursor the projector rebuilds the whole graph as an honest fresh snapshot."""

    def _state_path(self, host):
        return os.path.join(host._state_dir, "active.json")

    def test_corrupt_state_rebuilds_full_graph(self):
        host = FakeHost(_base_events(), _state_dir())
        first = update(Ctx(host, {}))
        with open(self._state_path(host), "w", encoding="utf-8") as fh:
            fh.write("{ this is not valid json ]")

        result = update(Ctx(host, {}))
        self.assertEqual(result["recovered"], "state-rebuilt")
        self.assertEqual(result["state_was"], "corrupt")
        self.assertEqual(result["operation"], "snapshot")  # fresh, not incremental
        self.assertEqual(result["node_count"], first["node_count"])
        art = loads(host.artifacts[-1]["data"].decode("utf-8"))
        self.assertEqual(check(art), [])

    def test_deleted_state_mid_stream_rebuilds(self):
        host = FakeHost(_base_events(), _state_dir())
        first = update(Ctx(host, {}))
        os.remove(self._state_path(host))

        result = update(Ctx(host, {}))
        self.assertEqual(result["recovered"], "state-rebuilt")
        self.assertEqual(result["state_was"], "missing")
        self.assertEqual(result["operation"], "snapshot")
        self.assertEqual(result["node_count"], first["node_count"])

    def test_export_with_corrupt_state_errors_clearly(self):
        host = FakeHost(_base_events(), _state_dir())
        update(Ctx(host, {}))
        with open(self._state_path(host), "w", encoding="utf-8") as fh:
            fh.write("not json")
        with self.assertRaises(ValueError) as caught:
            export(Ctx(host, {"format": "json"}))
        self.assertIn("run update first", str(caught.exception))


class ProjectorUnitTest(unittest.TestCase):
    def test_empty_batch_yields_no_artifact(self):
        # A page whose only events are a boundary + a clerical event groups to
        # zero closed turns; project_tick mints nothing.
        turns = group_turns([_event("c0", "model.call"),
                             _event("p", "permission.decision")])
        self.assertEqual(turns, [])
        self.assertIsNone(project_tick(
            None, turns,
            session_id="s", watermark="w", generated_at="2026-07-27T00:00:00Z"))


if __name__ == "__main__":
    unittest.main()
