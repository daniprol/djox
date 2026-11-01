"""The examples included in the docs run and print what the docs say."""

import importlib.util
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).parents[2] / "docs" / "examples"


async def test_quickstart(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    specification = importlib.util.spec_from_file_location("quickstart", EXAMPLES / "quickstart.py")
    assert specification is not None
    assert specification.loader is not None
    quickstart = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(quickstart)

    await quickstart.main(tmp_path / "quickstart.sqlite3")

    assert capsys.readouterr().out == "[('Dune',), ('Emma',)]\n(3,)\n"
