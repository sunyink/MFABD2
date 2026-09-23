"""Require the project identity statement in distributable payloads."""

import argparse
from pathlib import Path
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = "TRADEMARKS.md"


def verify_content(content: bytes) -> None:
    # Git checkouts may use CRLF on Windows and LF in Android/Linux CI.
    expected = (ROOT / DOCUMENT).read_bytes().replace(b"\r\n", b"\n")
    if not expected.strip() or content.replace(b"\r\n", b"\n") != expected:
        raise ValueError(f"Packaged {DOCUMENT} does not match the project statement")


def verify_directory(directory: Path) -> None:
    verify_content((directory / DOCUMENT).read_bytes())


def verify_archive(archive: ZipFile) -> None:
    verify_content(archive.read(DOCUMENT))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    verify_directory(args.directory)
    print(f"Project identity statement verified: {args.directory / DOCUMENT}")
