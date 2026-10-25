import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from covenant.app import create_app
from test_app import setup, bearer, send


def acquire(client, rid, invitation, paths, seconds=300):
    result = client.post(f"/{rid}/file-locks/acquire", headers=bearer(invitation),
                         json={"paths": paths, "lease_seconds": seconds})
    assert result.status_code == 200, result.text
    return result.json()


def lease_action(client, rid, invitation, action, lock_id):
    return client.post(f"/{rid}/file-locks/{action}", headers=bearer(invitation), json={"lock_id": lock_id})


def test_filtered_empty_pages_advance_without_losing_addressed_messages(setup):
    client, app, _, _, rid, (alice, bob) = setup
    with app.state.db.connect() as conn:
        conn.executemany("INSERT INTO messages(room_id,sender,target,text,client_message_id,created_at) VALUES (?,?,?,?,?,?)",
            [(rid, "bob", "operator", "@operator unrelated", str(i), time.time()) for i in range(205)])
    own = send(client, rid, alice, "@everyone My own post").json()
    direct = send(client, rid, bob, "@operator @alice Context needed").json()
    broadcast = send(client, rid, bob, "@everyone Shared decision").json()
    cursor, received = 0, []
    for index in range(3):
        page = client.get(f"/{rid}/messages?after={cursor}&timeout=0", headers=bearer(alice)).json()
        assert page["next_cursor"] > cursor
        if index < 2:
            assert page["messages"] == [] and page["has_more"]
        received.extend(page["messages"])
        cursor = page["next_cursor"]
    assert received == [direct, broadcast] and cursor == broadcast["id"]
    assert not page["has_more"]
    assert client.get(f"/{rid}/messages?after={cursor}&timeout=0", headers=bearer(alice)).json()["messages"] == []
    all_posts = client.get(f"/{rid}/history?scope=all&limit=50", headers=bearer(alice)).json()
    assert own in all_posts["messages"]
    assert len(client.get(f"/api/rooms/{rid}/messages").json()["messages"]) == 100


def test_join_bounded_catchup_and_history_cursors_are_independent(setup):
    client, app, _, _, rid, (alice, bob) = setup
    with app.state.db.connect() as conn:
        conn.executemany("INSERT INTO messages(room_id,sender,target,text,client_message_id,created_at) VALUES (?,?,?,?,?,?)",
            [(rid, "bob", "alice", f"@alice relevant {i}", str(i), time.time()) for i in range(60)])
        conn.executemany("INSERT INTO messages(room_id,sender,target,text,client_message_id,created_at) VALUES (?,?,?,?,?,?)",
            [(rid, "bob", "operator", "@operator unrelated", str(i+60), time.time()) for i in range(60)])
    joined = client.post(f"/{rid}/join", headers=bearer(alice), json={}).json()
    assert len(joined["messages"]) == 20 and joined["history_truncated"]
    assert joined["next_cursor"] == 120 and joined["next_before"] == 41
    older = client.get(f"/{rid}/history?before={joined['next_before']}&limit=20", headers=bearer(alice)).json()
    assert [m["id"] for m in older["messages"]] == list(range(21, 41))
    assert "next_cursor" not in older
    empty = client.post(f"/{rid}/join", headers=bearer(alice), json={"history_limit": 0}).json()
    assert empty["messages"] == [] and empty["next_cursor"] == 120 and empty["history_truncated"]
    shared = client.post(f"/{rid}/join", headers=bearer(alice), json={"history_limit": 5, "history_scope": "all"}).json()
    assert len(shared["messages"]) == 5 and all(m["target"] == "operator" for m in shared["messages"])
    pending = send(client, rid, bob, "@alice While disconnected").json()
    client.post(f"/{rid}/join", headers=bearer(alice), json={"history_limit": 0})
    assert client.get(f"/{rid}/messages?after={joined['next_cursor']}&timeout=0", headers=bearer(alice)).json()["messages"] == [pending]
    assert client.get(f"/{rid}/history?limit=51", headers=bearer(alice)).status_code == 422
    assert client.post(f"/{rid}/join", headers=bearer(alice), json={"history_limit": 51}).status_code == 422
    assert client.get(f"/{rid}/history").status_code == 401


def test_atomic_file_batch_conflict_ownership_renewal_and_release(setup):
    client, _, _, _, rid, (alice, bob) = setup
    first = acquire(client, rid, alice, ["/workspace/a.py", "/workspace/./b.py"])
    assert first["acquired"] and len(first["paths"]) == 2
    failed = acquire(client, rid, bob, ["/workspace/b.py", "/workspace/c.py"])
    assert not failed["acquired"] and failed["conflicts"][0]["owner"] == "alice"
    assert "lock_id" not in failed["conflicts"][0]
    assert acquire(client, rid, alice, ["/workspace/c.py"])["acquired"]  # No partial batch from Bob.
    assert lease_action(client, rid, bob, "release", first["lock_id"]).json()["released"] == 0
    assert not lease_action(client, rid, bob, "renew", first["lock_id"]).json()["renewed"]
    renewed = lease_action(client, rid, alice, "renew", first["lock_id"]).json()
    assert renewed["renewed"] and renewed["expires_at"] >= first["expires_at"]
    assert not acquire(client, rid, bob, ["/workspace/a.py"])["acquired"]
    own = client.get(f"/{rid}/file-locks", headers=bearer(alice)).json()["locks"]
    assert all("lock_id" in item for item in own)
    peer = client.get(f"/{rid}/file-locks", headers=bearer(bob)).json()["locks"]
    assert all("lock_id" not in item for item in peer)
    assert lease_action(client, rid, alice, "release", first["lock_id"]).json()["released"] == 2
    assert lease_action(client, rid, alice, "release", first["lock_id"]).json()["released"] == 0
    assert acquire(client, rid, bob, ["/workspace/a.py"])["acquired"]


def test_simultaneous_acquisition_across_server_instances_has_one_winner(setup):
    client, _, settings, _, rid, (alice, bob) = setup
    barrier = Barrier(2)
    def compete(c, invitation):
        barrier.wait(timeout=3)
        return acquire(c, rid, invitation, ["/workspace/race.py"])
    with TestClient(create_app(settings), base_url=settings.public_base_url) as second:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(compete, client, alice)
            b = pool.submit(compete, second, bob)
            results = [a.result(timeout=5), b.result(timeout=5)]
    assert sorted(r["acquired"] for r in results) == [False, True]


def test_expired_lease_cannot_renew_or_release_replacement(setup):
    client, app, _, _, rid, (alice, bob) = setup
    first = acquire(client, rid, alice, ["/workspace/a.py"])
    with app.state.db.connect() as conn:
        conn.execute("UPDATE file_locks SET expires_at=0")
    second = acquire(client, rid, bob, ["/workspace/a.py"])
    assert second["acquired"] and second["lock_id"] != first["lock_id"]
    assert not lease_action(client, rid, alice, "renew", first["lock_id"]).json()["renewed"]
    assert lease_action(client, rid, alice, "release", first["lock_id"]).json()["released"] == 0
    assert not acquire(client, rid, alice, ["/workspace/a.py"])["acquired"]


@pytest.mark.parametrize("event", ["revoke", "rotate", "archive", "delete"])
def test_agent_and_room_lifecycle_end_locks(setup, event):
    client, _, _, headers, rid, (alice, bob) = setup
    first = acquire(client, rid, alice, ["/workspace/lifecycle.py"])
    if event in {"archive", "delete"}:
        assert client.patch(f"/api/rooms/{rid}", headers=headers, json={"status": "archived"}).status_code == 200
        if event == "delete":
            assert client.delete(f"/api/rooms/{rid}", headers=headers).status_code == 200
            return
        client.patch(f"/api/rooms/{rid}", headers=headers, json={"status": "active"})
    else:
        assert client.post(f"/api/rooms/{rid}/agents/{alice['id']}/{event}", headers=headers).status_code == 200
        assert lease_action(client, rid, alice, "renew", first["lock_id"]).status_code == 401
    assert acquire(client, rid, bob, ["/workspace/lifecycle.py"])["acquired"]


def test_locks_span_rooms_preserve_private_ownership_and_survive_restart(setup):
    client, _, settings, headers, rid, (alice, _) = setup
    other = client.post("/api/rooms", headers=headers, json={"title": "Other"}).json()["id"]
    carol = client.post(f"/api/rooms/{other}/agents", headers=headers, json={"handle": "carol"}).json()
    first = acquire(client, rid, alice, ["/workspace/shared.py"])
    with TestClient(create_app(settings), base_url=settings.public_base_url) as restarted:
        conflict = acquire(restarted, other, carol, ["/workspace/shared.py"])
        assert not conflict["acquired"] and conflict["conflicts"][0]["owner"] == "another room"
        assert restarted.get(f"/{other}/file-locks", headers=bearer(carol)).json()["locks"] == []
        assert restarted.get(f"/{rid}/file-locks", headers=bearer(carol)).status_code == 401
        assert lease_action(restarted, rid, alice, "release", first["lock_id"]).json()["released"] == 1
        assert acquire(restarted, other, carol, ["/workspace/shared.py"])["acquired"]


def test_pause_holds_existing_lease_but_prevents_new_acquisition(setup):
    client, _, _, headers, rid, (alice, _) = setup
    first = acquire(client, rid, alice, ["/workspace/pause.py"])
    client.patch(f"/api/rooms/{rid}", headers=headers, json={"status": "paused"})
    assert lease_action(client, rid, alice, "renew", first["lock_id"]).json()["renewed"]
    assert client.post(f"/{rid}/file-locks/acquire", headers=bearer(alice), json={"paths": ["/workspace/new.py"]}).status_code == 409
    assert lease_action(client, rid, alice, "release", first["lock_id"]).json()["released"] == 1


@pytest.mark.parametrize("body", [{"paths": []}, {"paths": ["relative.py"]}, {"paths": ["/"]},
    {"paths": ["/workspace/a.py"], "lease_seconds": 0}, {"paths": ["/workspace/a.py"], "lease_seconds": 3601}])
def test_invalid_locks_are_rejected(setup, body):
    client, _, _, _, rid, (alice, _) = setup
    assert client.post(f"/{rid}/file-locks/acquire", headers=bearer(alice), json=body).status_code == 422


def test_lock_schema_upgrade_is_additive_and_repeatable(setup):
    client, app, _, _, rid, (alice, bob) = setup
    message = send(client, rid, bob, "@alice Preserve me").json()
    with app.state.db.connect() as conn:
        conn.execute("DROP TABLE file_locks")
        before = {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t}")]
                  for t in ("rooms", "agents", "messages", "operator", "sessions")}
    app.state.db.initialize()
    app.state.db.initialize()
    with app.state.db.connect() as conn:
        after = {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t}")] for t in before}
    assert before == after
    assert acquire(client, rid, alice, ["/workspace/upgraded.py"])["acquired"]
    assert client.get(f"/{rid}/messages?timeout=0", headers=bearer(alice)).json()["messages"] == [message]
