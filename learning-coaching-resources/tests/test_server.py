"""Behavior tests for the three tools, using the SAMPLE content in ./content."""
import json

import server as s

GUIDE_IDS = {
    "CG-ATT-001", "CG-SAF-001", "CG-CONF-001", "CG-CLI-001", "CG-PERF-001", "CG-COMM-001",
    "TRN-DIFF-001", "TRN-LIST-001", "MGR-PREP-001", "MGR-QREF-001",
}
TEMPLATE_IDS = {"DTP-CONV-001", "DTP-FOLLOW-001"}


def test_only_approved_content_is_loaded():
    assert set(s.DOCS) == GUIDE_IDS | TEMPLATE_IDS


def test_unapproved_restricted_and_invalid_files_are_skipped(tmp_path, monkeypatch):
    folder = tmp_path / "guides"
    folder.mkdir()
    base = ("id: X-{n}\ntitle: T\nversion: '1.0'\neffective_date: '2026-01-01'\n"
            "category: {cat}\napproved_for_ai: {approved}\nsensitivity: {sens}\n")
    cases = {
        "unapproved.md": base.format(n=1, cat="training", approved="false", sens="general"),
        "restricted.md": base.format(n=2, cat="training", approved="true", sens="restricted"),
        "badcategory.md": base.format(n=3, cat="policy", approved="true", sens="general"),
    }
    for name, meta in cases.items():
        (folder / name).write_text(f"---\n{meta}---\n\n## Body\ntext", encoding="utf-8")
    monkeypatch.setattr(s, "CONTENT_DIR", tmp_path)
    assert s.load_documents() == {}


def test_search_returns_cited_passages():
    r = s.search_guides("employee keeps arriving late", topic="attendance")
    assert r["found"]
    top = r["results"][0]
    for key in ("doc_id", "title", "version", "effective_date", "source_url",
                "passage", "category"):
        assert key in top


def test_natural_phrasing_matches_via_synonyms():
    r = s.search_guides("two employees keep arguing on my shift", topic="conflict")
    assert r["found"] and r["results"][0]["doc_id"] == "CG-CONF-001"


def test_uncovered_topics_return_no_source():
    for q in ("parental leave payout formula", "what is our dress code",
              "stock option vesting schedule"):
        r = s.search_guides(q)
        assert r["found"] is False
        assert "No approved source found" in r["message"]


def test_queries_with_personal_data_are_rejected():
    for q in ("John jo@example.com was late", "call him at 312-555-0147",
              "employee id 48213 missed a shift"):
        r = s.search_guides(q)
        assert r["found"] is False
        assert "personal data" in r["message"].lower()


def test_topic_filter_limits_results():
    r = s.search_guides("expectations conversation", topic="performance")
    assert r["found"]
    allowed = {d.id for d in s.DOCS.values() if "performance" in d.topics}
    assert {x["doc_id"] for x in r["results"]} <= allowed
    assert "CG-PERF-001" in {x["doc_id"] for x in r["results"]}


def test_category_filter_and_unknown_category():
    r = s.search_guides("steps for a difficult conversation", category="training")
    assert r["found"]
    assert {x["category"] for x in r["results"]} == {"training"}
    bad = s.search_guides("attendance", category="policy")
    assert bad["found"] is False and "Unknown category" in bad["message"]


def test_get_guide_full_and_by_section():
    full = s.get_guide("CG-ATT-001")
    assert full["found"] and "Purpose" in full["text"]
    part = s.get_guide("CG-ATT-001", "Key questions")
    assert part["found"] and "getting in the way" in part["text"]
    assert s.get_guide("CG-ATT-001", "Nope")["found"] is False
    assert s.get_guide("DOES-NOT-EXIST")["found"] is False


def test_guides_and_templates_do_not_cross_over():
    assert s.get_guide("DTP-CONV-001")["found"] is False
    r = s.get_documentation_template("attendance")
    assert r["found"]
    assert {t["doc_id"] for t in r["templates"]} == TEMPLATE_IDS


def test_documentation_template_edge_cases():
    assert s.get_documentation_template("made-up")["found"] is False
    assert s.get_documentation_template("")["found"] is False
    assert s.get_documentation_template("late for jo@example.com")["found"] is False


def test_guides_defer_escalation_to_the_policy_server():
    for doc in s.DOCS.values():
        if doc.category == "conversation-guide":
            assert "escalation rules" in doc.body.lower()


def test_review_overdue_flag():
    doc = s.DOCS["CG-ATT-001"]
    past = s.Document(**{**doc.__dict__, "review_date": "2000-01-01"})
    future = s.Document(**{**doc.__dict__, "review_date": "2999-01-01"})
    blank = s.Document(**{**doc.__dict__, "review_date": ""})
    assert past.review_overdue and not future.review_overdue and blank.review_overdue


def test_audit_log_can_redact_query_text(monkeypatch):
    monkeypatch.setattr(s, "LOG_QUERY_TEXT", False)
    s.search_guides("employee keeps arriving late", topic="attendance")
    last = json.loads(s.AUDIT_LOG.read_text(encoding="utf-8").splitlines()[-1])
    assert last["tool"] == "search_guides"
    assert set(last["args"].values()) == {"[redacted]"}
    assert "CG-ATT-001" in last["returned"]
