from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from vlytics.mirror.backfill import BackfillScope
from vlytics.mirror.parser import KovoParser
from vlytics.mirror.source_registry import SourceRegistryError, load_kovo_parser

SCOPE = BackfillScope("kovo", "001", "023", "201")


def _write_registry(path: Path, *, duplicate: bool = False) -> None:
    franchise_id = uuid4()
    mapping = f'''[[franchises]]
source = "kovo"
group_code = "001"
season_code = "023"
team_code = "T001"
franchise_id = "{franchise_id}"
mapping_version = "reviewed-test-v1"
evidence = "synthetic reviewed fixture"
'''
    path.write_text(
        f"""schema_version = "1.0"
{mapping}
{mapping if duplicate else ""}
[[season_rules]]
source = "kovo"
group_code = "001"
season_code = "023"
mapping_version = "reviewed-rules-test-v1"
evidence = "synthetic reviewed fixture"
regular_set_target = 25
deciding_set_target = 15
winning_margin = 2
sets_to_win = 3
maximum_sets = 5
""",
        encoding="utf-8",
    )


def test_source_registry_loads_exact_reviewed_scope(tmp_path: Path) -> None:
    path = tmp_path / "source.toml"
    _write_registry(path)

    parser = load_kovo_parser(path, (SCOPE,))

    assert isinstance(parser, KovoParser)


def test_source_registry_fails_closed_when_scope_has_no_reviewed_mapping(tmp_path: Path) -> None:
    path = tmp_path / "source.toml"
    path.write_text(
        'schema_version = "1.0"\nfranchises = []\nseason_rules = []\n',
        encoding="utf-8",
    )

    with pytest.raises(SourceRegistryError, match="franchise mapping"):
        load_kovo_parser(path, (SCOPE,))


def test_source_registry_rejects_duplicate_exact_identity(tmp_path: Path) -> None:
    path = tmp_path / "source.toml"
    _write_registry(path, duplicate=True)

    with pytest.raises(SourceRegistryError, match="duplicate franchise"):
        load_kovo_parser(path, (SCOPE,))
