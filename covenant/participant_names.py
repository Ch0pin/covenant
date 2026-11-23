"""Historical handles remain routable after a participant rename."""

def aliases(conn, room_id):
    return {r['handle']: r['current_handle'] for r in conn.execute(
        """SELECT h.handle,a.handle AS current_handle FROM agent_handle_aliases h
        JOIN agents a ON a.id=h.agent_id WHERE h.room_id=?""", (room_id,))}


def resolve_handle(conn, room_id, handle):
    return aliases(conn, room_id).get(handle, handle)


def expand_targets(targets, names):
    canonical = {names.get(t, t) for t in targets}
    return list(dict.fromkeys([*targets, *(names.get(t, t) for t in targets),
                              *(old for old, current in names.items() if current in canonical)]))
