"""How `brain context` matches a question to projects, timeline events and
notes, and the optional type/tag filters.

Regression for the 0.24.1 behaviour where every query word was matched as a
substring: "a" is inside "Ashgrove", "for" inside "platform", so a generic
question listed nearly every registered project and the first events of the
timeline, while "status TRON?" listed none ("tron?" is no substring of
"project-tron"). All names below are fictional.
"""
import re
import unittest

from brain import context, indexer, listing, search
from brain.mcp_bridge import _t_get_context
from brain.mcp_server import tool_get_context
from tests.helpers import TempVault

REGISTRY = """projects:
  - id: project-ashgrove-saga
    name: "Ashgrove: From Dust to Crown"
    path: /tmp/x
    status: archived
  - id: project-family-hub
    name: Family Hub
    path: /tmp/x
    status: active
    aliases: [fhub, family-hub]
  - id: project-meshdesk
    name: MeshDesk — Family Device Control
    path: /tmp/x
    status: active
  - id: project-orbit
    name: ORBIT
    path: /tmp/x
    status: active
    aliases: [orbit]
  - id: project-iron-and-salt
    name: Iron & Salt
    path: /tmp/x
    status: active
  - id: project-relay
    name: Relay
    path: /tmp/x
    status: active
    aliases: [conduitd]
  - id: project-uk-land-data
    name: UK Land Data
    path: /tmp/x
    status: on-hold
  - id: project-first-aid-site
    name: First Aid Demo Site
    path: /tmp/x
    status: on-hold
    aliases: [first-aid-demo]
  - id: project-example
    name: Example Widget Project
    path: /tmp/x
    status: active
  - id: project-zazolc
    name: Zażółć Gęślą
    path: /tmp/x
    status: planned
  - id: project-kestrel-brain
    name: Kestrel Brain
    path: /tmp/x
    status: active
  - id: project-kestrel-theme
    name: Kestrel Theme
    path: /tmp/x
    status: active
"""

EVENTS = [
    ("event-2026-01-05-orbit-stage-2", "ORBIT Stage 2 shipped"),
    ("event-2026-01-04-orbit-outage", "ORBIT outage: 90 minutes"),
    ("event-2026-01-03-kestrel-brain-backup", "Kestrel Brain backup verified with a restore"),
    ("event-2026-01-02-kestrel-brain-release", "Kestrel Brain release 1.2 deployed"),
    ("event-2026-01-01-form-status", "Contact form live: status page added"),
    ("event-2025-12-30-website-audit", "Website audit: sitemap restored"),
    ("event-2025-12-29-iron-and-salt", "Iron & Salt: combat prototype accepted"),
    ("event-2025-12-28-platform-plan", "Platform plan for a community"),
    ("event-2025-12-27-widget-shipped", "Widget project shipped"),
]

AUDIT_QUERIES = [
    "Design a professional website for a UK first-aid training company",
    "Audit a property investor's website for UX, SEO and conversion",
    "Design an accessible dashboard for a desktop application",
    "Create a business positioning strategy for a new service",
    "Build a secure web application with authentication",
    "Perform an independent pre-release quality audit",
]


class Base(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.config.registry_path.write_text(REGISTRY)
        # a registry entry is visible only with its project record (registry.py)
        for pid in re.findall(r"^  - id: (\S+)$", REGISTRY, re.M):
            if pid != "project-orbit":
                self.vault.write_note("30_PROJECTS/ACTIVE", f"{pid}.md", id=pid, type="project",
                                      title=pid, body="Project record.")
        for eid, title in EVENTS:
            self.vault.write_note("50_TIMELINE", f"{eid}.md", id=eid, type="event", title=title,
                                  body=f"{title}.", created=eid[6:16])
        self.vault.write_note("30_PROJECTS/ACTIVE", "project-orbit.md", id="project-orbit", type="project",
                              title="ORBIT", body="ORBIT is the event engine. A status report lives here.")
        self.vault.write_note("60_KNOWLEDGE", "knowledge-a.md", id="knowledge-a", type="knowledge",
                              title="A note about a thing", body="a a a an the it is what")
        indexer.rebuild(self.config)

    def tearDown(self):
        self.vault.cleanup()

    def ctx(self, q, **kw):
        return context.get_context(self.config, q, **kw)

    def project_ids(self, q, **kw):
        return [p["id"] for p in self.ctx(q, **kw).projects]

    def event_ids(self, q, **kw):
        return [t["id"] for t in self.ctx(q, **kw).timeline]


class TestNoSubjectReturnsNothing(Base):
    def test_function_words_and_single_letters(self):
        for q in ("a", "the", "what is it?", "Jak to jest?", "a i o u w z", "?", ""):
            r = self.ctx(q)
            self.assertEqual((r.notes, r.projects, r.timeline), ([], [], []), q)

    def test_shape_is_unchanged_when_empty(self):
        self.assertEqual(set(self.ctx("a").to_dict()), {"query", "notes", "projects", "timeline", "restricted_omitted"})


class TestProjectMatching(Base):
    def test_audit_questions_no_longer_list_every_project(self):
        for q in AUDIT_QUERIES:
            ids = self.project_ids(q)
            self.assertNotIn("project-ashgrove-saga", ids, q)  # "a" inside "ashgrove"
            self.assertNotIn("project-family-hub", ids, q)
            self.assertLessEqual(len(ids), 2, (q, ids))

    def test_audit_question_finds_the_project_it_is_about(self):
        self.assertEqual(self.project_ids(AUDIT_QUERIES[0])[:1], ["project-first-aid-site"])

    def test_two_letter_word_alone_does_not_pick_a_project(self):
        self.assertNotIn("project-uk-land-data", self.project_ids("a UK company"))
        self.assertIn("project-uk-land-data", self.project_ids("UK Land Data status"))

    def test_punctuation_and_case(self):
        for q in ("What is the status of ORBIT?", "Jaki jest status ORBIT?", "orbit.", "ORBIT-a"):
            self.assertEqual(self.project_ids(q), ["project-orbit"], q)

    def test_alias_and_slug(self):
        self.assertEqual(self.project_ids("fhub status"), ["project-family-hub"])
        self.assertEqual(self.project_ids("conduitd"), ["project-relay"])
        self.assertEqual(self.project_ids("first-aid-demo"), ["project-first-aid-site"])

    def test_named_project_beats_projects_sharing_a_word(self):
        self.assertEqual(self.project_ids("Where did Family Hub finish?"), ["project-family-hub"])
        self.assertEqual(self.project_ids("kestrel brain backup"), ["project-kestrel-brain"])

    def test_shared_word_still_matches_when_nothing_is_named(self):
        # the pre-existing contract (tests.test_context): a word of the name is enough
        self.assertIn("project-example", self.project_ids("widget"))
        self.assertEqual(set(self.project_ids("family device")), {"project-meshdesk", "project-family-hub"})
        self.assertEqual(self.project_ids("family device")[0], "project-meshdesk")  # more shared words first

    def test_ampersand_and_polish_inflection_and_diacritics(self):
        self.assertEqual(self.project_ids("Co dalej z Iron & Salt?"), ["project-iron-and-salt"])
        self.assertEqual(self.project_ids("iron and salt"), ["project-iron-and-salt"])
        self.assertEqual(self.project_ids("Co z Relayem?"), ["project-relay"])
        self.assertEqual(self.project_ids("zazolc gesla"), ["project-zazolc"])

    def test_status_words_alone_pick_nothing(self):
        for q in ("status", "what is next?", "Co dalej?", "latest update", "project"):
            self.assertEqual(self.project_ids(q), [], q)

    def test_include_projects_false_still_empty(self):
        self.assertEqual(self.project_ids("ORBIT", include_projects=False), [])


class TestTimelineMatching(Base):
    def test_named_project_events_only(self):
        self.assertEqual(self.event_ids("What is the status of ORBIT?"),
                         ["event-2026-01-05-orbit-stage-2", "event-2026-01-04-orbit-outage"])

    def test_named_project_narrowed_by_the_other_words(self):
        self.assertEqual(self.event_ids("kestrel brain backup"), ["event-2026-01-03-kestrel-brain-backup"])
        # without a narrowing word: every event of that project, newest first
        self.assertEqual(self.event_ids("Kestrel Brain"),
                         ["event-2026-01-03-kestrel-brain-backup", "event-2026-01-02-kestrel-brain-release"])

    def test_status_word_does_not_pull_unrelated_events(self):
        self.assertNotIn("event-2026-01-01-form-status", self.event_ids("status ORBIT"))
        self.assertEqual(self.event_ids("status"), [])

    def test_unnamed_question_needs_two_shared_words(self):
        self.assertEqual(self.event_ids("Perform an independent pre-release quality audit"), [])
        self.assertEqual(self.event_ids("website audit"), ["event-2025-12-30-website-audit"])
        self.assertEqual(self.event_ids("widget"), ["event-2025-12-27-widget-shipped"])  # one-word question

    def test_audit_questions_do_not_list_the_first_events(self):
        for q in AUDIT_QUERIES:
            self.assertNotIn("event-2026-01-05-orbit-stage-2", self.event_ids(q), q)

    def test_limit_and_disable(self):
        self.assertEqual(len(self.event_ids("ORBIT", limit=1)), 1)
        self.assertEqual(self.event_ids("ORBIT", include_timeline=False), [])


class TestSearchQuery(unittest.TestCase):
    def test_english_questions_reach_search_unchanged_in_effect(self):
        # every word `brain search` would have kept is still there, in order
        q = "Design a professional website for a UK first-aid training company"
        self.assertEqual(search._clean_terms(context.search_query(q)), search._clean_terms(q))

    def test_polish_function_words_and_single_letters_are_dropped(self):
        self.assertEqual(context.search_query("Jaki jest status TRON?"), "status TRON")
        self.assertEqual(context.search_query("Co dalej ze Smoke & Steel?"), "dalej Smoke Steel")
        self.assertEqual(context.search_query("a i o"), "")

    def test_hyphenated_and_dotted_words_are_kept_whole(self):
        self.assertEqual(context.search_query("pre-release of marcin.example"), "pre-release marcin.example")


class TestTypeAndTagFilters(Base):
    def setUp(self):
        super().setUp()
        self.vault.write_note("60_KNOWLEDGE", "knowledge-std.md", id="knowledge-std", type="knowledge",
                              title="Website baseline standard", body="ORBIT website standard.",
                              tags=["kb:standard", "domain:web"])
        self.vault.write_note("60_KNOWLEDGE", "knowledge-pct.md", id="knowledge-pct", type="knowledge",
                              title="Percent note", body="ORBIT note.", tags=["kbXstandard"])
        for i in range(40):  # strong, non-matching hits that fill the over-fetch window
            self.vault.write_note("40_DECISIONS", f"decision-{i}.md", id=f"decision-{i}", type="decision",
                                  title=f"ORBIT ORBIT decision {i}", body="ORBIT ORBIT ORBIT")
        indexer.rebuild(self.config)

    def test_type_filter_on_notes_and_sections(self):
        r = self.ctx("ORBIT", type="knowledge", limit=2)
        self.assertEqual({n.type for n in r.notes}, {"knowledge"})
        self.assertIn("knowledge-std", [n.id for n in r.notes])  # found beyond the over-fetch window
        self.assertEqual((r.projects, r.timeline), ([], []))
        self.assertEqual(self.project_ids("ORBIT", type="project"), ["project-orbit"])
        self.assertEqual(self.ctx("ORBIT", type="project").timeline, [])
        self.assertNotEqual(self.event_ids("ORBIT", type="event"), [])
        self.assertEqual({n.type for n in self.ctx("ORBIT", type="event").notes}, {"event"})

    def test_tag_filter_is_exact_like_brain_list(self):
        r = self.ctx("ORBIT", tag="kb:standard")
        self.assertEqual([n.id for n in r.notes], ["knowledge-std"])
        self.assertEqual((r.projects, r.timeline), ([], []))
        self.assertEqual(self.ctx("ORBIT", tag="KB:STANDARD").notes, [])  # case-sensitive, as `brain list`
        self.assertEqual(self.ctx("ORBIT", tag="kb%").notes, [])  # no LIKE wildcards
        self.assertEqual(self.ctx("ORBIT", tag="kb_standard").notes, [])
        self.assertEqual([n.id for n in self.ctx("ORBIT", type="knowledge", tag="domain:web").notes], ["knowledge-std"])

    def test_unknown_type_is_refused(self):
        with self.assertRaises(listing.ListingError):
            self.ctx("ORBIT", type="no-such-type")

    def test_no_filter_is_the_old_note_search(self):
        plain = [n.id for n in self.ctx("ORBIT", limit=5).notes]
        self.assertEqual(plain, [r.id for r in search.search(self.config, "ORBIT", limit=10)][:5])

    def test_search_filter_matches_list_semantics(self):
        ids = {r.id for r in search.search(self.config, "ORBIT", limit=50, type="knowledge")}
        self.assertEqual(ids, {"knowledge-std", "knowledge-pct"})
        self.assertEqual([r.id for r in search.search(self.config, "ORBIT", limit=50, tag="domain:web")],
                         ["knowledge-std"])

    def test_mcp_tool_and_bridge_pass_filters_only_when_given(self):
        d = tool_get_context(self.config, "ORBIT", type="knowledge", tag="kb:standard")
        self.assertEqual([n["id"] for n in d["notes"]], ["knowledge-std"])
        self.assertEqual(_t_get_context({"query": "q"})[0], ["context", "q", "--json", "--limit", "10"])
        self.assertEqual(_t_get_context({"query": "q", "limit": 5, "type": "knowledge", "tag": "kb:standard"})[0],
                         ["context", "q", "--json", "--limit", "5", "--type", "knowledge", "--tag", "kb:standard"])
        self.assertEqual(_t_get_context({"query": "q", "type": "", "tag": None})[0],
                         ["context", "q", "--json", "--limit", "10"])


if __name__ == "__main__":
    unittest.main()
