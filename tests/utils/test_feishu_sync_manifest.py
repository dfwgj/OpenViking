# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

import pytest

from openviking.utils.feishu_sync_manifest import (
    FEISHU_SYNC_MANIFEST_NAME,
    FeishuSyncManifest,
    ManifestFileEntry,
    safe_relative_path,
)
from openviking.storage.queuefs.semantic_msg import SemanticMsg


def test_safe_relative_path_rejects_traversal():
    assert safe_relative_path("../secret") is None
    assert safe_relative_path("images/a.png") == "images/a.png"


def test_manifest_roundtrip():
    manifest = FeishuSyncManifest(
        source_url="https://example.feishu.cn/docx/abc",
        doc_type="docx",
        token="abc",
        generated_at="2026-07-05T00:00:00+00:00",
        files=[ManifestFileEntry(path="document.md", sha256="deadbeef", kind="content")],
        dirs=["images"],
    )
    restored = FeishuSyncManifest.from_dict(manifest.to_dict())
    assert restored.source_url == manifest.source_url
    assert restored.files[0].path == "document.md"
    assert restored.dirs == ["images"]


def test_manifest_name_constant():
    assert FEISHU_SYNC_MANIFEST_NAME.startswith(".")


def test_semantic_msg_preserves_feishu_manifest_fields_roundtrip():
    msg = SemanticMsg(
        uri="viking://temp/doc",
        target_uri="viking://resources/doc",
        context_type="resource",
        sync_mode="feishu_manifest",
        source_url="https://example.feishu.cn/docx/abc",
        feishu_doc_type="docx",
        feishu_token="abc",
    )

    restored = SemanticMsg.from_dict(msg.to_dict())

    assert restored.sync_mode == "feishu_manifest"
    assert restored.source_url == "https://example.feishu.cn/docx/abc"
    assert restored.feishu_doc_type == "docx"
    assert restored.feishu_token == "abc"
