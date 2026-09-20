"""Behavior tests for the four tools, using the SAMPLE content in ./content."""
import server as s


def test_only_approved_content_is_loaded():
    assert set(s.DOCS) == {"POL-ATT-001", "GDE-CONF-001", "GDE-PERF-001", "TPL-ATT-001"}


def test_search_returns_cited_passages():
    r = s.search_policies("employee keeps arriving late", topic="attendance")
    assert r["found"]
    top = r["results"][0]
    for key in ("doc_id", "title", "version", "effective_date", "source_url", "passage"):
        assert key in top


def test_natural_phrasing_matches_via_synonyms():
    r = s.search_policies("two employees keep arguing on my shift", topic="conflict")
    assert r["found"] and r["results"][0]["doc_id"] == "GDE-CONF-001"


def test_uncovered_topics_return_no_source():
    for q in ("parental leave payout formula", "what is our dress code",
              "stock option vesting schedule"):
        r = s.search_policies(q)
        assert r["found"] is False
        assert "No approved source found" in r["message"]


def test_queries_with_personal_data_are_rejected():
    for q in ("John jo@example.com was late", "call him at 312-555-0147",
              "employee id 48213 missed a shift"):
        r = s.search_policies(q)
        assert r["found"] is False
        assert "personal data" in r["message"].lower()


def test_topic_filter_limits_results():
    r = s.search_policies("expectations conversation", topic="performance")
    assert r["found"]
    assert {x["doc_id"] for x in r["results"]} == {"GDE-PERF-001"}


def test_get_policy_full_and_by_section():
    full = s.get_policy("POL-ATT-001")
    assert full["found"] and "Purpose" in full["text"]
    part = s.get_policy("POL-ATT-001", "Patterns and follow-up")
    assert part["found"] and "30-day" in part["text"]
    assert s.get_policy("POL-ATT-001", "Nope")["found"] is False
    assert s.get_policy("DOES-NOT-EXIST")["found"] is False


def test_get_policy_does_not_return_templates():
    assert s.get_policy("TPL-ATT-001")["found"] is False


def test_escalation_rules():
    r = s.get_escalation_rule("Harassment")
    assert r["found"] and r["contact"] and r["timing"] and r["triggers"]
    assert s.get_escalation_rule("made-up")["found"] is False
    listing = s.get_escalation_rule("")
    assert "safety" in listing["available_scenario_types"]


def test_conversation_template():
    r = s.get_conversation_template("attendance")
    assert r["found"] and r["templates"][0]["doc_id"] == "TPL-ATT-001"
    assert s.get_conversation_template("dress code")["found"] is False
