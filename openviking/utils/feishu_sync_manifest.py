# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Feishu watch resync manifest helpers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from openviking.server.identity import RequestContext
from openviking.storage.transaction import LockLease, NO_LOCK
from openviking.storage.viking_fs import LS_ALL_NODES, get_viking_fs
from openviking_cli.utils.logger import get_logger
from openviking_cli.utils.uri import VikingURI

logger = get_logger(__name__)

FEISHU_SYNC_MANIFEST_NAME = ".feishu_sync_manifest.json"
_MANIFEST_VERSION = 1
_RESERVED_TARGET_NAMES = frozenset(
    {
        FEISHU_SYNC_MANIFEST_NAME,
        ".abstract.md",
        ".overview.md",
        ".image_mappings.json",
    }
)


@dataclass
class ManifestFileEntry:
    path: str
    sha256: str
    kind: str = "content"

    def to_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "kind": self.kind}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ManifestFileEntry":
        return cls(
            path=str(data.get("path") or ""),
            sha256=str(data.get("sha256") or ""),
            kind=str(data.get("kind") or "content"),
        )


@dataclass
class FeishuSyncManifest:
    version: int = _MANIFEST_VERSION
    source_type: str = "feishu"
    source_url: str = ""
    doc_type: str = ""
    token: str = ""
    generated_at: str = ""
    files: List[ManifestFileEntry] = field(default_factory=list)
    dirs: List[str] = field(default_factory=list)

    def file_paths(self) -> Set[str]:
        return {entry.path for entry in self.files if entry.path}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "source_type": self.source_type,
            "source_url": self.source_url,
            "doc_type": self.doc_type,
            "token": self.token,
            "generated_at": self.generated_at,
            "files": [entry.to_dict() for entry in self.files],
            "dirs": list(self.dirs),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FeishuSyncManifest":
        files = [ManifestFileEntry.from_dict(item) for item in data.get("files") or []]
        return cls(
            version=int(data.get("version") or _MANIFEST_VERSION),
            source_type=str(data.get("source_type") or "feishu"),
            source_url=str(data.get("source_url") or ""),
            doc_type=str(data.get("doc_type") or ""),
            token=str(data.get("token") or ""),
            generated_at=str(data.get("generated_at") or ""),
            files=files,
            dirs=[str(item) for item in data.get("dirs") or []],
        )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def safe_relative_path(path: str) -> Optional[str]:
    normalized = path.replace("\\", "/").strip("/")
    if not normalized or normalized in {".", ".."}:
        return None
    parts = normalized.split("/")
    if any(part in {".", ".."} for part in parts):
        return None
    return normalized


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _read_file_bytes(uri: str, *, ctx: Optional[RequestContext]) -> bytes:
    viking_fs = get_viking_fs()
    content = await viking_fs.read_file(uri, ctx=ctx)
    if isinstance(content, str):
        return content.encode("utf-8")
    return content or b""


async def hash_file(uri: str, *, ctx: Optional[RequestContext]) -> str:
    return hash_bytes(await _read_file_bytes(uri, ctx=ctx))


async def _list_visible_tree(
    root_uri: str,
    *,
    ctx: Optional[RequestContext],
) -> Tuple[Dict[str, str], Set[str]]:
    """Return relative_path -> absolute uri for visible files, and relative dirs."""

    viking_fs = get_viking_fs()
    files: Dict[str, str] = {}
    dirs: Set[str] = set()

    async def walk(dir_uri: str, rel_prefix: str) -> None:
        try:
            entries = await viking_fs.ls(
                dir_uri,
                show_all_hidden=False,
                node_limit=LS_ALL_NODES,
                ctx=ctx,
            )
        except Exception as exc:
            logger.warning("[FeishuManifest] Failed to list %s: %s", dir_uri, exc)
            return

        for entry in entries:
            name = entry.get("name", "")
            if not name or name in {".", ".."}:
                continue
            rel_path = safe_relative_path(f"{rel_prefix}/{name}" if rel_prefix else name)
            if rel_path is None:
                continue
            item_uri = VikingURI(dir_uri).join(name).uri
            if entry.get("isDir", False):
                dirs.add(rel_path)
                await walk(item_uri, rel_path)
            else:
                files[rel_path] = item_uri

    await walk(root_uri.rstrip("/"), "")
    return files, dirs


async def scan_manifest_from_uri(
    root_uri: str,
    *,
    source_url: str = "",
    doc_type: str = "",
    token: str = "",
    ctx: Optional[RequestContext] = None,
) -> FeishuSyncManifest:
    files_map, dirs = await _list_visible_tree(root_uri, ctx=ctx)
    entries: List[ManifestFileEntry] = []
    for rel_path, file_uri in sorted(files_map.items()):
        if rel_path in _RESERVED_TARGET_NAMES:
            continue
        kind = "image" if rel_path.startswith("images/") else "content"
        entries.append(
            ManifestFileEntry(
                path=rel_path,
                sha256=await hash_file(file_uri, ctx=ctx),
                kind=kind,
            )
        )
    return FeishuSyncManifest(
        source_url=source_url,
        doc_type=doc_type,
        token=token,
        generated_at=_utc_now_iso(),
        files=entries,
        dirs=sorted(dirs),
    )


async def load_manifest(
    target_uri: str,
    *,
    ctx: Optional[RequestContext] = None,
) -> Optional[FeishuSyncManifest]:
    viking_fs = get_viking_fs()
    manifest_uri = VikingURI(target_uri.rstrip("/")).join(FEISHU_SYNC_MANIFEST_NAME).uri
    if not await viking_fs.exists(manifest_uri, ctx=ctx):
        return None
    try:
        raw = await viking_fs.read_file(manifest_uri, ctx=ctx)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw or "{}")
        return FeishuSyncManifest.from_dict(data)
    except Exception as exc:
        logger.warning("[FeishuManifest] Failed to load manifest at %s: %s", manifest_uri, exc)
        return None


async def write_manifest_to_uri(
    target_uri: str,
    manifest: FeishuSyncManifest,
    *,
    ctx: Optional[RequestContext] = None,
    lock_handle=None,
) -> None:
    viking_fs = get_viking_fs()
    manifest_uri = VikingURI(target_uri.rstrip("/")).join(FEISHU_SYNC_MANIFEST_NAME).uri
    payload = json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2)
    await viking_fs.write_file(
        manifest_uri,
        payload,
        ctx=ctx,
        lock_handle=lock_handle,
    )


async def sync_with_feishu_manifest(
    root_uri: str,
    target_uri: str,
    *,
    ctx: Optional[RequestContext] = None,
    lock: LockLease = NO_LOCK,
    source_url: str = "",
    doc_type: str = "",
    token: str = "",
) -> "DiffResult":
    """Merge Feishu temp tree into target without deleting unmanaged user files."""

    from openviking.storage.queuefs.semantic_processor import DiffResult

    viking_fs = get_viking_fs()
    diff = DiffResult()
    lock_handle = lock.handle

    new_manifest = await scan_manifest_from_uri(
        root_uri,
        source_url=source_url,
        doc_type=doc_type,
        token=token,
        ctx=ctx,
    )
    old_manifest = await load_manifest(target_uri, ctx=ctx)
    merge_only = old_manifest is None

    new_files, new_dirs = await _list_visible_tree(root_uri, ctx=ctx)
    target_files, target_dirs = await _list_visible_tree(target_uri, ctx=ctx)

    async def target_file_uri(rel_path: str) -> str:
        return VikingURI(target_uri.rstrip("/")).join(rel_path).uri

    for rel_path, src_uri in sorted(new_files.items()):
        if rel_path in _RESERVED_TARGET_NAMES:
            continue
        dst_uri = await target_file_uri(rel_path)
        parent_uri = VikingURI(dst_uri).parent
        if parent_uri:
            await viking_fs.mkdir(parent_uri.uri, exist_ok=True, ctx=ctx)
        if rel_path in target_files:
            changed = await hash_file(src_uri, ctx=ctx) != await hash_file(
                target_files[rel_path], ctx=ctx
            )
            if changed:
                diff.updated_files.append(dst_uri)
                await viking_fs.rm(dst_uri, ctx=ctx, lock_handle=lock_handle)
                await viking_fs.mv(src_uri, dst_uri, ctx=ctx, lock_handle=lock_handle)
        else:
            diff.added_files.append(dst_uri)
            await viking_fs.mv(src_uri, dst_uri, ctx=ctx, lock_handle=lock_handle)

    if not merge_only and old_manifest is not None:
        old_paths = {entry.path: entry for entry in old_manifest.files if entry.path}
        new_paths = new_manifest.file_paths()
        for rel_path, entry in old_paths.items():
            if rel_path in new_paths or rel_path in _RESERVED_TARGET_NAMES:
                continue
            if rel_path not in target_files:
                continue
            target_uri_for_file = target_files[rel_path]
            try:
                current_hash = await hash_file(target_uri_for_file, ctx=ctx)
            except Exception:
                continue
            if current_hash != entry.sha256:
                logger.info(
                    "[FeishuManifest] Preserving user-modified file %s (hash changed)",
                    rel_path,
                )
                continue
            diff.deleted_files.append(target_uri_for_file)
            await viking_fs.rm(target_uri_for_file, ctx=ctx, lock_handle=lock_handle)

        for rel_dir in sorted(old_manifest.dirs, reverse=True):
            if rel_dir in new_dirs or rel_dir not in target_dirs:
                continue
            dir_uri = target_dirs[rel_dir]
            try:
                entries = await viking_fs.ls(
                    dir_uri,
                    show_all_hidden=False,
                    node_limit=LS_ALL_NODES,
                    ctx=ctx,
                )
            except Exception:
                continue
            if entries:
                continue
            diff.deleted_dirs.append(dir_uri)
            await viking_fs.rm(dir_uri, recursive=True, ctx=ctx, lock_handle=lock_handle)

    try:
        await viking_fs.delete_temp(root_uri, ctx=ctx)
    except Exception as exc:
        logger.error("[FeishuManifest] Failed to delete temp root %s: %s", root_uri, exc)

    await write_manifest_to_uri(
        target_uri,
        new_manifest,
        ctx=ctx,
        lock_handle=lock_handle,
    )
    return diff
