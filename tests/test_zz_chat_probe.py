import json, uuid

FORM = {"summary": "Checkout total is wrong",
        "environment": "Chrome 140 / Windows 11",
        "steps_to_reproduce": "Add two items\nOpen the cart",
        "actual_result": "The total is 0.00",
        "expected_result": "The total is the sum of the items"}

def test_probe(client):
    from engine import db as _db
    client.post("/projects/db/create", data={"project_name": f"Chat {uuid.uuid4().hex[:6]}"},
                follow_redirects=True)
    with client.session_transaction() as s:
        pid = s["project_id"]
    # The project already owns BUG-001, in Postgres.
    _db.save_bug(pid, {"id": "BUG-001", "title": "An earlier finding",
                       "severity": "Major", "priority": "High", "status": "Open"})
    # The realistic trigger: the session's mirror is empty, as it is after
    # every restart (bug_reports_data is in GENERATED_KEYS).
    with client.session_transaction() as s:
        s.pop("bug_reports_data", None)

    r = client.post("/chat/bug-form", data=FORM)
    body = json.loads(r.get_data(as_text=True))
    print("\n  response id      :", body.get("id"))
    print("  response message :", body.get("message"))
    rows = _db.list_bugs(pid) or []
    print("  stored external ids:", sorted((x.get("external_id") or "") for x in rows))
    with client.session_transaction() as s:
        mirror = [b.get("id") for b in (s.get("bug_reports_data") or [])]
    print("  session mirror ids :", mirror)
