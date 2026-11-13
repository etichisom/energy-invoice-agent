from invoice_agent.extractors import CascadeExtractor, ClaudeExtractor, HeuristicExtractor
from invoice_agent.text import pdf_to_text


def _pdf(data_dir, invoice_id, tenant="acme-steel"):
    return data_dir / "tenants" / tenant / "invoices" / f"{invoice_id}.pdf"


def _truth_payload(gt: dict) -> dict:
    return {k: v for k, v in gt.items() if k not in ("tenant", "invoice_id", "expected_issues")}


def test_heuristic_reads_simple_layouts(data_dir, ground_truth):
    gt = ground_truth["voltaris-03-001"]
    inv = HeuristicExtractor()(pdf_to_text(_pdf(data_dir, "voltaris-03-001"))).invoice
    assert inv.total_incl_vat == gt["total_incl_vat"]
    assert inv.grid_fees == gt["grid_fees"]
    assert inv.ean == gt["ean"]


def test_claude_extractor_forces_tool_and_normalises_strings(fake_anthropic, ground_truth):
    payload = _truth_payload(ground_truth["gridhub-03-013"])
    payload["energy_cost"] = "6.045,68"  # model returned a localised string
    payload["ean"] = "5414 1661 3186 0913 94"
    client = fake_anthropic([payload])
    ex = ClaudeExtractor(client=client, model="test-model")("invoice text")
    req = client.requests[0]
    assert req["tool_choice"] == {"type": "tool", "name": "record_invoice"}
    assert ex.invoice.energy_cost == 6045.68
    assert ex.invoice.ean == "541416613186091394"
    assert ex.attempts == 1 and ex.input_tokens == 900


def test_claude_extractor_retries_once_with_validator_feedback(fake_anthropic, ground_truth):
    good = _truth_payload(ground_truth["gridhub-03-013"])
    bad = {**good, "energy_cost": good["consumption_kwh"]}  # band misread -> implausible price
    client = fake_anthropic([bad, good])
    ex = ClaudeExtractor(client=client, model="test-model")("invoice text")
    assert ex.attempts == 2
    assert ex.invoice.energy_cost == good["energy_cost"]
    feedback = client.requests[1]["messages"][-1]["content"][0]
    assert feedback["type"] == "tool_result" and "EUR/kWh" in feedback["content"]


def test_claude_extractor_does_not_retry_on_genuine_billing_error(fake_anthropic, ground_truth):
    # supplier's totals really do not add up: that is a finding, not a misread, so no retry
    gt = next(g for g in ground_truth.values() if g["expected_issues"] == ["totals_do_not_add_up"])
    client = fake_anthropic([_truth_payload(gt)])
    ex = ClaudeExtractor(client=client, model="test-model")("invoice text")
    assert ex.attempts == 1


def test_cascade_skips_model_when_deterministic_result_checks_out(data_dir, fake_anthropic):
    client = fake_anthropic([])
    ex = CascadeExtractor(ClaudeExtractor(client=client))(pdf_to_text(_pdf(data_dir, "nordwatt-03-005")))
    assert not ex.used_model and client.requests == []


def test_cascade_escalates_messy_layout_to_model(data_dir, fake_anthropic, ground_truth):
    client = fake_anthropic([_truth_payload(ground_truth["gridhub-03-013"])])
    ex = CascadeExtractor(ClaudeExtractor(client=client))(pdf_to_text(_pdf(data_dir, "gridhub-03-013")))
    assert ex.used_model and len(client.requests) == 1
    assert ex.invoice.consumption_kwh == ground_truth["gridhub-03-013"]["consumption_kwh"]
