"""End-to-end tests: drive a complete SOLOQ draft through the web API with
`fastapi.testclient.TestClient`, structurally parallel to `tests/test_e2e_cli.py`.
`TestClient` is Starlette's, built on `httpx` (already an approved dependency) --
no new *test* dependency beyond `fastapi` itself. `ManualCSVProvider` (the default)
never touches the network, so this is fully offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from draftiq.web.app import create_app


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


BANS = [
    "Malphite",
    "Jax",
    "Renekton",
    "Sejuani",
    "Kindred",
    "Xin Zhao",
    "Orianna",
    "Yasuo",
    "Miss Fortune",
    "Nautilus",
]

# (champion, role) in strict B1 / R1 R2 / B2 B3 / R3 R4 / B4 B5 / R5 order.
PICKS = [
    ("Aatrox", "top"),
    ("Darius", "top"),
    ("Vi", "jungle"),
    ("Lee Sin", "jungle"),
    ("Ahri", "mid"),
    ("Zed", "mid"),
    ("Caitlyn", "bottom"),
    ("Jinx", "bottom"),
    ("Thresh", "support"),
    ("Lulu", "support"),
]


def _champion_ids_by_name(client: TestClient) -> dict[str, int]:
    resp = client.get("/api/champions")
    assert resp.status_code == 200, resp.text
    return {c["name"]: c["champion_id"] for c in resp.json()}


def test_full_soloq_draft_completes_via_api(client: TestClient) -> None:
    resp = client.post("/api/draft/new", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["next_action"] == "ban"

    by_name = _champion_ids_by_name(client)

    for champ in BANS:
        resp = client.post("/api/draft/ban", json={"champion_id": by_name[champ]})
        assert resp.status_code == 200, resp.text

    resp = client.get("/api/draft/suggest", params={"role": "top"})
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) > 0

    for champ, role in PICKS:
        resp = client.post("/api/draft/pick", json={"champion_id": by_name[champ], "role": role})
        assert resp.status_code == 200, resp.text

    resp = client.get("/api/draft/state")
    body = resp.json()
    assert body["is_complete"] is True
    names = {a["champion_name"] for a in body["actions"]}
    assert "Aatrox" in names
    assert "Lulu" in names

    # No more actions should be legal once the draft is complete.
    resp = client.post("/api/draft/ban", json={"champion_id": by_name["Aatrox"]})
    assert resp.status_code == 409, resp.text


def test_ban_before_new_returns_404(client: TestClient) -> None:
    resp = client.post("/api/draft/ban", json={"champion_id": 1})
    assert resp.status_code == 404
    assert "No draft in progress" in resp.json()["detail"]


def test_duplicate_ban_returns_409(client: TestClient) -> None:
    client.post("/api/draft/new", json={})
    by_name = _champion_ids_by_name(client)
    aatrox_id = by_name["Aatrox"]
    resp1 = client.post("/api/draft/ban", json={"champion_id": aatrox_id})
    assert resp1.status_code == 200
    resp2 = client.post("/api/draft/ban", json={"champion_id": aatrox_id})
    assert resp2.status_code == 409
    assert "already" in resp2.json()["detail"].lower()


def test_unknown_champion_id_returns_400(client: TestClient) -> None:
    client.post("/api/draft/new", json={})
    resp = client.post("/api/draft/ban", json={"champion_id": 999999})
    assert resp.status_code == 400
    assert "Unknown champion_id" in resp.json()["detail"]


def test_suggest_without_role_during_pick_phase_returns_400(client: TestClient) -> None:
    client.post("/api/draft/new", json={})
    by_name = _champion_ids_by_name(client)
    for champ in BANS:
        client.post("/api/draft/ban", json={"champion_id": by_name[champ]})
    resp = client.get("/api/draft/suggest")
    assert resp.status_code == 400
    assert "--role is required" in resp.json()["detail"]


def test_suggest_any_role_during_ban_phase_returns_400(client: TestClient) -> None:
    client.post("/api/draft/new", json={})
    resp = client.get("/api/draft/suggest", params={"any_role": "true"})
    assert resp.status_code == 400
    assert "only applies to picks" in resp.json()["detail"]


def test_suggest_any_role_and_lookahead_returns_400(client: TestClient) -> None:
    client.post("/api/draft/new", json={})
    by_name = _champion_ids_by_name(client)
    for champ in BANS:
        client.post("/api/draft/ban", json={"champion_id": by_name[champ]})
    resp = client.get("/api/draft/suggest", params={"any_role": "true", "lookahead": "true"})
    assert resp.status_code == 400
    assert "can't be combined" in resp.json()["detail"]


def test_suggest_any_role_returns_recommendations_with_role(client: TestClient) -> None:
    client.post("/api/draft/new", json={})
    by_name = _champion_ids_by_name(client)
    for champ in BANS:
        client.post("/api/draft/ban", json={"champion_id": by_name[champ]})
    resp = client.get("/api/draft/suggest", params={"any_role": "true"})
    assert resp.status_code == 200
    recs = resp.json()
    assert recs
    assert all("role" in r for r in recs)


def test_build_returns_items_and_runes(client: TestClient) -> None:
    client.post("/api/draft/new", json={})
    by_name = _champion_ids_by_name(client)
    resp = client.get("/api/draft/build", params={"champion_id": by_name["Aatrox"], "role": "top"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"]
    assert body["runes_primary"]


def test_build_for_missing_data_returns_404(client: TestClient) -> None:
    """The manual dataset only has build data for each champion's own primary role
    (see data/manual/builds.csv) -- Aatrox (top-only) has no jungle build."""
    client.post("/api/draft/new", json={})
    by_name = _champion_ids_by_name(client)
    resp = client.get(
        "/api/draft/build", params={"champion_id": by_name["Aatrox"], "role": "jungle"}
    )
    assert resp.status_code == 404
    assert "No build data available" in resp.json()["detail"]


def test_get_champions_requires_active_draft(client: TestClient) -> None:
    resp = client.get("/api/champions")
    assert resp.status_code == 404


def test_new_overwrites_existing_draft(client: TestClient) -> None:
    client.post("/api/draft/new", json={})
    by_name = _champion_ids_by_name(client)
    client.post("/api/draft/ban", json={"champion_id": by_name["Aatrox"]})

    resp = client.post("/api/draft/new", json={})
    assert resp.status_code == 200
    assert resp.json()["actions"] == []


def test_health(client: TestClient) -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_index_serves_html(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "draftiq" in resp.text
