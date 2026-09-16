#!/usr/bin/env python3
"""Fetch/verify the frozen public-PDF quality sources, never call a model.

Only sources.json is read. Gold, questions, API credentials and production state
are deliberately outside this program's inputs. All downloaded/derived content
must stay outside the repository. Requires pypdf; Poppler is optional.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

MAX_BYTES = 20 * 1024 * 1024
HOSTS = {"static.cninfo.com.cn"}
ROOT = Path(__file__).resolve().parents[1]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def require_private(path: Path) -> Path:
    absolute = path.absolute()
    for part in (absolute, *absolute.parents):
        if part.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(part)):
            raise ValueError(f"symlink/junction is not allowed: {part}")
    resolved = absolute.resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise ValueError("destination and report must be outside the public repository")
    return resolved


def check_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in HOSTS
            or parsed.port not in (None, 443) or parsed.username or parsed.password):
        raise ValueError("source/redirect must use an allowlisted HTTPS host")
    return url


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str) -> bytes:
    request = urllib.request.Request(check_url(url), headers={"User-Agent": "CrackRAG-reproducibility/1.0"})
    with urllib.request.build_opener(SafeRedirect()).open(request, timeout=30) as response:
        check_url(response.geturl())
        length = response.headers.get("Content-Length")
        if length and int(length) > MAX_BYTES:
            raise ValueError("PDF exceeds 20 MiB")
        chunks, total = [], 0
        while True:
            chunk = response.read(min(65536, MAX_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_BYTES:
                raise ValueError("PDF exceeds 20 MiB")
        return b"".join(chunks)


def write_new_or_identical(path: Path, data: bytes) -> None:
    require_private(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f"refusing to overwrite differing file: {path}")
        return
    # Exclusive creation avoids replacing an artifact from another run.
    with path.open("xb") as handle:
        handle.write(data)


def page_content_hash(page) -> str:
    contents = page.get_contents()
    return digest(contents.get_data() if contents is not None else b"")


def run(args) -> dict:
    import pypdf
    destination = require_private(args.destination)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    sources = manifest["sources"]
    if len(sources) != 2:
        raise ValueError("expected the two frozen sources")
    result = {"status": "PREPARED_NOT_EXECUTED", "no_api_calls": True,
              "model_api_calls": 0, "manifest_sha256": digest(args.manifest.read_bytes()),
              "pypdf_version": pypdf.__version__, "sources": []}
    for source in sources:
        source_id = source["id"]
        if not re.fullmatch(r"[a-z0-9-]+", source_id):
            raise ValueError("invalid source ID")
        if not 0 < source["bytes"] <= MAX_BYTES:
            raise ValueError("invalid declared byte limit")
        original = require_private(destination / "sources" / f"{source_id}.pdf")
        if original.exists():
            if original.stat().st_size != source["bytes"]:
                raise ValueError(f"existing PDF size mismatch: {source_id}")
            data = original.read_bytes()
        elif args.verify_existing:
            raise ValueError(f"missing PDF in offline mode: {source_id}")
        else:
            data = fetch(source["url"])
        if not data.startswith(b"%PDF-") or len(data) != source["bytes"] or digest(data) != source["sha256"]:
            raise ValueError(f"original PDF byte/hash/signature mismatch: {source_id}")
        reader = pypdf.PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted or len(reader.pages) != source["page_count"]:
            raise ValueError(f"PDF page count/encryption mismatch: {source_id}")
        write_new_or_identical(original, data)
        source_result = {"id": source_id, "sha256": digest(data), "bytes": len(data),
                         "page_count": len(reader.pages), "scopes": []}
        for scope in source["scopes"]:
            scope_id, pages = scope["id"], scope["physical_pages"]
            if (not re.fullmatch(r"[a-z0-9-]+", scope_id) or not 1 <= len(pages) <= 32
                    or len(set(pages)) != len(pages)
                    or any(type(page) is not int or not 1 <= page <= len(reader.pages) for page in pages)):
                raise ValueError("invalid selected page scope")
            writer = pypdf.PdfWriter()
            for page in pages:
                writer.add_page(reader.pages[page - 1])
            # No header/table reconstruction, cropping, OCR or gold injection.
            buffer = io.BytesIO()
            writer.write(buffer)
            selected_data = buffer.getvalue()
            selected = require_private(destination / "selected" / f"{scope_id}.pdf")
            write_new_or_identical(selected, selected_data)
            selected_reader = pypdf.PdfReader(io.BytesIO(selected_data), strict=True)
            mappings = []
            for index, original_page in enumerate(pages):
                source_page = reader.pages[original_page - 1]
                selected_page = selected_reader.pages[index]
                if (page_content_hash(source_page) != page_content_hash(selected_page)
                        or source_page.extract_text() != selected_page.extract_text()
                        or source_page.mediabox != selected_page.mediabox):
                    raise ValueError("selected page content changed")
                mappings.append({"selected_physical_page": index + 1,
                                 "original_physical_page": original_page,
                                 "content_stream_sha256": page_content_hash(source_page)})
            item = {"id": scope_id, "relative_path": f"selected/{scope_id}.pdf",
                    "sha256": digest(selected_data), "bytes": len(selected_data), "page_map": mappings}
            if args.render:
                renderer = args.pdftoppm or shutil.which("pdftoppm")
                if not renderer:
                    raise ValueError("--render requires pdftoppm on PATH or --pdftoppm")
                # Render to a unique temp directory, then install without overwrite.
                destination.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix="render-", dir=destination) as temporary:
                    prefix = Path(temporary) / scope_id
                    subprocess.run([str(renderer), "-png", "-r", "140", str(selected), str(prefix)],
                                   check=True, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                    item["renders"] = []
                    images = sorted(Path(temporary).glob("*.png"))
                    if len(images) != len(pages):
                        raise ValueError("rendered page count mismatch")
                    for png in images:
                        image_data = png.read_bytes()
                        output = destination / "selected-renders" / png.name
                        write_new_or_identical(output, image_data)
                        item["renders"].append({"relative_path": f"selected-renders/{png.name}",
                                                "sha256": digest(image_data)})
            source_result["scopes"].append(item)
        result["sources"].append(source_result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "eval/release/quality/sources.json")
    parser.add_argument("--destination", type=Path, required=True, help="private directory outside the repository")
    parser.add_argument("--verify-existing", action="store_true", help="disable all network downloads")
    parser.add_argument("--render", action="store_true", help="render selected original pages for manual review")
    parser.add_argument("--pdftoppm", type=Path)
    parser.add_argument("--report", type=Path, help="optional private JSON report; differing existing file is rejected")
    args = parser.parse_args()
    try:
        result = run(args)
        encoded = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if args.report:
            write_new_or_identical(require_private(args.report), encoded)
        print(encoded.decode("utf-8"), end="")
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"preparation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
