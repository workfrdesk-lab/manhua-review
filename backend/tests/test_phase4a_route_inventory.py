"""Inventory guard only; this is not a claim of exhaustive A11 coverage."""

from app.story import router

STORY_ROUTES = {
    ("POST", "/api/v1/chapters/{chapter_id}/story/analyze"),
    ("GET", "/api/v1/chapters/{chapter_id}/story/status"),
    ("GET", "/api/v1/chapters/{chapter_id}/characters"),
    ("GET", "/api/v1/chapters/{chapter_id}/scenes"),
    ("GET", "/api/v1/scenes/{scene_id}"),
    ("GET", "/api/v1/scenes/{scene_id}/events"),
    ("GET", "/api/v1/chapters/{chapter_id}/story/summary"),
    ("PATCH", "/api/v1/characters/{character_id}"),
    ("PATCH", "/api/v1/scenes/{scene_id}"),
    ("PATCH", "/api/v1/events/{event_id}"),
    ("POST", "/api/v1/characters/{character_id}/merge"),
    ("POST", "/api/v1/characters/{character_id}/split"),
    ("GET", "/api/v1/chapters/{chapter_id}/story/versions"),
    ("GET", "/api/v1/chapters/{chapter_id}/relationships"),
    ("DELETE", "/api/v1/events/{event_id}"),
}


def test_story_route_inventory():
    actual = {(method, route.path) for route in router.routes for method in route.methods}
    assert actual == STORY_ROUTES, "Story routes changed: update the A11 acceptance matrix"
