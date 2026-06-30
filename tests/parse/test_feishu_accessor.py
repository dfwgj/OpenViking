# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Tests for FeishuAccessor user token and image handling."""

import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

from openviking.parse.accessors.feishu_accessor import FeishuAccessor


class _SuccessResponse:
    def __init__(self, data):
        self.data = data
        self.code = 0
        self.msg = ""

    @staticmethod
    def success():
        return True


class _FakeRequestOption:
    def __init__(self):
        self.user_access_token = None

    @staticmethod
    def builder():
        return _FakeRequestOptionBuilder()


class _FakeRequestOptionBuilder:
    def __init__(self):
        self._option = _FakeRequestOption()

    def user_access_token(self, token):
        self._option.user_access_token = token
        return self

    def build(self):
        return self._option


class _FakeBaseRequest:
    @staticmethod
    def builder():
        return _FakeBaseRequestBuilder()


class _FakeBaseRequestBuilder:
    def __init__(self):
        self._request = SimpleNamespace(http_method=None, uri=None, token_types=None)

    def http_method(self, method):
        self._request.http_method = method
        return self

    def uri(self, uri):
        self._request.uri = uri
        return self

    def token_types(self, token_types):
        self._request.token_types = token_types
        return self

    def build(self):
        return self._request


class _FakeRawResponse:
    def __init__(self, content=b"image-bytes", status_code=200):
        self.content = content
        self.status_code = status_code


class _FakeMediaResponse:
    def __init__(self, content=b"image-bytes", success=True, code=0, msg=""):
        self.raw = _FakeRawResponse(content)
        self.code = code
        self.msg = msg
        self._success = success

    def success(self):
        return self._success


class _FakeListDocumentBlockRequest:
    @staticmethod
    def builder():
        return _FakeListDocumentBlockRequestBuilder()


class _FakeListDocumentBlockRequestBuilder:
    def __init__(self):
        self._request = SimpleNamespace(document_id=None)

    def document_id(self, document_id):
        self._request.document_id = document_id
        return self

    def page_size(self, _page_size):
        return self

    def document_revision_id(self, _revision_id):
        return self

    def build(self):
        return self._request


def _install_fake_lark_modules(monkeypatch):
    lark = ModuleType("lark_oapi")
    lark.BaseRequest = _FakeBaseRequest
    lark.HttpMethod = SimpleNamespace(GET="GET")
    lark.AccessTokenType = SimpleNamespace(TENANT="tenant")
    docx_v1 = ModuleType("lark_oapi.api.docx.v1")
    docx_v1.ListDocumentBlockRequest = _FakeListDocumentBlockRequest
    core_model = ModuleType("lark_oapi.core.model")
    core_model.RequestOption = _FakeRequestOption
    monkeypatch.setitem(sys.modules, "lark_oapi", lark)
    monkeypatch.setitem(sys.modules, "lark_oapi.api.docx.v1", docx_v1)
    monkeypatch.setitem(sys.modules, "lark_oapi.core.model", core_model)


def test_fetch_all_blocks_uses_user_access_token_option(monkeypatch):
    _install_fake_lark_modules(monkeypatch)
    list_blocks = MagicMock(
        return_value=_SuccessResponse(
            SimpleNamespace(items=[], has_more=False, page_token=None),
        )
    )
    accessor = FeishuAccessor()
    accessor._user_token_client = SimpleNamespace(
        docx=SimpleNamespace(v1=SimpleNamespace(document_block=SimpleNamespace(list=list_blocks)))
    )

    blocks = accessor._fetch_all_blocks("doc_token", feishu_access_token="u-test")

    assert blocks == []
    request, option = list_blocks.call_args.args
    assert request.document_id == "doc_token"
    assert option.user_access_token == "u-test"


def test_resolve_image_refs_respects_download_images_disabled():
    accessor = FeishuAccessor()
    accessor._config = SimpleNamespace(download_images=False)
    markdown = "![screenshot](feishu://image/img_token_123)"

    updated, images = accessor._resolve_image_refs(markdown)

    assert updated == markdown
    assert images == {}


def test_resolve_image_refs_downloads_media_and_rewrites_markdown(monkeypatch):
    _install_fake_lark_modules(monkeypatch)
    request_media = MagicMock(return_value=_FakeMediaResponse(b"\x89PNG\r\n"))
    accessor = FeishuAccessor()
    accessor._config = SimpleNamespace(download_images=True)
    accessor._client = SimpleNamespace(request=request_media)

    updated, images = accessor._resolve_image_refs(
        "before ![screenshot](feishu://image/img_token_123) after"
    )

    assert updated == "before ![screenshot](images/img_token_123.png) after"
    assert images == {"images/img_token_123.png": b"\x89PNG\r\n"}
    request = request_media.call_args.args[0]
    assert request.http_method == "GET"
    assert request.uri == "/open-apis/drive/v1/medias/img_token_123/download"


def test_access_writes_downloaded_images_next_to_markdown(monkeypatch):
    accessor = FeishuAccessor()
    accessor._config = SimpleNamespace(download_images=True)

    async def fake_fetch_document(*_args, **_kwargs):
        from openviking.parse.accessors.feishu_accessor import FeishuDocument

        return FeishuDocument(
            doc_type="docx",
            token="doc_token",
            markdown_content="![screenshot](feishu://image/img_token_123)",
            title="Test Doc",
            meta={},
        )

    monkeypatch.setattr(accessor, "_fetch_document", fake_fetch_document)
    monkeypatch.setattr(
        accessor,
        "_resolve_image_refs",
        lambda markdown, **_: (
            "![screenshot](images/img_token_123.png)",
            {"images/img_token_123.png": b"\x89PNG\r\n"},
        ),
    )

    resource = asyncio.run(accessor.access("https://example.feishu.cn/docx/doc_token"))

    try:
        assert resource.path.name == "document.md"
        assert resource.path.read_text(encoding="utf-8") == (
            "![screenshot](images/img_token_123.png)"
        )
        image_path = resource.path.parent / "images" / "img_token_123.png"
        assert image_path.read_bytes() == b"\x89PNG\r\n"
        assert resource.meta["original_filename"] == "Test Doc"
    finally:
        resource.cleanup()

    assert not resource.path.parent.exists()
