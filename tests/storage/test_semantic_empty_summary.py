# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

import pytest

from openviking.storage.queuefs.semantic_processor import (
    NO_SUBSTANTIVE_CONTENT_SUMMARY,
    SemanticProcessor,
    _is_effectively_empty_content,
)


def test_is_effectively_empty_content():
    assert _is_effectively_empty_content("")
    assert _is_effectively_empty_content("   \n\t")
    assert _is_effectively_empty_content("# Title Only")
    assert _is_effectively_empty_content("# Title\n\n## Subtitle")
    assert not _is_effectively_empty_content("hello")
    assert not _is_effectively_empty_content("# Title\n\nBody text")


@pytest.mark.asyncio
async def test_generate_text_summary_skips_llm_for_empty_content(monkeypatch):
    processor = SemanticProcessor()

    async def _fail_read(*args, **kwargs):
        raise AssertionError("read_file should not be called when content empty")

    async def _read_empty(uri, ctx=None):
        return ""

    monkeypatch.setattr(
        "openviking.storage.queuefs.semantic_processor.get_viking_fs",
        lambda: type("FS", (), {"read_file": _read_empty})(),
    )

    result = await processor._generate_text_summary(
        "viking://resources/empty/.abstract.md",
        ".abstract.md",
    )
    assert result["summary"] == ""
    assert result["has_substantive_content"] is False


@pytest.mark.asyncio
async def test_generate_overview_skips_llm_when_no_substantive_summaries():
    processor = SemanticProcessor()
    overview = await processor._generate_overview(
        "viking://resources/title-only",
        [{"name": "title-only.md", "summary": "", "has_substantive_content": False}],
        [],
    )
    assert NO_SUBSTANTIVE_CONTENT_SUMMARY in overview
