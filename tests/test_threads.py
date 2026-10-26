from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from covenant.app import create_app
from test_app import setup, bearer, send
from test_receipts import operator_send, receipts


def reply(client, rid, author, parent, text="@everyone Thread reply", headers=None, mid=None):
    return client.post(f"/{rid}/messages" if author else f"/api/rooms/{rid}/messages",
        headers=bearer(author) if author else headers,
        json={"text": text, "client_message_id": mid or str(uuid4()), "reply_to": parent})


def test_nested_threads_are_bounded_and_exclude_other_discussions(setup):
    client, _, _, headers, rid, (alice, bob) = setup
    root = send(client, rid, alice, "@bob Review this component").json()
    unrelated = send(client, rid, bob, "@everyone Other discussion").json()
    first = reply(client, rid, bob, root["id"], "@alice Initial evidence").json()
    nested = reply(client, rid, None, first["id"], "@alice Please check the assumptions", headers).json()
    last = reply(client, rid, alice, nested["id"], "@operator Checked").json()
    assert nested["reply_to"] == first["id"] and nested["thread_root"] == root["id"]
    page = client.get(f"/{rid}/threads/{nested['id']}?limit=2", headers=bearer(bob)).json()
    assert page["root"] == root
    assert [m["id"] for m in page["messages"]] == [first["id"], nested["id"]]
    assert page["has_more"] and page["next_cursor"] == nested["id"]
    next_page = client.get(f"/{rid}/threads/{root['id']}?after={page['next_cursor']}&limit=2", headers=bearer(bob)).json()
    assert next_page["messages"] == [last] and not next_page["has_more"]
    assert unrelated not in page["messages"]
    # Normal inbox filtering is unchanged; explicit thread context includes other recipients.
    inbox = client.get(f"/{rid}/messages?timeout=0", headers=bearer(bob)).json()
    assert [m["id"] for m in inbox["messages"]] == [root["id"]]
    assert receipts(client, rid, nested["id"])[0]["status"] == "answered"


def test_any_room_message_can_be_replied_to_but_cross_room_links_are_rejected(setup):
    client, _, _, headers, rid, (alice, bob) = setup
    root = send(client, rid, bob, "@operator Need input").json()
    response = reply(client, rid, None, root["id"], "@bob Here is the decision", headers)
    assert response.status_code == 200 and response.json()["reply_to"] == root["id"]
    other = client.post("/api/rooms", headers=headers, json={"title": "Other"}).json()["id"]
    carol = client.post(f"/api/rooms/{other}/agents", headers=headers, json={"handle": "carol"}).json()
    foreign = send(client, other, carol).json()
    assert reply(client, rid, alice, foreign["id"]).status_code == 422
    assert client.get(f"/{rid}/threads/{foreign['id']}", headers=bearer(alice)).status_code == 404
    assert client.get(f"/{other}/threads/{foreign['id']}", headers=bearer(alice)).status_code == 401
    assert client.get(f"/{rid}/threads/{root['id']}").status_code == 401
    client.cookies.clear()
    assert client.get(f"/api/rooms/{rid}/threads/{root['id']}", headers=bearer(alice)).status_code == 401


def test_reply_retries_preserve_parent_and_do_not_duplicate(setup):
    client, _, _, headers, rid, (alice, bob) = setup
    root = operator_send(client, rid, headers)["id"]
    other = send(client, rid, bob).json()["id"]
    first = reply(client, rid, alice, root, "@operator Complete", mid="retry").json()
    assert reply(client, rid, alice, root, "@operator Complete", mid="retry").json() == first
    assert reply(client, rid, alice, other, "@operator Complete", mid="retry").status_code == 409
    thread = client.get(f"/api/rooms/{rid}/threads/{root}").json()
    assert len(thread["messages"]) == 1
    assert thread["root"]["receipts"][0]["status"] == "answered"


def test_archive_revocation_restart_and_thread_read_cursor(setup):
    client, _, settings, headers, rid, (alice, bob) = setup
    root = operator_send(client, rid, headers)["id"]
    child = reply(client, rid, bob, root, "@operator Extra context").json()
    # Reading a thread never acknowledges the operator message or advances an inbox.
    page = client.get(f"/{rid}/threads/{child['id']}", headers=bearer(alice)).json()
    assert receipts(client, rid, root)[0]["status"] == "queued"
    assert client.get(f"/{rid}/messages?timeout=0", headers=bearer(alice)).json()["messages"][0]["id"] == root
    with TestClient(create_app(settings), base_url=settings.public_base_url) as restarted:
        assert restarted.get(f"/{rid}/threads/{root}", headers=bearer(alice)).json() == page
    client.patch(f"/api/rooms/{rid}", headers=headers, json={"status": "archived"})
    assert client.get(f"/{rid}/threads/{root}", headers=bearer(alice)).status_code == 200
    assert reply(client, rid, None, root, headers=headers).status_code == 409
    client.post(f"/api/rooms/{rid}/agents/{alice['id']}/revoke", headers=headers)
    assert client.get(f"/{rid}/threads/{root}", headers=bearer(alice)).status_code == 401
    assert client.delete(f"/api/rooms/{rid}", headers=headers).status_code == 200


@pytest.mark.parametrize("query", ["limit=0", "limit=51", "after=-1"])
def test_thread_pagination_limits(setup, query):
    client, _, _, _, rid, (alice, _) = setup
    mid = send(client, rid, alice).json()["id"]
    assert client.get(f"/{rid}/threads/{mid}?{query}", headers=bearer(alice)).status_code == 422


def test_operator_thread_visibility_includes_replies_outside_loaded_page(setup):
    client, _, _, headers, rid, (alice, bob) = setup
    root = send(client, rid, alice, "@bob Review this").json()["id"]
    solo = send(client, rid, alice, "@bob Separate update").json()["id"]
    before = client.get(f"/api/rooms/{rid}/messages").json()["messages"]
    assert all(not m["has_thread"] for m in before)
    child = reply(client, rid, bob, root, "@alice Reviewed").json()["id"]
    # The reply is outside this history page, but its root still has a thread.
    history = client.get(f"/api/rooms/{rid}/messages?before={child}").json()["messages"]
    assert {m["id"]: m["has_thread"] for m in history} == {root: True, solo: False}
    latest = client.get(f"/api/rooms/{rid}/messages?after={solo}").json()["messages"]
    assert latest[0]["has_thread"] and latest[0]["reply_to"] == root
