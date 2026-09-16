import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
from store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "silo")
