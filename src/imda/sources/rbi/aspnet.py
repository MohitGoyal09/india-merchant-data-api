"""Pure helpers for RBI's ASP.NET WebForms pages (hidden-field postback)."""

from __future__ import annotations

from collections.abc import Mapping

from selectolax.parser import HTMLParser, Node

from imda.models import Dataset, Source
from imda.sources.base import (
    HttpClient,
    ParseError,
    RawPayload,
    UpstreamError,
    UpstreamRequest,
)

REQUIRED_FIELDS = ("__VIEWSTATE", "__EVENTVALIDATION")


def extract_form_state(
    html: str,
    *,
    source: Source = Source.RBI,
    dataset: Dataset = Dataset.HOLIDAYS,
) -> dict[str, str]:
    """Return every named hidden input as ``{name: value}``.

    Raises ``ParseError`` when ``__VIEWSTATE`` or ``__EVENTVALIDATION`` is absent,
    because a POST without them is rejected by the server.
    """
    state: dict[str, str] = {}
    for node in HTMLParser(html).css("input[type=hidden]"):
        name = node.attributes.get("name")
        if name:
            state[name] = node.attributes.get("value") or ""
    missing = [field for field in REQUIRED_FIELDS if field not in state]
    if missing:
        raise ParseError(source, dataset, f"hidden form field(s) missing: {', '.join(missing)}")
    return state


def extract_select_options(
    html: str,
    select_name: str,
    *,
    source: Source = Source.RBI,
    dataset: Dataset = Dataset.HOLIDAYS,
) -> list[tuple[str, str]]:
    """Return ``(value, label)`` for each option of ``<select name=select_name>``."""
    select = HTMLParser(html).css_first(f'select[name="{select_name}"]')
    if select is None:
        raise ParseError(source, dataset, f"select {select_name!r} not found")
    options: list[tuple[str, str]] = []
    for option in select.css("option"):
        label = option.text(strip=True)
        value = option.attributes.get("value")
        options.append((value if value is not None else label, label))
    return options


def clean_text(node: Node | None) -> str:
    """Visible text of ``node`` with non-breaking spaces and runs of whitespace collapsed."""
    if node is None:
        return ""
    return " ".join(node.text().replace("\xa0", " ").split())


def form_field_names(html: str) -> list[str]:
    """Sorted names of every input and select on the page (structure only, no values)."""
    tree = HTMLParser(html)
    names = {n.attributes.get("name") for n in tree.css("input, select")}
    return sorted(name for name in names if name)


class PostbackSession:
    """GET a WebForms page once, then POST against it, reusing the freshest hidden fields.

    The hidden state of each response page is kept for the next POST. When the cached state
    is rejected (``UpstreamError``), the session re-GETs once and retries; a failure with
    freshly fetched state is raised as is.
    """

    def __init__(self, url: str, *, source: Source, dataset: Dataset) -> None:
        self._url = url
        self._source = source
        self._dataset = dataset
        self._state: dict[str, str] | None = None

    def post(self, client: HttpClient, fields: Mapping[str, str]) -> RawPayload:
        had_cached_state = self._state is not None
        try:
            raw = self._post_once(client, fields)
        except UpstreamError:
            if not had_cached_state:
                raise
            self._state = None
            raw = self._post_once(client, fields)
        self._state = self._state_from(raw)
        return raw

    def _post_once(self, client: HttpClient, fields: Mapping[str, str]) -> RawPayload:
        state = self._state if self._state is not None else self._load_state(client)
        return client.send(UpstreamRequest(method="POST", url=self._url, form={**state, **fields}))

    def _load_state(self, client: HttpClient) -> dict[str, str]:
        page = client.send(UpstreamRequest(method="GET", url=self._url))
        return extract_form_state(page.text(), source=self._source, dataset=self._dataset)

    def _state_from(self, raw: RawPayload) -> dict[str, str] | None:
        try:
            return extract_form_state(raw.text(), source=self._source, dataset=self._dataset)
        except ParseError:
            return None
