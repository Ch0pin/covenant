import pytest
from test_app import setup, bearer, send
from covenant.approvals import create_bridge
from covenant.auth import digest


def test_rename_preserves_routing_identity_and_retries(setup):
    client, app, _, headers, rid, (alice, bob) = setup
    aid = client.post(f"/{rid}/join", headers=bearer(alice), json={}).json()["participants"][1]["id"]
    old = send(client, rid, bob, "@alice queued").json()
    own = send(client, rid, alice, "@bob result", "stable-retry").json()
    endpoint = f"/api/rooms/{rid}/agents/{aid}"
    response = client.patch(endpoint, headers=headers, json={"handle":"Reviewer", "display_name":"Static Reviewer"})
    assert response.status_code == 200
    assert response.json()["handle"] == "reviewer"
    assert send(client, rid, alice, "@bob result", "stable-retry").json()["id"] == own["id"]
    new = send(client, rid, bob, "@reviewer current").json()
    legacy = send(client, rid, bob, "@alice previous").json()
    for message in (new, legacy):
        assert set(message["targets"]) == {"alice", "reviewer"}
        assert message["target"] == "reviewer"
    inbox = client.get(f"/{rid}/messages?after=0&timeout=0", headers=bearer(alice)).json()["messages"]
    assert [m["id"] for m in inbox] == [old["id"], new["id"], legacy["id"]]
    joined = client.post(f"/{rid}/join", headers=bearer(alice), json={}).json()
    person = next(p for p in joined["participants"] if p.get("id") == aid)
    assert person["display_name"] == "Static Reviewer"
    assert person["aliases"] == ["alice"]
    assert joined["handle"] == "reviewer"
    bridge, _ = create_bridge(app.state.db, rid, "alice", "codex", expected_token_hash=digest(alice["token"]))
    with app.state.db.connect() as conn:
        assert conn.execute("SELECT agent_id FROM approval_bridges WHERE id=?", (bridge,)).fetchone()[0] == aid
    with pytest.raises(ValueError):
        create_bridge(app.state.db, rid, "alice", "codex", expected_token_hash="wrong")
    assert client.post(f"/api/rooms/{rid}/agents", headers=headers, json={"handle":"alice"}).status_code == 409
    assert client.patch(endpoint, headers=headers, json={"handle":"alice"}).status_code == 200
    assert send(client, rid, bob, "@reviewer reclaimed").status_code == 200


def test_rename_validation_and_authorization(setup):
    client, _, _, headers, rid, (alice, bob) = setup
    people = client.post(f"/{rid}/join", headers=bearer(alice), json={}).json()["participants"]
    aid = people[1]["id"]
    endpoint = f"/api/rooms/{rid}/agents/{aid}"
    assert client.patch(endpoint, json={"handle":"new"}).status_code == 403
    assert client.patch(endpoint, headers=bearer(bob), json={"handle":"new"}).status_code == 403
    for handle, status in [("bob",409),("operator",422),("everyone",422),("bad name",422)]:
        assert client.patch(endpoint, headers=headers, json={"handle":handle}).status_code == status
    assert client.patch(endpoint, headers=headers, json={"handle":"alice","display_name":"a\nb"}).status_code == 422
    other = client.post("/api/rooms", headers=headers, json={"title":"Other"}).json()["id"]
    assert client.patch(f"/api/rooms/{other}/agents/{aid}", headers=headers, json={"handle":"new"}).status_code == 404
