"""Tests for source citation extraction and filtering utilities."""

import json

import pytest
from components.utils import (
    extract_and_strip_sources_block,
    filter_sources_by_citations,
    stream_with_source_filtering,
)


class TestExtractAndStripSourcesBlock:
    def test_basic_extraction(self):
        text = "Answer text\n[Sources: 1, 3]"
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == "Answer text"
        assert citations == {1, 3}

    def test_single_source(self):
        text = "Answer text\n[Source: 2]"
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == "Answer text"
        assert citations == {2}

    def test_many_sources(self):
        text = "Answer text\n[Sources: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]"
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == "Answer text"
        assert citations == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10}

    def test_no_sources_block(self):
        text = "Answer with no block"
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == "Answer with no block"
        assert citations is None

    def test_sources_with_trailing_whitespace(self):
        text = "Answer text\n[Sources: 1, 3]   "
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == "Answer text"
        assert citations == {1, 3}

    def test_sources_with_extra_spaces(self):
        text = "Answer text\n[Sources:  1 ,  3 , 5 ]"
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == "Answer text"
        assert citations == {1, 3, 5}

    def test_multiline_answer(self):
        text = "Line 1\n\nLine 2\n\nLine 3\n[Sources: 2, 4]"
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == "Line 1\n\nLine 2\n\nLine 3"
        assert citations == {2, 4}

    def test_empty_string(self):
        clean, citations = extract_and_strip_sources_block("")
        assert clean == ""
        assert citations is None

    def test_sources_mid_text_not_stripped(self):
        text = "Answer [Sources: 1, 2] and more text after"
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == text
        assert citations is None

    def test_brackets_around_numbers_only(self):
        text = "Answer text\nSources: [1, 3]"
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == "Answer text"
        assert citations == {1, 3}

    def test_no_brackets_at_all(self):
        text = "Answer text\nSources: 1, 3"
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == "Answer text"
        assert citations == {1, 3}

    def test_singular_no_brackets(self):
        text = "Answer text\nSource: 2"
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == "Answer text"
        assert citations == {2}

    def test_sources_none(self):
        text = "Answer text\n[Sources: none]"
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == "Answer text"
        assert citations == set()

    def test_sources_none_no_brackets(self):
        text = "Answer text\nSources: none"
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == "Answer text"
        assert citations == set()

    def test_sources_none_capitalized(self):
        text = "Answer text\n[Sources: None]"
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == "Answer text"
        assert citations == set()


class TestFilterSourcesByCitations:
    def test_basic_filtering(self):
        sources = [
            {"source_type": "document", "file": "a"},
            {"source_type": "document", "file": "b"},
            {"source_type": "document", "file": "c"},
            {"source_type": "document", "file": "d"},
            {"source_type": "document", "file": "e"},
        ]
        result = filter_sources_by_citations(sources, {1, 3, 5})
        assert result == [sources[0], sources[2], sources[4]]

    def test_none_citations_returns_all(self):
        sources = [{"source_type": "document", "file": "a"}, {"source_type": "document", "file": "b"}]
        result = filter_sources_by_citations(sources, None)
        assert result == sources

    def test_empty_citations_returns_empty_for_docs(self):
        sources = [{"source_type": "document", "file": "a"}, {"source_type": "document", "file": "b"}]
        result = filter_sources_by_citations(sources, set())
        assert result == []

    def test_empty_citations_keeps_web_sources(self):
        web = {"source_type": "web", "url": "https://example.com"}
        sources = [{"source_type": "document", "file": "a"}, web]
        result = filter_sources_by_citations(sources, set())
        assert result == [web]

    def test_out_of_range_citations_fallback(self):
        sources = [{"source_type": "document", "file": "a"}, {"source_type": "document", "file": "b"}]
        result = filter_sources_by_citations(sources, {99})
        assert result == sources

    def test_partial_out_of_range(self):
        sources = [
            {"source_type": "document", "file": "a"},
            {"source_type": "document", "file": "b"},
            {"source_type": "document", "file": "c"},
        ]
        result = filter_sources_by_citations(sources, {1, 99})
        assert result == [sources[0]]

    def test_single_citation(self):
        sources = [
            {"source_type": "document", "file": "a"},
            {"source_type": "document", "file": "b"},
            {"source_type": "document", "file": "c"},
        ]
        result = filter_sources_by_citations(sources, {2})
        assert result == [sources[1]]

    def test_empty_sources(self):
        result = filter_sources_by_citations([], {1, 2})
        assert result == []

    def test_all_cited(self):
        sources = [
            {"source_type": "document", "file": "a"},
            {"source_type": "document", "file": "b"},
            {"source_type": "document", "file": "c"},
        ]
        result = filter_sources_by_citations(sources, {1, 2, 3})
        assert result == sources

    def test_preserves_order(self):
        sources = [
            {"source_type": "document", "file": "a"},
            {"source_type": "document", "file": "b"},
            {"source_type": "document", "file": "c"},
            {"source_type": "document", "file": "d"},
        ]
        result = filter_sources_by_citations(sources, {4, 2})
        assert result == [sources[1], sources[3]]

    def test_cited_docs_plus_uncited_web_preserved(self):
        doc1 = {"source_type": "document", "file": "a.pdf"}
        doc2 = {"source_type": "document", "file": "b.pdf"}
        web = {"source_type": "web", "url": "https://example.com"}
        sources = [doc1, doc2, web]
        result = filter_sources_by_citations(sources, {1})
        assert result == [doc1, web]

    def test_cited_web_not_duplicated(self):
        doc = {"source_type": "document", "file": "a.pdf"}
        web = {"source_type": "web", "url": "https://example.com"}
        sources = [doc, web]
        result = filter_sources_by_citations(sources, {1, 2})
        assert result == [doc, web]

    def test_web_only_sources_none_citations(self):
        web1 = {"source_type": "web", "url": "https://a.com"}
        web2 = {"source_type": "web", "url": "https://b.com"}
        sources = [web1, web2]
        result = filter_sources_by_citations(sources, set())
        assert result == [web1, web2]


# --- helpers for streaming tests ---


def _make_chunk(content: str, chunk_id: str = "chatcmpl-1") -> str:
    """Build an SSE line with a content delta."""
    return "data: " + json.dumps({"id": chunk_id, "choices": [{"delta": {"content": content}, "finish_reason": None}]})


def _make_finish(chunk_id: str = "chatcmpl-1") -> str:
    """Build an SSE line with finish_reason='stop'."""
    return "data: " + json.dumps({"id": chunk_id, "choices": [{"delta": {}, "finish_reason": "stop"}]})


DONE_LINE = "data: [DONE]"


async def _fake_stream(lines: list[str]):
    for line in lines:
        yield line


async def _collect(async_gen) -> list[str]:
    return [line async for line in async_gen]


def _parse_finish_sources(sse_lines: list[str]) -> list:
    """Extract the sources list from the finish chunk (second-to-last line before [DONE])."""
    for line in reversed(sse_lines):
        if line.startswith("data: ") and line.strip() != "data: [DONE]":
            data = json.loads(line[len("data: ") :])
            extra = data.get("extra")
            if extra and extra != "{}":
                return json.loads(extra).get("sources", [])
    return []


def _collect_content(sse_lines: list[str]) -> str:
    """Concatenate all content deltas from SSE lines."""
    parts = []
    for line in sse_lines:
        if not line.startswith("data: ") or line.strip() == "data: [DONE]":
            continue
        data = json.loads(line[len("data: ") :])
        content = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
        if content:
            parts.append(content)
    return "".join(parts)


class TestStreamWithSourceFiltering:
    SOURCES = [{"file": "a.pdf"}, {"file": "b.pdf"}, {"file": "c.pdf"}]

    @pytest.mark.asyncio
    async def test_case1_llm_cites_specific_sources(self):
        """Case 1: LLM cites [Sources: 1, 3] → only cited sources returned."""
        lines = [
            _make_chunk("Here is the answer."),
            _make_chunk("\n[Sources: 1, 3]"),
            _make_finish(),
            DONE_LINE,
        ]
        result = await _collect(stream_with_source_filtering(_fake_stream(lines), self.SOURCES, "test-model"))
        assert _collect_content(result) == "Here is the answer."
        assert _parse_finish_sources(result) == [{"file": "a.pdf"}, {"file": "c.pdf"}]

    @pytest.mark.asyncio
    async def test_case2_llm_says_sources_none(self):
        """Case 2: LLM says [Sources: none] → no sources returned."""
        lines = [
            _make_chunk("I cannot find this in the documents."),
            _make_chunk("\n[Sources: none]"),
            _make_finish(),
            DONE_LINE,
        ]
        result = await _collect(stream_with_source_filtering(_fake_stream(lines), self.SOURCES, "test-model"))
        assert _collect_content(result) == "I cannot find this in the documents."
        assert _parse_finish_sources(result) == []

    @pytest.mark.asyncio
    async def test_case3_llm_no_tag_fallback_all(self):
        """Case 3: LLM omits tag entirely → fallback to all sources."""
        lines = [
            _make_chunk("Answer without any sources tag."),
            _make_finish(),
            DONE_LINE,
        ]
        result = await _collect(stream_with_source_filtering(_fake_stream(lines), self.SOURCES, "test-model"))
        assert _collect_content(result) == "Answer without any sources tag."
        assert _parse_finish_sources(result) == self.SOURCES
