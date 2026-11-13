import copy
import json

from invoice_agent import evals


def test_field_scoring_tolerances():
    assert evals.field_correct("energy_cost", 100.004, 100.0)
    assert not evals.field_correct("energy_cost", 100.02, 100.0)
    assert evals.field_correct("consumption_kwh", 1000.4, 1000)
    assert evals.field_correct("supplier", "gridhub ", "GridHub")
    assert not evals.field_correct("ean", None, "541452601815908306")


def test_heuristic_meets_committed_baseline():
    """The CI gate: a change that makes the deterministic path worse fails the build."""
    current = evals.run("heuristic")
    baseline = json.loads((evals.ROOT / "evals" / "baseline_heuristic.json").read_text())
    assert evals.compare(current, baseline, max_drop=0.0) == []
    assert current["headline"]["silent_errors"] == 0
    assert current["headline"]["billing_errors_reaching_review"] == 1.0


def test_gate_catches_a_regression():
    baseline = json.loads((evals.ROOT / "evals" / "baseline_heuristic.json").read_text())
    worse = copy.deepcopy(baseline)
    worse["headline"]["field_accuracy"] -= 0.05
    worse["headline"]["silent_errors"] += 1
    worse["per_supplier"]["NordWatt Supply"]["field_accuracy"] -= 0.1
    problems = evals.compare(worse, baseline, max_drop=0.02)
    assert any("field_accuracy" in p for p in problems)
    assert any("silent_errors" in p for p in problems)
    assert any("NordWatt" in p for p in problems)


def test_eval_scores_llm_run_with_cost(fake_anthropic, ground_truth):
    payloads = [{k: v for k, v in g.items() if k not in ("tenant", "invoice_id", "expected_issues")}
                for g in list(ground_truth.values())[:4]]
    res = evals.run("claude", model="test-model", limit=4, client=fake_anthropic(payloads))
    assert res["headline"]["field_accuracy"] == 1.0
    assert res["cost"]["model_calls"] == 4 and res["cost"]["input_tokens"] == 3600
