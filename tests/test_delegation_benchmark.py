from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "delegation_benchmark.py"


@pytest.mark.parametrize("lines", [None, 12, 0])
def test_stage_sizes_document_and_keeps_evidence_lines(tmp_path, lines):
    spec = spec_from_file_location("delegation_benchmark", SCRIPT)
    bench = module_from_spec(spec)
    spec.loader.exec_module(bench)
    bench.stage(tmp_path / "w", **({} if lines is None else {"document_lines": lines}))
    text = (tmp_path / "w" / "relay.md").read_text(encoding="utf-8").splitlines()
    assert "frame payload" in text[3].lower() and "rollback" in text[10].lower()
    assert sum("Synthetic relay node" in line for line in text) == (360 if lines is None else lines)
