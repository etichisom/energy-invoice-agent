import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
DATA = ROOT / "data"


@pytest.fixture
def data_dir() -> Path:
    return DATA


@pytest.fixture
def ground_truth() -> dict:
    rows = [json.loads(line) for line in (DATA / "ground_truth.jsonl").read_text(encoding="utf-8").splitlines()]
    return {r["invoice_id"]: r for r in rows}


class FakeAnthropic:
    """Stands in for anthropic.Anthropic: returns queued tool-use payloads and records requests."""

    def __init__(self, payloads: list[dict]):
        self.payloads = list(payloads)
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        payload = self.payloads.pop(0)
        block = SimpleNamespace(type="tool_use", id=f"toolu_{len(self.requests)}", name="record_invoice", input=payload)
        return SimpleNamespace(content=[block], usage=SimpleNamespace(input_tokens=900, output_tokens=150))


@pytest.fixture
def fake_anthropic():
    return FakeAnthropic
