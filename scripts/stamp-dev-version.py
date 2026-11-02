"""Turn the version in Cargo.toml into a unique development version, like `0.1.0.dev42`.

The release workflow runs this on every push to main, so each push can go to TestPyPI
(which, like PyPI, never accepts the same version twice). Tagged releases skip it.

    python3 scripts/stamp-dev-version.py 42

Cargo.toml holds the version in Rust's SemVer form (`0.1.0`, or `0.2.0-alpha.1`); maturin
turns it into the Python form (`0.1.0`, `0.2.0a1`). We append `-dev42` (or `.dev42` after a
pre-release part), which is valid in both: `0.1.0-dev42` becomes `0.1.0.dev42`.
Cargo.lock records the version of our own crates too, so it is updated the same way;
otherwise `cargo build --locked` would fail.
"""

import re
import sys
import tomllib
from pathlib import Path

OUR_CRATES = ["djox_python", "djox-core", "djox"]


def main() -> None:
    build_number = sys.argv[1]
    old_version = tomllib.loads(Path("Cargo.toml").read_text())["workspace"]["package"]["version"]
    separator = "." if "-" in old_version else "-"
    new_version = f"{old_version}{separator}dev{build_number}"

    replace_once(Path("Cargo.toml"), "[workspace.package]\nversion = ", old_version, new_version)
    for crate in OUR_CRATES:
        replace_once(Path("Cargo.lock"), f'name = "{crate}"\nversion = ', old_version, new_version)
    print(new_version)


def replace_once(path: Path, prefix: str, old_version: str, new_version: str) -> None:
    """Replace `prefix"old_version"` with `prefix"new_version"` in a file, and fail if it is not there."""
    # Bytes, not text, so Windows line endings (\r\n) are kept as they are; the pattern accepts both.
    text = path.read_bytes().decode()
    pattern = r"\r?\n".join(re.escape(line) for line in prefix.split("\n")) + re.escape(f'"{old_version}"')
    matches = re.findall(pattern, text)
    if len(matches) != 1:
        sys.exit(f"Expected {prefix}{old_version!r} once in {path}, found it {len(matches)} times")
    new_text = re.sub(pattern, lambda match: match.group(0).replace(old_version, new_version), text)
    path.write_bytes(new_text.encode())


if __name__ == "__main__":
    main()
