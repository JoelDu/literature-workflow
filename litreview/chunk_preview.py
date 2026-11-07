"""Offline comparison of legacy and structured chunks; never touches indexes."""
import argparse
import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from .chunker import split_markdown
from .structured_chunker import STRATEGY_VERSION, StructureChunkError


def _summary(chunks):
    return {
        "chunks": len(chunks),
        "max_chars": max((len(c.content) for c in chunks), default=0),
        "total_chars": sum(len(c.content) for c in chunks),
        "kinds": dict(Counter(getattr(c, "kind", "legacy") for c in chunks)),
        "decimal_newlines": sum(len(re.findall(r"\d\.\n\d", c.content)) for c in chunks),
        # Heuristics only: these do not prove original formulas are valid.
        "odd_display_delimiters": sum(c.content.count("$$") % 2 for c in chunks),
        "table_fragments_without_opening": sum(
            bool(re.search(r"<(?:td|tr)\b", c.content, re.I))
            and not bool(re.search(r"<table\b", c.content, re.I)) for c in chunks
        ),
    }


def preview(md: str, *, chunk_size=1000, overlap=150, max_chars=5000, table_chunk_size=2500):
    source_hash = hashlib.sha256(md.encode("utf-8")).hexdigest()
    config = {"strategy": STRATEGY_VERSION, "chunk_size": chunk_size,
              "overlap": overlap, "max_chars": max_chars, "table_chunk_size": table_chunk_size}
    fingerprint = hashlib.sha256(json.dumps(
        {"source_sha256": source_hash, **config}, sort_keys=True
    ).encode("utf-8")).hexdigest()
    legacy = split_markdown(source_hash, md, chunk_size, overlap)
    report = {"source_sha256": source_hash, "config": config,
              "preview_fingerprint": fingerprint,
              "offset_basis": "clean_markdown(input)", "legacy": _summary(legacy)}
    try:
        chunks = split_markdown(source_hash, md, chunk_size, overlap,
                                strategy="structured", max_chars=max_chars, table_chunk_size=table_chunk_size)
    except StructureChunkError as exc:
        report.update(status="needs_source_review", error=str(exc), structured=None, chunks=[])
    else:
        report.update(status="ok", structured=_summary(chunks), chunks=[asdict(c) for c in chunks])
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Existing MinerU Markdown file")
    parser.add_argument("--chunk-size", type=int, default=1000)
    parser.add_argument("--overlap", type=int, default=150)
    parser.add_argument("--max-chars", type=int, default=5000)
    parser.add_argument("--table-chunk-size", type=int, default=2500)
    parser.add_argument("--output", type=Path, help="Save full JSON to a NEW file; otherwise print summary")
    args = parser.parse_args(argv)
    try:
        report = preview(args.input.read_text(encoding="utf-8"), chunk_size=args.chunk_size,
                         overlap=args.overlap, max_chars=args.max_chars, table_chunk_size=args.table_chunk_size)
        if args.output:
            # Avoid clobbering existing experiment artifacts.
            with args.output.open("x", encoding="utf-8") as output:
                json.dump(report, output, ensure_ascii=False, indent=2)
        print(json.dumps({k: v for k, v in report.items() if k != "chunks"}, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "ok" else 2
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Preview failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
