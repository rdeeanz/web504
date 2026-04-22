#!/usr/bin/env python3
"""
AlternativeTo.net Local JSON Parser
=====================================
Parses all Firecrawl-scraped JSON files from the database-source/ directory
and extracts structured software data from each file's markdown content.

Handles two file classes:
  1. *_about_.json  — individual software detail pages (primary target)
  2. Listing/browse pages — skipped or lightly parsed for slug discovery

Output: alternativeto_parsed.json

Usage:
    python parse_alternativeto.py
    python parse_alternativeto.py --input-dir ./database-source --output alternativeto_parsed.json
    python parse_alternativeto.py --about-only          # skip non-about pages
    python parse_alternativeto.py --verbose             # show per-file info
"""

import json
import os
import re
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
DEFAULT_INPUT_DIR = "./database-source"
DEFAULT_OUTPUT    = "alternativeto_parsed.json"


# ═══════════════════════════════════════════════════════════════
#  MARKDOWN SECTION EXTRACTOR
# ═══════════════════════════════════════════════════════════════

def _extract_section_items(markdown: str, section_heading: str) -> List[str]:
    """
    Extracts bullet-list items that appear under a markdown heading.

    Looks for patterns like:
        #### Cost / License
        - Pay once
        - Proprietary

    Returns a list of clean text strings.
    """
    # Build a case-insensitive pattern matching the section heading
    # then capture everything until the next #### heading or double newline gap
    pattern = re.compile(
        rf"####\s*{re.escape(section_heading)}\s*\n((?:[-*]\s*.+\n?)+)",
        re.IGNORECASE,
    )
    match = pattern.search(markdown)
    if not match:
        return []

    block = match.group(1)
    items = []
    for line in block.splitlines():
        line = line.strip()
        if line.startswith("- ") or line.startswith("* "):
            item = line[2:].strip()
            # Strip image markdown first: ![alt](url) — must precede link stripping
            item = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", item).strip()
            # Strip markdown link syntax: [text](url) → text
            item = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", item).strip()
            if item:
                items.append(item)
    return items


def _extract_heading_section_items(markdown: str, heading_variants: List[str]) -> List[str]:
    """Try multiple heading name variants and return the first match found."""
    for heading in heading_variants:
        items = _extract_section_items(markdown, heading)
        if items:
            return items
    return []


def _extract_name(markdown: str, metadata: dict) -> Optional[str]:
    """Extract software name from H1 or og:title."""
    # H1 is the most reliable: # SoftwareName
    h1_match = re.search(r"^#\s+(.+)$", markdown, re.MULTILINE)
    if h1_match:
        return h1_match.group(1).strip()

    # Fallback: parse og:title (format: "SoftwareName: tagline | AlternativeTo")
    og_title = metadata.get("og:title") or metadata.get("ogTitle") or metadata.get("title", "")
    if og_title:
        # Strip " | AlternativeTo" suffix and tagline after ":"
        name = og_title.split("|")[0].split(":")[0].strip()
        if name:
            return name

    return None


def _extract_description(markdown: str, metadata: dict) -> Optional[str]:
    """
    Extract the software description.

    Priority:
      1. "What is X?" section (most complete, official description)
      2. Paragraph immediately after the H1 + likes line
      3. meta description
    """
    # 1. "What is SoftwareName?" section — find the heading then collect lines
    lines = markdown.splitlines()
    what_is_idx = None
    for i, line in enumerate(lines):
        if re.match(r"##\s+What is .+\?", line, re.IGNORECASE):
            what_is_idx = i
            break

    if what_is_idx is not None:
        desc_lines = []
        for line in lines[what_is_idx + 1:]:
            # Stop at the next ## or ### heading
            if re.match(r"##", line):
                break
            desc_lines.append(line)
        desc = " ".join(desc_lines).strip()
        desc = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", desc)
        desc = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", desc)
        desc = re.sub(r"\s+", " ", desc).strip()
        if len(desc) > 30:
            return desc

    # 2. Short paragraph right after H1 + likes line (safe line-by-line)
    h1_idx = None
    for i, line in enumerate(lines):
        if re.match(r"^#\s+", line):
            h1_idx = i
            break
    if h1_idx is not None:
        # Skip blank lines and the "N likes" line
        para_lines = []
        past_likes = False
        for line in lines[h1_idx + 1:]:
            stripped = line.strip()
            if not past_likes:
                if re.match(r"^\d+\s+likes$", stripped, re.IGNORECASE):
                    past_likes = True
                continue
            # Stop at section markers
            if re.match(r"^####", stripped) or re.match(r"^!\[", stripped):
                break
            if stripped:
                para_lines.append(stripped)
            elif para_lines:  # blank line after we have content = end of paragraph
                break
        if para_lines:
            desc = " ".join(para_lines).strip()
            desc = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", desc)
            desc = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", desc)
            desc = re.sub(r"\s+", " ", desc).strip()
            if len(desc) > 30:
                return desc

    # 3. meta description
    desc = (
        metadata.get("description")
        or metadata.get("og:description")
        or metadata.get("ogDescription")
        or ""
    ).strip()
    return desc or None


def _extract_official_link(markdown: str) -> Optional[str]:
    """
    Extract the official website URL.

    Looks for the "Official Links" section pattern:
        ## Official Links
        [Official Website<display>](https://example.com "...")
    """
    # Pattern 1: explicit "Official Website" link text
    match = re.search(
        r"##\s+Official Links\s*\n+\[Official Website[^\]]*\]\(([^)\s\"]+)",
        markdown,
        re.IGNORECASE,
    )
    if match:
        return match.group(1)

    # Pattern 2: any link in the Official Links section
    match = re.search(
        r"##\s+Official Links\s*\n+\[([^\]]+)\]\(([^)\s\"]+)",
        markdown,
        re.IGNORECASE,
    )
    if match:
        url = match.group(2)
        # Filter out internal alternativeto.net links
        if "alternativeto.net" not in url:
            return url

    return None


def _extract_appstore_links(markdown: str) -> List[dict]:
    """
    Extract app store and download links.
    Looks for known store patterns in the markdown links.
    """
    store_patterns = {
        "App Store":        r"apps\.apple\.com",
        "Google Play":      r"play\.google\.com",
        "Microsoft Store":  r"microsoft\.com/store|apps\.microsoft\.com",
        "GitHub":           r"github\.com",
        "F-Droid":          r"f-droid\.org",
        "SourceForge":      r"sourceforge\.net",
        "Steam":            r"store\.steampowered\.com",
        "Product Hunt":     r"producthunt\.com",
        "Flathub":          r"flathub\.org",
        "Snap Store":       r"snapcraft\.io",
    }

    # Find all markdown links: [text](url)
    all_links = re.findall(r"\[([^\]]*)\]\(([^)\s\"]+)", markdown)
    results = []
    seen_urls = set()

    for text, url in all_links:
        if url in seen_urls:
            continue
        for store_name, pattern in store_patterns.items():
            if re.search(pattern, url, re.IGNORECASE):
                results.append({"store": store_name, "url": url, "label": text.strip()})
                seen_urls.add(url)
                break

    return results


def _extract_social_links(markdown: str) -> dict:
    """Extract social network URLs from the Social Networks section."""
    social = {}
    section_match = re.search(
        r"###\s+Social Networks\s*\n+((?:\[.+\]\(.+\)\s*)+)",
        markdown,
        re.IGNORECASE,
    )
    if not section_match:
        return social

    block = section_match.group(1)
    platforms = {
        "facebook":  r"facebook\.com",
        "twitter":   r"(?:twitter|x)\.com",
        "instagram": r"instagram\.com",
        "linkedin":  r"linkedin\.com",
        "youtube":   r"youtube\.com",
        "discord":   r"discord\.(?:com|gg)",
        "reddit":    r"reddit\.com",
        "mastodon":  r"mastodon\.",
        "bluesky":   r"bsky\.app",
    }
    links = re.findall(r"\[([^\]]*)\]\(([^)\s\"]+)", block)
    for _, url in links:
        for platform, pattern in platforms.items():
            if re.search(pattern, url, re.IGNORECASE):
                social[platform] = url
                break

    return social


def _extract_developer(markdown: str) -> Optional[str]:
    """Extract developer/company name from the 'Developed by' section."""
    match = re.search(
        r"####\s+Developed by\s*\n+(?:!\[[^\]]*\]\([^)]*\))?\s*([^\n\[]+)",
        markdown,
        re.IGNORECASE,
    )
    if match:
        dev = match.group(1).strip()
        # Strip trailing markdown link artifacts
        dev = re.sub(r"\[([^\]]+)\].*", r"\1", dev).strip()
        return dev or None
    return None


def _extract_pricing(markdown: str) -> Optional[str]:
    """Extract pricing sentence from the 'Pricing' section."""
    match = re.search(
        r"####\s+Pricing\s*\n+([^\n]+)",
        markdown,
        re.IGNORECASE,
    )
    if match:
        pricing = match.group(1).strip()
        # Clean markdown bold markers
        pricing = re.sub(r"\*\*([^*]+)\*\*", r"\1", pricing)
        return pricing or None
    return None


def _extract_rating(markdown: str) -> Optional[str]:
    """Extract numeric rating (e.g. '4.4') from the page."""
    # Pattern: "Average rating of4.4" or "4.4\n\nVery Good"
    match = re.search(r"Average rating of\s*([\d.]+)", markdown, re.IGNORECASE)
    if match:
        return match.group(1)
    # Pattern: standalone rating near reviews count
    match = re.search(r"([\d.]+)\n+(?:Very Good|Good|Average|Poor)", markdown)
    if match:
        return match.group(1)
    return None


def _extract_likes(markdown: str) -> Optional[int]:
    """Extract number of likes."""
    match = re.search(r"(\d+)\s+likes", markdown, re.IGNORECASE)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            pass
    return None


def _extract_alternatives_count(markdown: str) -> Optional[int]:
    """Extract how many alternatives are listed."""
    match = re.search(r"(\d+)\s*alternatives listed", markdown, re.IGNORECASE)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            pass
    return None


def _extract_categories(markdown: str) -> List[str]:
    """Extract AlternativeTo categories from the categories section."""
    match = re.search(
        r"####\s+AlternativeToCategories\s*\n+([^\n]+)",
        markdown,
        re.IGNORECASE,
    )
    if not match:
        # Try without the ####
        match = re.search(
            r"AlternativeTo\s*Categories\s*\n+([^\n]+)",
            markdown,
            re.IGNORECASE,
        )
    if match:
        cats_line = match.group(1)
        # Extract text from markdown links [Category Name](url)
        cats = re.findall(r"\[([^\]]+)\]\([^)]*\)", cats_line)
        if cats:
            return cats
        # Plain text, comma-separated
        return [c.strip() for c in cats_line.split(",") if c.strip()]
    return []


def _extract_features(markdown: str) -> List[str]:
    """Extract feature list items."""
    # Features are numbered lists under ### Features
    match = re.search(
        r"###\s+Features\s*\n+((?:\d+\.\s*.+\n?)+)",
        markdown,
        re.IGNORECASE,
    )
    if not match:
        return []
    block = match.group(1)
    features = []
    for line in block.splitlines():
        m = re.match(r"\d+\.\s+(.+)", line.strip())
        if m:
            features.append(m.group(1).strip())
    return features


def _extract_tags(markdown: str) -> List[str]:
    """Extract tags."""
    match = re.search(
        r"###\s+Tags\s*\n+((?:[-*]\s*\[.+\]\([^)]*\)\s*\n?)+)",
        markdown,
        re.IGNORECASE,
    )
    if not match:
        return []
    block = match.group(1)
    tags = re.findall(r"\[([^\]]+)\]\(", block)
    return tags


def _extract_languages(markdown: str) -> List[str]:
    """Extract supported languages (safe line-by-line to avoid backtracking)."""
    lines = markdown.splitlines()
    start_idx = None
    for i, line in enumerate(lines):
        if re.match(r"####\s+Supported Languages", line, re.IGNORECASE):
            start_idx = i
            break
    if start_idx is None:
        return []
    langs = []
    for line in lines[start_idx + 1:]:
        stripped = line.strip()
        if re.match(r"####", stripped):
            break
        lang = stripped.lstrip("-* ").strip()
        if lang and lang not in langs:
            langs.append(lang)
    return langs


def _extract_popular_alternatives(markdown: str) -> List[str]:
    """Extract popular alternatives listed on the page (safe line-by-line)."""
    lines = markdown.splitlines()
    start_idx = None
    for i, line in enumerate(lines):
        if re.match(r"####\s+Popular alternatives", line, re.IGNORECASE):
            start_idx = i
            break
    if start_idx is None:
        return []
    alts = []
    for line in lines[start_idx + 1:]:
        if line.strip().startswith("[View all]") or re.match(r"####|###|##", line.strip()):
            break
        # Extract slugs from links like /software/<slug>/about/
        found = re.findall(r"/software/([^/]+)/about/", line)
        for slug in found:
            if slug not in alts:
                alts.append(slug)
    return alts


def _extract_source_url(metadata: dict) -> Optional[str]:
    """Get the canonical source URL."""
    return (
        metadata.get("sourceURL")
        or metadata.get("og:url")
        or metadata.get("ogUrl")
        or metadata.get("url")
        or None
    )


def _is_about_page(metadata: dict, filename: str) -> bool:
    """Determine if this JSON file is an individual software /about/ page."""
    url = _extract_source_url(metadata) or ""
    if "/software/" in url and "/about/" in url:
        return True
    # Fall back to filename heuristic
    if "_software_" in filename and "_about_" in filename:
        return True
    return False


# ═══════════════════════════════════════════════════════════════
#  MAIN PARSER
# ═══════════════════════════════════════════════════════════════

def parse_file(filepath: Path, verbose: bool = False) -> Optional[dict]:
    """
    Parse a single Firecrawl JSON file and return a structured dict.
    Returns None if the file cannot be parsed or is not an about page.
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        if verbose:
            print(f"  [SKIP] Cannot read {filepath.name}: {e}")
        return None

    markdown = raw.get("markdown", "")
    metadata = raw.get("metadata", {})

    if not markdown:
        if verbose:
            print(f"  [SKIP] No markdown content: {filepath.name}")
        return None

    source_url = _extract_source_url(metadata)
    filename   = filepath.name

    # ── Structured extraction ─────────────────────────────────
    name = _extract_name(markdown, metadata)

    data = {
        "name":               name,
        "slug":               _slug_from_url(source_url or filename),
        "description":        _extract_description(markdown, metadata),
        "cost_license":       _extract_heading_section_items(markdown, [
                                  "Cost / License", "Cost/License", "License"
                              ]),
        "application_types":  _extract_heading_section_items(markdown, [
                                  "Application types", "Application type",
                                  "Application Types", "Application Type",
                              ]),
        "origin":             _extract_heading_section_items(markdown, ["Origin"]),
        "platforms":          _extract_heading_section_items(markdown, ["Platforms", "Platform"]),
        "categories":         _extract_categories(markdown),
        "features":           _extract_features(markdown),
        "tags":               _extract_tags(markdown),
        "supported_languages": _extract_languages(markdown),
        "developer":          _extract_developer(markdown),
        "pricing":            _extract_pricing(markdown),
        "rating":             _extract_rating(markdown),
        "likes":              _extract_likes(markdown),
        "alternatives_count": _extract_alternatives_count(markdown),
        "popular_alternatives": _extract_popular_alternatives(markdown),
        "official_website":   _extract_official_link(markdown),
        "appstore_links":     _extract_appstore_links(markdown),
        "social_links":       _extract_social_links(markdown),
        "og_image":           metadata.get("ogImage") or metadata.get("og:image"),
        "source_url":         source_url,
        "source_file":        filename,
        "parsed_at":          datetime.now(timezone.utc).isoformat(),
    }

    if verbose:
        print(f"  [OK]   {filename} → {name or '(no name)'}")

    return data


def _slug_from_url(source: str) -> Optional[str]:
    """Extract the software slug from a URL or filename."""
    # From URL: /software/<slug>/about/
    url_match = re.search(r"/software/([^/]+)/", source)
    if url_match:
        return url_match.group(1)
    # From filename: alternativeto.net_software_<slug>_about_.json
    fn_match = re.search(r"_software_([^_]+(?:_[^_]+)*)_about_", source)
    if fn_match:
        return fn_match.group(1).replace("_", "-")
    return None


def parse_all(
    input_dir: str,
    output_file: str,
    about_only: bool = False,
    verbose: bool = False,
) -> None:
    source_dir = Path(input_dir)
    if not source_dir.exists():
        print(f"ERROR: Input directory not found: {input_dir}", file=sys.stderr)
        sys.exit(1)

    json_files = sorted(source_dir.glob("*.json"))
    print(f"Found {len(json_files)} JSON files in {input_dir}")

    results   = []
    skipped   = 0
    errors    = 0

    for filepath in json_files:
        # Quick pre-filter by filename
        if about_only and "_about_" not in filepath.name:
            skipped += 1
            continue

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            print(f"  [ERR]  {filepath.name}: {e}")
            errors += 1
            continue

        metadata = raw.get("metadata", {})

        # Filter to about pages if requested
        if about_only and not _is_about_page(metadata, filepath.name):
            skipped += 1
            continue

        result = parse_file(filepath, verbose=verbose)
        if result:
            # Only include entries that have at least a name
            if result.get("name"):
                results.append(result)
            else:
                skipped += 1
        else:
            skipped += 1

    # Sort by name for readability
    results.sort(key=lambda x: (x.get("name") or "").lower())

    output_path = Path(output_file)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print(f"✓ Parsed:  {len(results)} software entries")
    print(f"✓ Skipped: {skipped} files (no name / filtered)")
    print(f"✓ Errors:  {errors} files")
    print(f"✓ Output:  {output_path.resolve()}")
    print(f"{'='*60}")


# ═══════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parse AlternativeTo.net Firecrawl JSON files into structured data."
    )
    parser.add_argument(
        "--input-dir", "-i",
        default=DEFAULT_INPUT_DIR,
        help=f"Directory containing raw JSON files (default: {DEFAULT_INPUT_DIR})",
    )
    parser.add_argument(
        "--output", "-o",
        default=DEFAULT_OUTPUT,
        help=f"Output JSON file path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--about-only",
        action="store_true",
        help="Only process individual software /about/ pages",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print per-file parsing status",
    )
    args = parser.parse_args()

    parse_all(
        input_dir=args.input_dir,
        output_file=args.output,
        about_only=args.about_only,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
