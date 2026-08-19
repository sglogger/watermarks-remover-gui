#!/usr/bin/env python3
"""Smoke-test a running Watermarks Remover GUI end to end.

Uses only the standard library, so it runs anywhere Python does. Point it
somewhere else with GUI=..., and authenticate with TOKEN=... when the server
has GUI_AUTH_TOKEN set.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

GUI = os.environ.get("GUI", "http://127.0.0.1:8080").rstrip("/")
TOKEN = os.environ.get("TOKEN", "")
HERE = Path(__file__).resolve().parent

ZWSP = "​"
NBSP = " "


def heading(text: str) -> None:
    print(f"\n\033[1m{text}\033[0m")


def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}


def get(path: str):
    request = urllib.request.Request(GUI + path, headers=auth_headers())
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response)


def post_json(path: str, payload: dict):
    request = urllib.request.Request(
        GUI + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **auth_headers()},
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.load(response)


def post_files(path: str, files: list[tuple[str, bytes]], options: dict):
    """Build a multipart body by hand — no third-party dependency needed."""
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for name, data in files:
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="files"; filename="{name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n".encode()
            + data
            + b"\r\n"
        )
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="options"\r\n\r\n'
        f"{json.dumps(options)}\r\n".encode()
    )
    parts.append(f"--{boundary}--\r\n".encode())

    request = urllib.request.Request(
        GUI + path,
        data=b"".join(parts),
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            **auth_headers(),
        },
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.load(response)


def main() -> int:
    heading("1. Engine status")
    status = get("/api/status")
    engine, contract, release = status["engine"], status["contract"], status["release"]
    print(f"   engine    ok={engine['ok']} version={engine.get('version')} at {engine['url']}")
    print(f"   contract  ok={contract['ok']} batch={contract['batch_supported']}")
    for message in contract["messages"]:
        print(f"             note: {message}")
    print(f"   release   running {release['current']}, latest {release['latest']}"
          f"{' — UPDATE AVAILABLE' if release['outdated'] else ''}")

    heading("2. Scan pasted text — every hidden character is located")
    text = f"Hello{ZWSP} world.{NBSP}Second{ZWSP} sentence."
    item = post_json("/api/scan/text", {"text": text, "format": "text"})["items"][0]
    print(f"   suspicious={item['suspicious']}  scan id={item['id']}")
    spans = (item.get("highlight") or {}).get("spans", [])
    for span in spans:
        shown = repr(text[span["start"] : span["end"]])
        print(f"   offset {span['start']:>3}: {span['label']} {shown} -> {span['action']}")
    assert len(spans) == 3, f"expected 3 marked positions, got {len(spans)}"

    heading("3. Remove them, and verify the result is clean")
    cleaned = post_json("/api/clean", {"ids": [item["id"]], "options": {}})["items"][0]
    print(f"   verified={cleaned['verified']}  remaining={cleaned['remaining_hits']}")
    print(f"   result: {cleaned['text']!r}")
    assert ZWSP not in cleaned["text"] and NBSP not in cleaned["text"]
    assert cleaned["verified"] is True

    heading("4. Scan every example file")
    samples = sorted(
        p for p in HERE.glob("sample-*") if p.suffix in
        {".md", ".html", ".svg", ".txt", ".png", ".docx"}
    )
    result = post_files(
        "/api/scan/files", [(p.name, p.read_bytes()) for p in samples], {}
    )
    marked_ids = []
    for entry in result["items"]:
        if not entry["ok"]:
            print(f"   {entry['name']:<22} refused: {entry['error']}")
            continue
        highlight = entry.get("highlight") or {}
        chars = highlight.get("carrier_chars", 0)
        blocks = highlight.get("block_regions", 0)
        verdict = "watermarked" if entry["suspicious"] else "clean"
        detail = []
        if chars:
            detail.append(f"{chars} hidden character{'' if chars == 1 else 's'}")
        if blocks:
            detail.append(f"{blocks} marked block{'' if blocks == 1 else 's'}")
        extra = f" ({', '.join(detail)})" if detail else ""
        print(f"   {entry['name']:<22} {entry['kind']:<10} {verdict}{extra}")
        if entry["suspicious"]:
            marked_ids.append(entry["id"])
    for warning in result["warnings"]:
        print(f"   warning: {warning}")

    heading("5. Clean the watermarked files, and re-verify each one")
    if marked_ids:
        cleaned_files = post_json("/api/clean", {"ids": marked_ids, "options": {}})
        for entry in cleaned_files["items"]:
            state = "verified clean" if entry["verified"] else "STILL FLAGGED"
            print(f"   {entry['name']:<22} -> {entry['cleaned_name']}  ({state})")
            for leftover in entry.get("remaining_findings", []):
                print(f"       still flagged: {leftover}")
    else:
        print("   nothing to clean")

    heading("6. Audio is refused before it ever reaches the engine")
    mp3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 64
    refused = post_files("/api/scan/files", [("song.mp3", mp3), ("disguised.png", mp3)], {})
    for entry in refused["items"]:
        print(f"   {entry['name']:<22} {entry['error']}")
        assert entry["ok"] is False

    print("\n\033[1mAll checks passed.\033[0m")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as exc:
        print(f"\nHTTP {exc.code}: {exc.read().decode(errors='replace')[:400]}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"\nCannot reach {GUI}: {exc.reason}", file=sys.stderr)
        sys.exit(1)
