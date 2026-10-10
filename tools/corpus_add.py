"""Add a document to the knowledge corpus, or check the source register (T33; AGT-10, PR-14).

    python tools/corpus_add.py --file ~/Downloads/article.html --url "https://doi.org/<the article's DOI>" \\
        --licence "CC BY 4.0" --title "The MetroPT dataset for predictive maintenance" \\
        --author "Veloso et al." [--accessed 2026-10-12] [--doc-id veloso-2022] [--kind external]
    python tools/corpus_add.py --verify            # exit 1 if the register has any problem

Run it on a machine with internet access, after downloading the document
yourself and reading its licence. The file goes to corpus/docs/ (corpus/injection/
for --kind injection_test) as <doc_id>.md or <doc_id>.txt:

* Markdown and plain text are copied byte for byte;
* HTML is sanitised first (pdm_common.corpus.sanitize: comments, scripts and
  styles removed, tags stripped) and stored as plain text, and the SHA-256 of
  the file you downloaded is kept as source_sha256;
* anything else is refused: convert a PDF to text first (pdftotext, for
  example), read the result, and add the text file.

Then an entry with the stored file's SHA-256 is appended to corpus/register.yaml
and the whole register is verified; if the check fails, the copy and the entry
are both undone. Only licences listed under `licences:` in the register are
accepted; add one there only when it lets the team store and quote the document.
--accessed defaults to today (UTC). --author is required for external documents,
since a CC BY licence needs the attribution.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from pdm_common.corpus import (DOC_ID, ISO_DATE, KINDS, REGISTER_FILE, accepted_licences,  # noqa: E402
                               load_register, passages, sanitize, verify_register)

TEXT_SUFFIXES = {".md": ".md", ".markdown": ".md", ".txt": ".txt"}
HTML_SUFFIXES = {".html", ".htm", ".xhtml"}
TEAM = "PdM-Agent team"


class AddError(RuntimeError):
    pass


def slug(title: str) -> str:
    """A doc_id from a title: lowercase words joined by '-', at most 48 characters."""
    words = re.findall(r"[a-z0-9]+", title.lower())
    out = ""
    for w in words:
        if len(out) + len(w) + (1 if out else 0) > 48:
            break
        out = f"{out}-{w}" if out else w
    return out or "document"


def entry_block(entry: dict) -> str:
    """The register entry as YAML lines in the register's own layout. Every value is JSON-quoted, which YAML
    reads as a plain string, so an id such as ``no`` or ``010`` cannot turn into a boolean or a number."""
    lines = [f"  - doc_id: {json.dumps(entry['doc_id'])}"]
    for key in ("title", "path", "url", "accessed", "licence", "sha256", "kind", "author", "source_sha256"):
        if entry.get(key):
            lines.append(f"    {key}: {json.dumps(str(entry[key]), ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


def prepare(src: Path) -> tuple[bytes, str, str | None]:
    """(bytes to store, file extension, SHA-256 of the source when it was converted)."""
    raw = src.read_bytes()
    suffix = src.suffix.lower()
    if suffix in HTML_SUFFIXES:
        text = sanitize(raw.decode("utf-8", errors="replace"))
        if not text:
            raise AddError(f"{src}: no readable text left after sanitising")
        return (text + "\n").encode("utf-8"), ".txt", hashlib.sha256(raw).hexdigest()
    if suffix in TEXT_SUFFIXES:
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AddError(f"{src}: not UTF-8 text ({exc}); convert it first") from exc
        return raw, TEXT_SUFFIXES[suffix], None
    raise AddError(f"{src}: only Markdown, plain text or HTML can be added; convert a {suffix or 'suffix-less'} "
                   "file to text first and check that the text reads properly")


def add(corpus: Path, file: str, url: str | None, licence: str, title: str, accessed: str | None = None,
        kind: str = "external", author: str | None = None, doc_id: str | None = None) -> dict:
    """Copy (or convert) the file into the corpus, append its register entry and verify. Returns the entry."""
    problems = verify_register(corpus)
    if problems:
        raise AddError("the register has problems; fix them first:\n  " + "\n  ".join(problems))
    src = Path(file)
    if not src.is_file():
        raise AddError(f"{src}: no such file")
    if kind not in KINDS:
        raise AddError(f"--kind must be one of {', '.join(KINDS)}")
    licences = accepted_licences(corpus)
    if licence not in licences:
        raise AddError(f"licence {licence!r} is not accepted; the register accepts {', '.join(licences)}. Add a "
                       "licence there only if it lets the team store and quote the document.")
    accessed = accessed or dt.datetime.now(dt.timezone.utc).date().isoformat()
    try:
        if not ISO_DATE.match(accessed):
            raise ValueError(accessed)
        dt.date.fromisoformat(accessed)
    except ValueError as exc:
        raise AddError(f"--accessed {accessed!r} is not an ISO date (YYYY-MM-DD)") from exc
    doc_id = doc_id or slug(title)
    if not DOC_ID.match(doc_id):
        raise AddError(f"doc_id {doc_id!r}: use lowercase letters, digits, '.', '_' or '-' (at most 64)")
    if any(e.get("doc_id") == doc_id for e in load_register(corpus)):
        raise AddError(f"doc_id {doc_id!r} is already in the register; pass another with --doc-id")
    if kind == "external":
        if not url or not url.startswith(("https://", "http://", "doi:")):
            raise AddError("an external document needs --url with the https:// (or doi:) address it came from")
        if not author:
            raise AddError("an external document needs --author: CC BY requires the attribution")
    data, ext, source_sha256 = prepare(src)
    rel = f"{'injection' if kind == 'injection_test' else 'docs'}/{doc_id}{ext}"
    dest = corpus / rel
    if dest.exists():
        raise AddError(f"{dest} already exists")
    entry = {"doc_id": doc_id, "title": title, "path": rel, "url": url or f"repo:corpus/{rel}",
             "accessed": accessed, "licence": licence, "sha256": hashlib.sha256(data).hexdigest(), "kind": kind,
             "author": author or TEAM, "source_sha256": source_sha256}
    register = corpus / REGISTER_FILE
    before = register.read_text(encoding="utf-8")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        register.write_text(before + ("" if before.endswith("\n") else "\n") + entry_block(entry), encoding="utf-8")
        problems = verify_register(corpus)
        if problems:
            raise AddError("the register did not verify after the addition, so it was undone:\n  "
                           + "\n  ".join(problems))
        if not any(e.get("doc_id") == doc_id for e in load_register(corpus)):
            raise AddError("the new entry did not land in the register's documents list, so it was undone; "
                           "documents must stay the register's last key")
    except BaseException:
        register.write_text(before, encoding="utf-8")
        dest.unlink(missing_ok=True)
        raise
    return entry


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Add a document to the knowledge corpus, or verify its register")
    p.add_argument("--corpus", type=Path, default=ROOT / "corpus", help="the corpus folder (default: corpus/)")
    p.add_argument("--verify", action="store_true", help="check the register and exit 1 on any problem")
    p.add_argument("--file", help="the document to add: .md, .txt or .html")
    p.add_argument("--url", help="where it came from (https:// or doi:); team notes default to repo:corpus/<path>")
    p.add_argument("--licence", help="one of the licences the register accepts, e.g. 'CC BY 4.0'")
    p.add_argument("--title", help="the document's title")
    p.add_argument("--accessed", help="date retrieved, YYYY-MM-DD (default: today, UTC)")
    p.add_argument("--kind", default="external", choices=KINDS)
    p.add_argument("--author", help="for the attribution; required for external documents")
    p.add_argument("--doc-id", help="the id to cite it by (default: from the title)")
    args = p.parse_args(argv)

    if args.verify:
        problems = verify_register(args.corpus)
        for problem in problems:
            print(problem)
        if problems:
            return 1
        print(f"register OK: {len(load_register(args.corpus))} documents, every file listed and its sha256 matching")
        return 0
    missing = [f"--{name}" for name in ("file", "licence", "title") if not getattr(args, name)]
    if missing:
        p.error(f"adding a document needs {', '.join(missing)} (or use --verify)")
    try:
        entry = add(args.corpus, args.file, args.url, args.licence, args.title, args.accessed, args.kind,
                    args.author, args.doc_id)
    except AddError as exc:
        print(f"not added: {exc}", file=sys.stderr)
        return 1
    text = sanitize((args.corpus / entry["path"]).read_bytes())
    print(f"added {entry['doc_id']}: {entry['path']}, sha256 {entry['sha256'][:12]}..., "
          f"{len(passages(text))} passages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
