"""Optional telemetry must never interrupt message delivery on older servers."""
import httpx


async def report(client, room_url, suffix, payload):
    try:
        response = await client.post(room_url + suffix, json=payload, timeout=3)
        return response.status_code == 200
    except httpx.HTTPError:
        return False
