# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Tests for FeishuMarkdownConverter."""

from types import SimpleNamespace

import pytest

from openviking.parse.feishu.converter import FeishuConvertOptions, FeishuMarkdownConverter


def _make_block(**kwargs):
    defaults = {
        "block_id": "block_1",
        "block_type": 0,
        "parent_id": "parent",
        "children": None,
        "page": None,
        "text": None,
        "reference_synced": None,
        "bitable": None,
        "sheet": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TestFeishuMarkdownConverterBlocks:
    def setup_method(self):
        self.converter = FeishuMarkdownConverter()

    def test_synced_block_emits_placeholder_and_warning(self):
        block = _make_block(block_type=0, reference_synced=SimpleNamespace())
        result = self.converter.block_to_markdown(
            block,
            {},
            {},
            options=FeishuConvertOptions(include_placeholders=True),
        )
        assert result is not None
        assert "preserved as placeholder" in result
        assert "reference_synced" in result
        assert self.converter._active_warnings
        self.converter._active_warnings = []
        block2 = _make_block(block_type=18, bitable=SimpleNamespace())
        result2 = self.converter.block_to_markdown(
            block2,
            {},
            {},
            options=FeishuConvertOptions(include_placeholders=True),
        )
        assert result2 is not None
        assert "bitable" in result2

    def test_parse_feishu_url_supports_sheets_and_base(self):
        doc_type, token = FeishuMarkdownConverter.parse_feishu_url(
            "https://example.feishu.cn/sheets/shtcn123"
        )
        assert doc_type == "sheets"
        assert token == "shtcn123"
        doc_type, token = FeishuMarkdownConverter.parse_feishu_url(
            "https://example.feishu.cn/base/bascn999"
        )
        assert doc_type == "base"
        assert token == "bascn999"

    def test_unsupported_doc_type_raises(self):
        converter = FeishuMarkdownConverter()
        with pytest.raises(ValueError, match="Unsupported Feishu document type"):
            converter.convert_url("https://example.feishu.cn/mindnote/abc123")

    def test_sub_page_list_uses_wiki_child_links_when_context_available(self, monkeypatch):
        converter = FeishuMarkdownConverter()
        converter._active_wiki_context = {
            "space_id": "space1",
            "node_token": "node1",
            "host": "example.feishu.cn",
        }
        monkeypatch.setattr(
            converter,
            "_list_wiki_child_nodes",
            lambda *_args, **_kwargs: [
                SimpleNamespace(title="Child Page", node_token="child1", obj_type="docx")
            ],
        )
        block = _make_block(block_type=0, sub_page_list=SimpleNamespace())

        result = converter.block_to_markdown(
            block,
            {},
            {},
            options=FeishuConvertOptions(include_placeholders=True),
        )

        assert result == "> Child pages:\n> - [Child Page](https://example.feishu.cn/wiki/child1) (docx)"

    def test_sub_page_list_without_wiki_context_uses_placeholder(self):
        converter = FeishuMarkdownConverter()
        block = _make_block(block_type=0, sub_page_list=SimpleNamespace())

        result = converter.block_to_markdown(
            block,
            {},
            {},
            options=FeishuConvertOptions(include_placeholders=True),
        )

        assert result is not None
        assert "sub_page_list" in result

    def test_trim_empty_table_removes_blank_rows_and_columns(self):
        rows = [
            ["Module", "Status", "", ""],
            ["docx import", "Done", "", ""],
            ["", "", "", ""],
            ["", "", "", ""],
        ]
        assert FeishuMarkdownConverter.trim_empty_table(rows) == [
            ["Module", "Status"],
            ["docx import", "Done"],
        ]

    def test_bitable_uses_app_title_when_available(self, monkeypatch):
        converter = FeishuMarkdownConverter()

        class _FakeListResponse:
            def success(self):
                return True

            class data:
                items = [SimpleNamespace(table_id="tbl1", name="Tasks")]
                has_more = False
                page_token = None

        class _FakeFieldResponse:
            def success(self):
                return True

            class data:
                items = []

        class _FakeRecordResponse:
            def success(self):
                return True

            class data:
                items = []
                has_more = False
                page_token = None

        class _FakeClient:
            class bitable:
                class v1:
                    class app_table:
                        @staticmethod
                        def list(*_args, **_kwargs):
                            return _FakeListResponse()

                    class app_table_field:
                        @staticmethod
                        def list(*_args, **_kwargs):
                            return _FakeFieldResponse()

                    class app_table_record:
                        @staticmethod
                        def list(*_args, **_kwargs):
                            return _FakeRecordResponse()

        monkeypatch.setattr(converter, "_get_client", lambda **_: _FakeClient())
        monkeypatch.setattr(
            converter,
            "_fetch_bitable_app_title",
            lambda *_args, **_kwargs: "Project Tracker Base",
        )

        markdown, title, _warnings = converter._convert_bitable(
            "app_token",
            options=FeishuConvertOptions(),
        )
        assert title == "Project Tracker Base"
        assert markdown.startswith("# Project Tracker Base")

    def test_bitable_title_fallback_emits_warning(self, monkeypatch):
        converter = FeishuMarkdownConverter()

        class _FakeListResponse:
            def success(self):
                return True

            class data:
                items = [SimpleNamespace(table_id="tbl1", name="Tasks")]
                has_more = False
                page_token = None

        class _FakeFieldResponse:
            def success(self):
                return True

            class data:
                items = []

        class _FakeRecordResponse:
            def success(self):
                return True

            class data:
                items = []
                has_more = False
                page_token = None

        class _FakeClient:
            class bitable:
                class v1:
                    class app_table:
                        @staticmethod
                        def list(*_args, **_kwargs):
                            return _FakeListResponse()

                    class app_table_field:
                        @staticmethod
                        def list(*_args, **_kwargs):
                            return _FakeFieldResponse()

                    class app_table_record:
                        @staticmethod
                        def list(*_args, **_kwargs):
                            return _FakeRecordResponse()

        monkeypatch.setattr(converter, "_get_client", lambda **_: _FakeClient())
        monkeypatch.setattr(converter, "_fetch_bitable_app_title", lambda *_args, **_kwargs: None)

        _markdown, title, warnings = converter._convert_bitable(
            "app_token",
            options=FeishuConvertOptions(),
        )
        assert title == "Bitable (1 tables)"
        assert any("fallback title" in warning for warning in warnings)

    def test_bitable_view_name_is_added_to_title(self, monkeypatch):
        converter = FeishuMarkdownConverter()

        class _FakeListResponse:
            def success(self):
                return True

            class data:
                items = [SimpleNamespace(table_id="tbl1", name="Tasks")]
                has_more = False
                page_token = None

        class _FakeFieldResponse:
            def success(self):
                return True

            class data:
                items = []

        class _FakeRecordResponse:
            def success(self):
                return True

            class data:
                items = []
                has_more = False
                page_token = None

        class _FakeClient:
            class bitable:
                class v1:
                    class app_table:
                        @staticmethod
                        def list(*_args, **_kwargs):
                            return _FakeListResponse()

                    class app_table_field:
                        @staticmethod
                        def list(*_args, **_kwargs):
                            return _FakeFieldResponse()

                    class app_table_record:
                        @staticmethod
                        def list(*_args, **_kwargs):
                            return _FakeRecordResponse()

        monkeypatch.setattr(converter, "_get_client", lambda **_: _FakeClient())
        monkeypatch.setattr(converter, "_fetch_bitable_app_title", lambda *_args, **_kwargs: "Project Tracker Base")
        monkeypatch.setattr(converter, "_fetch_bitable_view_name", lambda *_args, **_kwargs: "Grid View")

        markdown, title, _warnings = converter._convert_bitable(
            "app_token",
            options=FeishuConvertOptions(),
            table_id="tbl1",
            view_id="vew1",
        )
        assert title == "Project Tracker Base - Grid View"
        assert markdown.startswith("# Project Tracker Base - Grid View")
