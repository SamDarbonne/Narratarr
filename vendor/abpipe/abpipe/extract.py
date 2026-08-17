"""Stage 1 -- extract. CONTRACT.md section 5. Owner: Worker A.

Reads the EPUB. Writes ``01-extract/<id>.txt``, ``book.json``, and ``cover.jpg``.

This module also owns the book-config loader, CONTRACT.md section 4.1/4.1.1:
``load_book_config()``. Every stage that needs per-book data reads it from the
loaded config, not from a code constant -- CONTRACT.md 4.1's opening rule.
"""

from __future__ import annotations

import json
import posixpath
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from abpipe.context import project_root
from abpipe.meta import (
    clear_meta,
    hash_bytes,
    hash_file,
    hash_obj,
    is_fresh,
    write_bytes,
    write_meta,
    write_text,
)

# The config_hash of cover.jpg does not depend on the book config (unlike
# every text chapter output) -- it is a fixed marker so is_fresh()'s
# config_hash comparison still means something (a change to how cover
# extraction itself works would need its own bump here in the future).
COVER_CONFIG = {"stage": "extract_cover", "schema": 1}

STAGE = "extract"

DEFAULT_CHAPTER_PATTERN = r"^\s*Chapter\s+[IVXLCDM]+\s*$"

# A true GLOBAL default, CONTRACT.md 5.2: general English dialect, not one
# book's data. Kokoro reads `givin'` as `ʤˈɪvɪn` (soft "j", sounds like
# "jivin'"); `guivin` measures as `ɡˈɪvɪn`, the correct hard g. Every new book
# gets this fix for free unless its config sets inherit_default_pronunciations
# to false. A per-book correction (like Book A's former `Gyko` entry)
# belongs in that book's source/<slug>.config.json, never here.
DEFAULT_PRONUNCIATIONS = {"givin": "guivin"}

DEFAULT_ENGINE = {
    "name": "kokoro_mlx",
    "model": "mlx-community/Kokoro-82M-bf16",
    "voice": "bm_george",
    "speed": 1.0,
    "lang_code": "b",
    "sample_rate": 24000,
}

DEFAULT_ELEMENTS = {
    "note_markers": "drop",
    "footnotes": "drop",
    "tables": "drop",
    "figures": "drop",
    "captions": "drop",
    "epigraphs": "render",
}

# CONTRACT.md 6.1's course correction (2026-08-15): expand_numbers now
# defaults to false -- misaki already reads most number forms correctly, and
# a hand-written rule only earns its keep when it beats that. caliber
# defaults to true (CONTRACT.md 6.1.1): misaki's one measured miss,
# ".22 caliber" -> "point two two caliber", is real in Book B and cheap
# to fix. recase_caps_run and drop_symbol_paragraphs default to true: the
# ALL-CAPS letter-spelling defect and the "asterisk" scene-break defect are
# both real most of the time; a book with neither hazard opts out explicitly
# (Book A does, for both).
DEFAULT_NORMALIZE = {
    "expand_numbers": False,
    "recase_caps_run": True,
    "min_caps_run_words": 2,
    "drop_symbol_paragraphs": True,
    "caliber": True,
    # CONTRACT.md 6's drop_citations (added 2026-08-16, Book C):
    # the first normalize rule that removes words the author
    # wrote, not just spelling/case/punctuation -- default False so no
    # existing book's output can change. Only a book config that opts in
    # (book-c.config.json) sets it True.
    "drop_citations": False,
    # CONTRACT.md 6's drop_sic (added 2026-08-16): removes a spoken-noise
    # editorial "[sic]" mark. Default True is safe because Book A and
    # Book B hold zero brackets of any kind -- only a book with real
    # [sic] marks (Book C, Book D) is affected.
    "drop_sic": True,
}

_ELEMENT_POLICY_VALUES = ("drop", "render")

# --------------------------------------------------------------------------- book config schema

_TOP_LEVEL_KEYS = {
    "schema", "slug", "source_epub", "title", "author", "year", "genre", "language",
    "chapters", "engine", "credits", "pronunciations", "inherit_default_pronunciations",
    "elements", "normalize", "qc",
}
_CHAPTERS_KEYS = {"select", "pattern", "labels", "span_to_next_toc_entry", "drop_paragraph_classes"}
_ENGINE_KEYS = {"name", "model", "voice", "speed", "lang_code", "sample_rate"}
_CREDITS_KEYS = {"enabled", "text"}
_ELEMENTS_KEYS = set(DEFAULT_ELEMENTS)
_NORMALIZE_KEYS = set(DEFAULT_NORMALIZE)
_QC_KEYS = {"equivalences"}
_CHAPTERS_SELECT_VALUES = ("pattern", "labels")


def _default_book_config(slug: str) -> dict:
    """Return the full CONTRACT.md 4.1 default config for `slug`.

    Every documented key is present. A book with no config file at all runs
    on exactly this -- CONTRACT.md 4.1.1: "a missing file returns the full
    default config with the given slug, so a book with no config still runs
    on the defaults."
    """
    return {
        "schema": 1,
        "slug": slug,
        "source_epub": f"source/{slug}.epub",
        "title": None,
        "author": None,
        "year": None,
        "genre": None,
        "language": None,
        "chapters": {
            "select": "pattern",
            "pattern": DEFAULT_CHAPTER_PATTERN,
            "labels": [],
            "span_to_next_toc_entry": False,
            "drop_paragraph_classes": [],
        },
        "engine": dict(DEFAULT_ENGINE),
        "credits": {"enabled": True, "text": None},
        "pronunciations": {},
        "inherit_default_pronunciations": True,
        "elements": dict(DEFAULT_ELEMENTS),
        "normalize": dict(DEFAULT_NORMALIZE),
        "qc": {"equivalences": {}},
    }


def _fail(filename, message: str) -> None:
    raise ValueError(f"{filename}: {message}")


def _merge_sub_dict(raw_sub: dict, default_sub: dict, allowed_keys: set, filename, label: str) -> dict:
    if not isinstance(raw_sub, dict):
        _fail(filename, f"{label!r} must be a JSON object")
    unknown = set(raw_sub) - allowed_keys
    if unknown:
        _fail(filename, f"unknown key {sorted(unknown)[0]!r} in {label!r}")
    merged = dict(default_sub)
    merged.update(raw_sub)
    return merged


def _validate_elements(elements: dict, filename) -> None:
    for key, value in elements.items():
        if value not in _ELEMENT_POLICY_VALUES:
            _fail(
                filename,
                f"elements.{key} is {value!r}, must be 'drop' or 'render'",
            )


def _validate_chapters(chapters: dict, filename) -> None:
    select = chapters.get("select")
    if select not in _CHAPTERS_SELECT_VALUES:
        _fail(filename, f"chapters.select is {select!r}, must be 'pattern' or 'labels'")
    drop_paragraph_classes = chapters.get("drop_paragraph_classes")
    if drop_paragraph_classes is not None:
        if not isinstance(drop_paragraph_classes, list) or not all(
            isinstance(c, str) for c in drop_paragraph_classes
        ):
            _fail(filename, "chapters.drop_paragraph_classes must be a list of strings")


def load_book_config(path: str | Path | None, slug: str | None = None) -> dict:
    """Read, validate, and default a book config. CONTRACT.md 4.1.1.

    `path` of None means ``source/<slug>.config.json``. `slug`, when given,
    always wins over whatever the file itself says (the CLI's --slug flag
    overrides the config, CONTRACT.md section 14's precedence chain) -- it
    also names the book when the file is entirely absent.

    A missing file returns the full default config for the resolved slug. A
    present file is validated: an unknown top-level or nested key, a bad
    ``schema``, a bad ``chapters.select``, or an ``elements`` value that is
    not ``"drop"``/``"render"`` all raise ValueError naming the file and the
    fault.
    """
    if path is None:
        if slug is None:
            raise ValueError("load_book_config: slug is required when path is None")
        resolved_path = project_root() / "source" / f"{slug}.config.json"
    else:
        resolved_path = Path(path)
        if not resolved_path.is_absolute():
            resolved_path = project_root() / resolved_path

    name = resolved_path.name
    if name.endswith(".config.json"):
        derived_slug = name[: -len(".config.json")]
    else:
        derived_slug = resolved_path.stem
    effective_slug = slug or derived_slug

    if not resolved_path.exists():
        return _default_book_config(effective_slug)

    filename = str(resolved_path)
    try:
        raw = json.loads(resolved_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _fail(filename, f"not valid JSON ({exc})")
    if not isinstance(raw, dict):
        _fail(filename, "must be a JSON object")

    unknown = set(raw) - _TOP_LEVEL_KEYS
    if unknown:
        _fail(filename, f"unknown key {sorted(unknown)[0]!r}")

    schema = raw.get("schema", 1)
    if schema != 1:
        _fail(filename, f"unsupported schema {schema!r}, expected 1")

    # CONTRACT.md 14.1 Rule 1: `slug` is a fallback, never an override. A
    # config file that declares its own `slug` always wins -- the incident
    # this rule fixes was exactly the old, backwards precedence here: a
    # caller passing an unrelated slug got that file's real content (a
    # different book's EPUB, chapter selection, everything) silently
    # relabelled as the caller's book, and every downstream stage then wrote
    # one book's material into another book's work/ directory. A caller that
    # passes a slug contradicting the file's own is a real, load-bearing
    # mistake -- refuse it loudly instead of picking a side silently.
    file_slug = raw.get("slug")
    if file_slug:
        if slug is not None and slug != file_slug:
            _fail(
                filename,
                f"declares slug {file_slug!r}, but the caller asked for slug {slug!r}. "
                "A config file's own slug always wins (CONTRACT.md 14.1 rule 1) -- pass "
                "the matching slug, or point --config at the file for the slug you meant.",
            )
        resolved_slug = file_slug
    else:
        resolved_slug = effective_slug
    default = _default_book_config(resolved_slug)

    config = dict(default)
    config["schema"] = 1
    config["slug"] = resolved_slug
    for key in ("source_epub", "title", "author", "year", "genre", "language"):
        if key in raw:
            config[key] = raw[key]

    if "chapters" in raw:
        config["chapters"] = _merge_sub_dict(raw["chapters"], default["chapters"], _CHAPTERS_KEYS, filename, "chapters")
    _validate_chapters(config["chapters"], filename)

    if "engine" in raw:
        config["engine"] = _merge_sub_dict(raw["engine"], default["engine"], _ENGINE_KEYS, filename, "engine")

    if "credits" in raw:
        config["credits"] = _merge_sub_dict(raw["credits"], default["credits"], _CREDITS_KEYS, filename, "credits")

    if "pronunciations" in raw:
        if not isinstance(raw["pronunciations"], dict):
            _fail(filename, "'pronunciations' must be a JSON object")
        config["pronunciations"] = dict(raw["pronunciations"])

    if "inherit_default_pronunciations" in raw:
        config["inherit_default_pronunciations"] = bool(raw["inherit_default_pronunciations"])

    if "elements" in raw:
        config["elements"] = _merge_sub_dict(raw["elements"], default["elements"], _ELEMENTS_KEYS, filename, "elements")
    _validate_elements(config["elements"], filename)

    if "normalize" in raw:
        config["normalize"] = _merge_sub_dict(raw["normalize"], default["normalize"], _NORMALIZE_KEYS, filename, "normalize")

    if "qc" in raw:
        config["qc"] = _merge_sub_dict(raw["qc"], default["qc"], _QC_KEYS, filename, "qc")

    return config


def extract_config_hash(book_config: dict) -> str:
    """Return stage 1's config_hash for a loaded book config. CONTRACT.md
    section 5: "the whole book config, as the loader returns it after every
    default is filled." """
    return hash_obj(book_config)


NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
    "cn": "urn:oasis:names:tc:opendocument:xmlns:container",
    "xhtml": "http://www.w3.org/1999/xhtml",
    "epub": "http://www.idpf.org/2007/ops",
}

_HEADING_TAG_RE = re.compile(r"^h[1-6]$")
# Collapse only ASCII line-structure whitespace (the source hard-wraps inside
# every <p>). NBSP (U+00A0) and hair space (U+200A) are meaningful typographic
# content in this EPUB (they appear inside the ". . ." ellipsis convention and
# around elision marks) and survive verbatim -- normalize.py (stage 2) is the
# stage that maps them to a plain space. Extract stays faithful.
_WS_RE = re.compile(r"[ \t\n\r\f\v]+")


# --------------------------------------------------------------------------- DRM refusal, CONTRACT.md 5.4

_ENC_NS = {"enc": "http://www.w3.org/2001/04/xmlenc#"}
# Font obfuscation is normal in a retail EPUB and is not DRM. The IDPF and
# Adobe font-obfuscation algorithms are both allowed regardless of where the
# obfuscated file lives; a CipherReference under fonts/ is allowed regardless
# of algorithm, as a second, path-based signal for a publisher that used a
# nonstandard URI for the same purpose.
_ALLOWED_FONT_ALGORITHMS = {
    "http://www.idpf.org/2008/embedding",
    "http://ns.adobe.com/pdf/enc#RC",
}


def _check_drm(zf: zipfile.ZipFile, epub_path) -> None:
    """CONTRACT.md 5.4. Raise ValueError on real DRM. Font obfuscation passes."""
    try:
        data = zf.read("META-INF/encryption.xml")
    except KeyError:
        return
    root = ET.fromstring(data)
    for enc in root.findall(".//enc:EncryptedData", _ENC_NS):
        method_el = enc.find("enc:EncryptionMethod", _ENC_NS)
        algorithm = method_el.get("Algorithm") if method_el is not None else None
        ref_el = enc.find(".//enc:CipherReference", _ENC_NS)
        uri = ref_el.get("URI") if ref_el is not None else ""
        is_font_path = "fonts/" in uri or uri.startswith("font")
        is_font_algorithm = algorithm in _ALLOWED_FONT_ALGORITHMS
        if not (is_font_path or is_font_algorithm):
            raise ValueError(
                f"{epub_path}: holds DRM on {uri!r} (algorithm {algorithm!r}) that is not "
                "font obfuscation. The pipeline needs a DRM-free source."
            )


# --------------------------------------------------------------------------- EPUB parsing


def _find_opf_path(zf: zipfile.ZipFile) -> str:
    root = ET.fromstring(zf.read("META-INF/container.xml"))
    rootfile = root.find(".//cn:rootfile", NS)
    return rootfile.get("full-path")


def _parse_opf(zf: zipfile.ZipFile, opf_path: str) -> dict:
    opf_dir = posixpath.dirname(opf_path)
    root = ET.fromstring(zf.read(opf_path))
    md = root.find("opf:metadata", NS)

    def _dc(tag: str) -> str | None:
        el = md.find(f"dc:{tag}", NS)
        return el.text.strip() if el is not None and el.text else None

    metadata = {
        "title": _dc("title"),
        "creator": _dc("creator"),
        "date": _dc("date"),
        "language": _dc("language") or "en",
    }

    manifest: dict[str, dict] = {}
    for item in root.find("opf:manifest", NS):
        iid = item.get("id")
        manifest[iid] = {
            "href": item.get("href"),
            "media_type": item.get("media-type"),
            "properties": item.get("properties") or "",
        }

    spine_el = root.find("opf:spine", NS)
    ncx_id = spine_el.get("toc")
    spine = [it.get("idref") for it in spine_el.findall("opf:itemref", NS)]

    cover_id = None
    for meta_el in md.findall("opf:meta", NS):
        if meta_el.get("name") == "cover":
            cover_id = meta_el.get("content")
    if cover_id is None:
        for iid, item in manifest.items():
            if "cover-image" in item["properties"].split():
                cover_id = iid
                break

    ncx_href = manifest[ncx_id]["href"] if ncx_id in manifest else None

    # CONTRACT.md 5: the EPUB3 nav document is found by the `nav` property in
    # the OPF manifest, not by a fixed filename.
    nav_href = None
    for item in manifest.values():
        if "nav" in item["properties"].split():
            nav_href = item["href"]
            break

    return {
        "opf_dir": opf_dir,
        "metadata": metadata,
        "manifest": manifest,
        "spine": spine,
        "cover_id": cover_id,
        "ncx_href": ncx_href,
        "nav_href": nav_href,
    }


def _parse_ncx(zf: zipfile.ZipFile, ncx_path: str) -> list[dict]:
    root = ET.fromstring(zf.read(ncx_path))
    navmap = root.find("ncx:navMap", NS)
    points = []
    for nav in navmap.findall("ncx:navPoint", NS):
        label_el = nav.find("ncx:navLabel/ncx:text", NS)
        content_el = nav.find("ncx:content", NS)
        label = (label_el.text or "").strip() if label_el is not None else ""
        src = content_el.get("src") if content_el is not None else ""
        order = int(nav.get("playOrder") or 0)
        points.append({"label": label, "src": src, "playOrder": order})
    points.sort(key=lambda p: p["playOrder"])
    return points


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _epub_type_of(el, attrib_name: str = "type") -> list[str]:
    """Return the split value of an `epub:type`-shaped attribute, tolerant of
    whichever namespace prefix the publisher bound to the idpf ops URI (the
    spec allows any prefix; "epub" is conventional, but a real EPUB is free
    to use "ops" instead -- Book B's own OPF does)."""
    for key, value in el.attrib.items():
        local = key.rsplit("}", 1)[-1] if "}" in key else key.rsplit(":", 1)[-1]
        prefix_ish = key.split(":")[0] if ":" in key and "}" not in key else ""
        if local == attrib_name and (
            key.startswith(f"{{{NS['epub']}}}")
            or prefix_ish in ("epub", "ops")
        ):
            return (value or "").split()
    return []


def _parse_nav(zf: zipfile.ZipFile, nav_path: str) -> list[dict]:
    """Parse an EPUB3 nav.xhtml's ``<nav epub:type="toc">`` list. CONTRACT.md 5.

    Returns the same record shape _parse_ncx returns: a label, a source href
    (possibly with a #fragment), and an order, in reading order.
    """
    root = ET.fromstring(zf.read(nav_path))
    nav_el = None
    for el in root.iter():
        if _local_tag(el.tag) == "nav" and "toc" in _epub_type_of(el):
            nav_el = el
            break
    if nav_el is None:
        return []
    records = []
    order = 0
    for a in nav_el.iter():
        if _local_tag(a.tag) != "a":
            continue
        href = a.get("href")
        if not href:
            continue
        label = "".join(a.itertext())
        label = _WS_RE.sub(" ", label).strip()
        order += 1
        records.append({"label": label, "src": href, "playOrder": order})
    return records


def _read_toc(zf: zipfile.ZipFile, opf: dict) -> list[dict]:
    """CONTRACT.md 5: read the EPUB3 nav.xhtml TOC first, the EPUB2 NCX second."""
    if opf.get("nav_href"):
        nav_path = posixpath.join(opf["opf_dir"], opf["nav_href"])
        records = _parse_nav(zf, nav_path)
        if records:
            return records
    if opf.get("ncx_href"):
        ncx_path = posixpath.join(opf["opf_dir"], opf["ncx_href"])
        return _parse_ncx(zf, ncx_path)
    return []


def _toc_target_item_id(record: dict, opf: dict) -> tuple[str, str | None]:
    """Return (manifest item id, fragment or None) of one TOC record's target."""
    href_part, _, fragment = record["src"].partition("#")
    href_to_id = {item["href"]: iid for iid, item in opf["manifest"].items()}
    item_id = href_to_id.get(href_part)
    if item_id is None:
        raise KeyError(f"TOC target not found in manifest: {href_part!r}")
    return item_id, (fragment or None)


def _resolve_navpoint(navpoint: dict, opf: dict) -> tuple[str, str | None]:
    """Return (internal_zip_path, fragment) of a navPoint's target file."""
    item_id, fragment = _toc_target_item_id(navpoint, opf)
    full_path = posixpath.join(opf["opf_dir"], opf["manifest"][item_id]["href"])
    return full_path, fragment


# --------------------------------------------------------------------------- chapter selection, CONTRACT.md 5.1


def _select_by_pattern(all_records: list[dict], pattern: str) -> list[tuple[int, dict]]:
    chapter_re = re.compile(pattern)
    return [(i, r) for i, r in enumerate(all_records) if chapter_re.match(r["label"])]


def _select_by_labels(all_records: list[dict], labels: list[str]) -> list[tuple[int, dict]]:
    """Select TOC records by an ordered list of exact labels, matching by
    POSITION in reading order -- CONTRACT.md 5.1's warning about a naive set
    membership test collapsing two records that share a label. A single
    cursor walks forward through `all_records` once; each wanted label
    consumes the next record with that exact text, so two requested labels
    that happen to share text still resolve to two distinct records, in
    order, never the same one twice.
    """
    result = []
    idx = 0
    n = len(all_records)
    for wanted in labels:
        while idx < n and all_records[idx]["label"] != wanted:
            idx += 1
        if idx >= n:
            raise ValueError(f"chapter label not found in the TOC, in reading order: {wanted!r}")
        result.append((idx, all_records[idx]))
        idx += 1
    return result


def _select_toc_records(all_records: list[dict], chapters_cfg: dict) -> list[tuple[int, dict]]:
    select = chapters_cfg.get("select") or "pattern"
    if select == "labels":
        return _select_by_labels(all_records, chapters_cfg.get("labels") or [])
    pattern = chapters_cfg.get("pattern") or DEFAULT_CHAPTER_PATTERN
    return _select_by_pattern(all_records, pattern)


def _chapter_spine_items(all_records: list[dict], idx: int, opf: dict, span: bool) -> tuple[list[str], str | None]:
    """Return (list of manifest item ids, first-item fragment) for one
    selected chapter. CONTRACT.md 5.1.

    Not spanning: exactly the TOC target's own spine item.

    Spanning: every spine item from the TOC target's own position, up to but
    not including the position of the NEXT TOC entry in the full TOC (not
    only the next *selected* one) -- measured against the real Book B
    EPUB: the last selected part has no selected chapter after it, but "Dedication"
    is the very next TOC entry, and c09 alone (1,886 words) is the correct
    span, not c09 through the end of the spine (which would also absorb
    "Other Books by This Author" and "About the Author"). Flagged to the
    overlord: CONTRACT.md 5.1's prose says "up to but not including the next
    selected TOC target" -- this implementation uses the next TOC target of
    ANY kind, which is what the measured word counts actually require.
    """
    item_id, fragment = _toc_target_item_id(all_records[idx], opf)
    spine = opf["spine"]
    start = spine.index(item_id)
    if not span:
        return [item_id], fragment
    end = len(spine)
    if idx + 1 < len(all_records):
        next_item_id, _ = _toc_target_item_id(all_records[idx + 1], opf)
        if next_item_id in spine:
            end = spine.index(next_item_id)
    if end <= start:
        end = start + 1
    return spine[start:end], fragment


# --------------------------------------------------------------------------- structural stripping, CONTRACT.md 5.3


def _has_epub_type(tag, value: str) -> bool:
    return value in _epub_type_of(_AttrShim(tag))


class _AttrShim:
    """Adapts a bs4 Tag's .attrs to the .attrib-based _epub_type_of() helper
    written against ElementTree, so both the NCX/nav (ElementTree) side and
    the chapter-body (BeautifulSoup) side of this module share one
    epub:type-reading rule instead of two that could drift apart."""

    def __init__(self, tag) -> None:
        self.attrib = dict(tag.attrs) if tag is not None else {}
        for key, value in list(self.attrib.items()):
            if isinstance(value, list):
                self.attrib[key] = " ".join(value)


def _class_list(tag) -> list[str]:
    """Return a tag's `class` attribute as a list of tokens, regardless of
    whether the parser returned a string (bs4's XML-mode "lxml-xml" builder,
    which this module uses throughout) or a list (bs4's HTML-mode builders
    split on whitespace automatically; XML mode does not)."""
    value = tag.get("class")
    if value is None:
        return []
    if isinstance(value, str):
        return value.split()
    return list(value)


def _strip_elements(scope, elements: dict, drop_paragraph_classes: list[str] | None = None) -> None:
    """Mutate `scope` (a bs4 Tag) in place, removing elements per the
    CONTRACT.md 5.3 policy table. Runs BEFORE any text is pulled out of the
    tree -- "strip before the text is read, not after. A rule that works on
    the extracted string cannot tell a note marker from a real number."

    `drop_paragraph_classes` is `chapters.drop_paragraph_classes` from the
    book config (default `[]`, CONTRACT.md 4.1): an EXACT class-token match,
    unlike the `captions` policy's substring match above. It exists for a
    publisher that repeats the chapter title as styled paragraphs instead of
    a heading element -- Book C's `<p class="label">FOUR</p>`
    and `<p class="h2a"><em>The Aztecs</em></p>` duplicate the TOC label the
    pipeline already speaks as the chapter heading line. Default `[]` means
    no existing book's output changes.
    """
    if elements.get("note_markers") == "drop":
        for el in list(scope.find_all(True)):
            if _has_epub_type(el, "noteref"):
                el.decompose()
        for sup in list(scope.find_all("sup")):
            if sup.decomposed:
                continue
            text = sup.get_text().strip()
            if text and not any(ch.isalpha() for ch in text):
                sup.decompose()

    if elements.get("footnotes") == "drop":
        for aside in list(scope.find_all("aside")):
            if _has_epub_type(aside, "footnote") or _has_epub_type(aside, "endnote"):
                aside.decompose()

    if elements.get("tables") == "drop":
        for table in list(scope.find_all("table")):
            table.decompose()

    if elements.get("figures") == "drop":
        for figure in list(scope.find_all("figure")):
            figure.decompose()
        for img in list(scope.find_all("img")):
            img.decompose()

    if elements.get("captions") == "drop":
        for figcaption in list(scope.find_all("figcaption")):
            figcaption.decompose()
        for p in list(scope.find_all("p")):
            if p.decomposed:
                continue
            if any("caption" in c for c in _class_list(p)):
                p.decompose()

    if elements.get("epigraphs") == "drop":
        for bq in list(scope.find_all("blockquote")):
            if _has_epub_type(bq, "epigraph"):
                bq.decompose()
        for div in list(scope.find_all("div")):
            if div.decomposed:
                continue
            if "epigraph" in _class_list(div):
                div.decompose()

    if drop_paragraph_classes:
        drop_set = set(drop_paragraph_classes)
        for p in list(scope.find_all("p")):
            if p.decomposed:
                continue
            if drop_set & set(_class_list(p)):
                p.decompose()


def _clean_text(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def _extract_from_scope(scope, elements: dict, drop_paragraph_classes: list[str] | None = None) -> tuple[str, list[str]]:
    """Strip, then read (heading_text, paragraphs) out of one bs4 scope."""
    _strip_elements(scope, elements, drop_paragraph_classes)

    heading_el = scope.find(_HEADING_TAG_RE)
    heading = _clean_text(heading_el.get_text()) if heading_el is not None else ""

    paragraphs = []
    for p in scope.find_all("p"):
        text = _clean_text(p.get_text())
        if text:
            paragraphs.append(text)

    return heading, paragraphs


def _extract_chapter_body(
    zf: zipfile.ZipFile,
    zip_path: str,
    fragment: str | None,
    elements: dict,
    drop_paragraph_classes: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Return (heading_text, paragraphs) for one chapter file.

    Defect fixed 2026-08-16 (Book C): a TOC fragment does
    not always point at an element that WRAPS the chapter's content. This
    publisher's EPUB nests the fragment target as an empty self-closing
    anchor a couple of levels INSIDE the real content instead:
    `<p class="label" id="page_66"><a id="ch4"/>FOUR</p>`. Scoping to
    `<a id="ch4"/>` is scoping to an element with no children at all, so the
    old code silently returned zero paragraphs -- stage 1 reported "done"
    and wrote an empty chapter file. When the fragment scope yields no
    paragraphs, fall back to the whole body and re-extract from there. This
    can only ADD content where there was none: a fragment that already
    scopes to real content (the ordinary case, and the one CONTRACT.md 5.1's
    span-to-next-toc-entry logic depends on) is untouched, because its
    paragraph list is non-empty and the fallback branch never runs.
    """
    data = zf.read(zip_path).decode("utf-8")
    soup = BeautifulSoup(data, "lxml-xml")

    scope = None
    if fragment:
        scope = soup.find(id=fragment)
    if scope is None:
        scope = soup.body or soup

    heading, paragraphs = _extract_from_scope(scope, elements, drop_paragraph_classes)

    if fragment and not paragraphs:
        fallback_scope = soup.body or soup
        if fallback_scope is not scope:
            heading, paragraphs = _extract_from_scope(fallback_scope, elements, drop_paragraph_classes)

    return heading, paragraphs


def _extract_chapter_body_multi(
    zf: zipfile.ZipFile,
    opf: dict,
    item_ids: list[str],
    first_fragment: str | None,
    elements: dict,
    drop_paragraph_classes: list[str] | None = None,
) -> tuple[str, list[str], list[str]]:
    """CONTRACT.md 5.1: join the paragraphs of every spanned spine item, in
    spine order. Only the first item honours the TOC target's own fragment
    (a spanned-in item has no fragment of its own -- it is included purely by
    spine position). The heading comes from whichever spanned item has one;
    Book B's part heading is `<h1 class="chapter"><img/></h1>` (no
    text), so this naturally falls through to every book's TOC-label
    fallback in run() below."""
    heading = ""
    paragraphs: list[str] = []
    srcs: list[str] = []
    for i, item_id in enumerate(item_ids):
        href = opf["manifest"][item_id]["href"]
        full_path = posixpath.join(opf["opf_dir"], href)
        srcs.append(full_path)
        frag = first_fragment if i == 0 else None
        h, ps = _extract_chapter_body(zf, full_path, frag, elements, drop_paragraph_classes)
        if not heading and h:
            heading = h
        paragraphs.extend(ps)
    return heading, paragraphs, srcs


def _extract_cover(zf: zipfile.ZipFile, opf: dict) -> bytes | None:
    cover_id = opf["cover_id"]
    if not cover_id or cover_id not in opf["manifest"]:
        return None
    href = opf["manifest"][cover_id]["href"]
    full_path = posixpath.join(opf["opf_dir"], href)
    return zf.read(full_path)


def _year_from_opf_date(date: str | None) -> str:
    if not date:
        return ""
    m = re.search(r"\b(1[0-9]{3}|2[0-9]{3})\b", date)
    return m.group(1) if m else ""


# --------------------------------------------------------------------------- file format


def _write_chapter_file(out_path, heading: str, paragraphs: list[str]) -> None:
    lines = [heading, ""]
    for i, para in enumerate(paragraphs):
        if i > 0:
            lines.append("")
        lines.append(para)
    text = "\n".join(lines) + "\n"
    write_text(out_path, text)


def _write_credits_file(out_path, text: str) -> None:
    write_text(out_path, text.strip() + "\n")


# --------------------------------------------------------------------------- run()


def run(
    ctx,
    chapters: list[str] | None = None,
    force: bool = False,
    book_config: dict | None = None,
    **kw,
) -> dict:
    """Run stage 1. Return a summary dict. CONTRACT.md section 5."""
    if book_config is None:
        book_config = load_book_config(None, slug=ctx.slug)

    credits_cfg = dict(book_config.get("credits") or {"enabled": True, "text": None})
    chapters_cfg = book_config.get("chapters") or {}
    elements = book_config.get("elements") or dict(DEFAULT_ELEMENTS)
    span = bool(chapters_cfg.get("span_to_next_toc_entry"))
    drop_paragraph_classes = list(chapters_cfg.get("drop_paragraph_classes") or [])

    own_pronunciations = dict(book_config.get("pronunciations") or {})
    inherit = book_config.get("inherit_default_pronunciations", True)
    pronunciations = {**DEFAULT_PRONUNCIATIONS, **own_pronunciations} if inherit else own_pronunciations

    epub_path = ctx.epub
    source_sha256 = hash_file(epub_path)
    config_hash = extract_config_hash(book_config)

    ctx.book_dir.mkdir(parents=True, exist_ok=True)
    extract_dir = ctx.stage_dir("extract")

    with zipfile.ZipFile(epub_path, "r") as zf:
        _check_drm(zf, epub_path)

        opf_path = _find_opf_path(zf)
        opf = _parse_opf(zf, opf_path)
        all_records = _read_toc(zf, opf)
        selected = _select_toc_records(all_records, chapters_cfg)

        records = []  # dicts: id, index, label, src, synthetic, heading, paragraphs

        if credits_cfg.get("enabled", True):
            records.append(
                {
                    "id": "ch00",
                    "index": 0,
                    "label": "Opening Credits",
                    "src": None,
                    "synthetic": True,
                    "heading": None,
                    "paragraphs": [credits_cfg.get("text") or ""],
                }
            )

        for i, (toc_idx, record) in enumerate(selected, start=1):
            item_ids, fragment = _chapter_spine_items(all_records, toc_idx, opf, span)
            heading, paragraphs, srcs = _extract_chapter_body_multi(
                zf, opf, item_ids, fragment, elements, drop_paragraph_classes
            )
            if not heading:
                heading = record["label"]
            src_value = srcs if span else srcs[0]
            records.append(
                {
                    "id": f"ch{i:02d}",
                    "index": i,
                    "label": record["label"],
                    "src": src_value,
                    "synthetic": False,
                    "heading": heading,
                    "paragraphs": paragraphs,
                }
            )

        cover_bytes = _extract_cover(zf, opf)

        opf_title = opf["metadata"]["title"] or ctx.slug
        opf_author = opf["metadata"]["creator"] or "Unknown"
        opf_language = opf["metadata"]["language"] or "en"
        opf_year = _year_from_opf_date(opf["metadata"]["date"])

    title = book_config.get("title") or opf_title
    author = book_config.get("author") or opf_author
    language = book_config.get("language") or opf_language
    year = book_config.get("year") or opf_year or ""
    genre = book_config.get("genre") or "Fiction"

    credits_text = credits_cfg.get("text") or f"{title}, by {author}."
    credits_out = {"enabled": bool(credits_cfg.get("enabled", True)), "text": credits_text}
    if records and records[0]["id"] == "ch00":
        records[0]["paragraphs"] = [credits_text]

    # -------------------------------------------------------------- restrict + write

    allowed_ids = set(chapters) if chapters is not None else None
    if allowed_ids is not None:
        known = {r["id"] for r in records}
        unknown = allowed_ids - known
        if unknown:
            raise KeyError(f"unknown chapter id: {', '.join(sorted(unknown))}")

    done = 0
    skipped = 0
    failed: list[str] = []
    chapters_out = []

    try:
        source_epub_rel = str(ctx.epub.relative_to(ctx.root))
    except ValueError:
        source_epub_rel = str(ctx.epub)

    for rec in records:
        cid = rec["id"]
        words = sum(len(p.split()) for p in rec["paragraphs"])
        chapters_out.append(
            {
                "id": cid,
                "index": rec["index"],
                "label": rec["label"],
                "src": rec["src"],
                "synthetic": rec["synthetic"],
                "words": words,
            }
        )

        if allowed_ids is not None and cid not in allowed_ids:
            continue

        out_path = extract_dir / f"{cid}.txt"

        if not force and is_fresh(out_path, source_sha256, config_hash):
            skipped += 1
            continue

        try:
            if rec["synthetic"]:
                _write_credits_file(out_path, rec["paragraphs"][0])
            else:
                _write_chapter_file(out_path, rec["heading"], rec["paragraphs"])
            write_meta(out_path, STAGE, source_sha256, config_hash, extra={"words": words})
            done += 1
        except Exception:
            failed.append(cid)

    # Clean up a stray ch00 left over from a run where credits were enabled.
    if not credits_out["enabled"]:
        stray = extract_dir / "ch00.txt"
        if stray.exists():
            clear_meta(stray)

    book = {
        "schema": 1,
        "slug": ctx.slug,
        "title": title,
        "author": author,
        "year": str(year),
        "genre": genre,
        "language": language,
        "source_epub": source_epub_rel,
        "source_sha256": source_sha256,
        "cover": "cover.jpg" if cover_bytes else None,
        "engine": dict(book_config.get("engine") or DEFAULT_ENGINE),
        "credits": credits_out,
        "pronunciations": pronunciations,
        "chapters": chapters_out,
    }
    ctx.save_book(book)

    if cover_bytes:
        # Defect 4 (abpipe): cover.jpg, unlike every other artifact
        # this stage writes, used to be written with a plain write_bytes()
        # (not atomic) and no meta file beside it -- so a kill mid-write left
        # a truncated cover with no way for is_fresh() to ever catch it, and
        # bind.py (stage 7) embeds this exact file into the final m4b.
        cover_path = ctx.book_dir / "cover.jpg"
        cover_input_hash = hash_bytes(cover_bytes)
        cover_config_hash = hash_obj(COVER_CONFIG)
        if force:
            clear_meta(cover_path)
        if force or not is_fresh(cover_path, cover_input_hash, cover_config_hash):
            write_bytes(cover_path, cover_bytes)
            write_meta(
                cover_path, STAGE, cover_input_hash, cover_config_hash,
                extra={"bytes": len(cover_bytes)},
            )

    return {
        "stage": STAGE,
        "done": done,
        "skipped": skipped,
        "failed": failed,
        "chapters": len(records),
        "words_total": sum(c["words"] for c in chapters_out),
    }
