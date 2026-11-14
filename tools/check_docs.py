"""Check local documentation links and SVG validity without making network requests."""

from pathlib import Path
import re
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
TEXT_EXTENSIONS = {
    ".md",
    ".txt",
    ".py",
    ".toml",
    ".json",
    ".yaml",
    ".yml",
    ".mmd",
    ".svg",
    ".mjs",
    ".js",
    ".sh",
    ".ps1",
    ".cfg",
    ".ini",
}
# Common UTF-8 bytes misread as Windows-1252/Latin-1, plus lost-character replacements.
MOJIBAKE = re.compile(
    r"\ufffd|\u00c3[\u0080-\u00bf]|\u00c2[\u0080-\u00bf]|\u00e2(?:\u20ac|[\u0080-\u00bf])|\u00f0\u0178"
)


def text_errors(root, names):
    errors = []
    for name in names:
        path = root / name
        if not path.is_file() or (
            path.suffix.lower() not in TEXT_EXTENSIONS
            and path.name
            not in {
                "LICENSE",
                "NOTICE",
                ".gitignore",
                ".gitattributes",
                ".editorconfig",
                ".prettierignore",
            }
        ):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeError:
            errors.append(name + ": invalid UTF-8 text")
            continue
        for number, line in enumerate(content.splitlines(), 1):
            if MOJIBAKE.search(line):
                errors.append(f"{name}:{number}: possible corrupted text encoding")
    return errors


def main():
    names = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode("utf-8").split("\0")
    errors = text_errors(ROOT, [name for name in names if name])
    paths = [ROOT / name for name in names if name.endswith(".md") and (ROOT / name).is_file()]
    for path in paths:
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeError:
            continue  # Tracked invalid text was reported above.
        for raw in re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", content):
            target = raw.split(' "', 1)[0].strip("<>")
            parsed = urllib.parse.urlsplit(target)
            if parsed.scheme or not parsed.path:
                continue
            local = path.parent / urllib.parse.unquote(parsed.path)
            if not local.exists():
                errors.append(str(path.relative_to(ROOT)) + ": " + target)
    for svg in [*ROOT.glob("assets/*.svg"), *ROOT.glob("docs/diagrams/*.svg")]:
        ET.parse(svg)
    if errors:
        raise RuntimeError("Documentation checks failed:\n" + "\n".join(errors))
    print("Tracked text encoding, documentation links and SVG syntax passed.")


if __name__ == "__main__":
    main()
