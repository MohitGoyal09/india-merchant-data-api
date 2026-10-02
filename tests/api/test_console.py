"""The merchant console: static page, strict CSP, same-origin assets, nothing inline."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from imda.api.console import routes as console_routes
from tests.api.helpers import assert_error

EXPECTED_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
)
CONSOLE_DIR = Path(console_routes.__file__).parent
ASSET_PREFIX = "/console/assets/"


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.refs: list[str] = []
        self.inline_refs: list[str] = []
        self.script_bodies: list[str] = []
        self.styled_tags: list[str] = []
        self.style_tags = 0
        self.handlers: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = dict(attrs)
        for key in ("src", "href"):
            ref = found.get(key)
            if ref and tag in {"script", "link", "img"}:
                (self.inline_refs if ref.startswith("data:") else self.refs).append(ref)
        if "style" in found:
            self.styled_tags.append(tag)
        if tag == "style":
            self.style_tags += 1
        self.handlers.extend(k for k in found if k.startswith("on"))
        self._in_script = tag == "script"

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script and data.strip():
            self.script_bodies.append(data)


@pytest.fixture
def page(client: TestClient) -> _Page:
    parsed = _Page()
    parsed.feed(client.get("/console").text)
    return parsed


def test_console_returns_html_with_strict_csp(client: TestClient) -> None:
    response = client.get("/console")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["content-security-policy"] == EXPECTED_CSP
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "<h1>Merchant Console</h1>" in response.text


@pytest.mark.parametrize(
    ("name", "content_type"),
    [
        ("console.css", "text/css"),
        ("console.js", "text/javascript"),
        ("chart.js", "text/javascript"),
    ],
)
def test_assets_are_served_with_their_content_type(
    client: TestClient, name: str, content_type: str
) -> None:
    response = client.get(f"{ASSET_PREFIX}{name}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(content_type)
    assert response.headers["content-security-policy"] == EXPECTED_CSP
    assert response.content


def test_index_references_only_same_origin_assets(client: TestClient, page: _Page) -> None:
    assert page.refs
    assert page.inline_refs == ["data:,"]  # the empty favicon, so browsers do not request one
    for ref in page.refs:
        assert ref.startswith(ASSET_PREFIX), ref
        assert client.get(ref).status_code == 200


def test_index_has_no_inline_script_or_style(page: _Page) -> None:
    assert page.script_bodies == []
    assert page.style_tags == 0
    assert page.styled_tags == []
    assert page.handlers == []


def test_every_module_import_resolves_to_a_served_asset(client: TestClient) -> None:
    for path in CONSOLE_DIR.glob("*.js"):
        for target in re.findall(r'from "\./([\w-]+\.js)"', path.read_text(encoding="utf-8")):
            assert client.get(f"{ASSET_PREFIX}{target}").status_code == 200, (path.name, target)


def test_scripts_never_write_api_text_as_html() -> None:
    forbidden = re.compile(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(")
    for path in CONSOLE_DIR.glob("*.js"):
        assert not forbidden.search(path.read_text(encoding="utf-8")), path.name


@pytest.mark.parametrize("name", ["missing.js", "index.html", "routes.py", "..%2Fapp.py"])
def test_unknown_or_non_asset_files_are_not_served(client: TestClient, name: str) -> None:
    response = client.get(f"{ASSET_PREFIX}{name}")

    assert_error(response, 404, "NOT_FOUND")


def test_console_is_not_part_of_the_openapi_schema(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert not [p for p in paths if p.startswith("/console")]
