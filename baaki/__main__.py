"""CLI: python -m baaki <command>"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from . import audit as audit_mod
from .data import generate, save
from .domain import rupees
from .runner import Faults, run_all

console = Console()
ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"


def cmd_generate(a):
    debtors, invoices = generate(a.seed)
    path = ROOT / "data" / "ledger.json"
    save(path, debtors, invoices)
    console.print(f"[green]wrote[/] {path} — {len(debtors)} debtors, {len(invoices)} invoices, {rupees(sum(i.amount_paise for i in invoices))} receivable")


LLM_KEYS = {
    "claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    "openai": ("OPENAI_API_KEY",),
}


def cmd_run(a):
    keys = LLM_KEYS.get(a.brain)
    if keys and not any(os.environ.get(k) for k in keys):
        console.print(f"[yellow]No {keys[0]} set — {a.brain} decisions will fall back to the rules brain (this is the graceful-degradation path, not a crash).[/]")
    faults = Faults(razorpay_fail_creates=2, llm_outage_days={3}, rogue_day=2) if a.demo_faults else Faults()
    results = run_all(REPORTS, brain=a.brain, horizon=a.days, seed=a.seed, faults=faults, llm_effort=a.effort, model=a.model)
    print_summary(results)
    write_markdown(results, a.brain)


def print_summary(results):
    runs = results["runs"]
    t = Table(title="Recovery over the horizon — same ledger, same debtors, three strategies")
    t.add_column("metric")
    for m in ("none", "naive", "agent"):
        t.add_column(m, justify="right")
    rows = [
        ("recovered", lambda r: r["recovered"]),
        ("recovery rate", lambda r: f"{r['recovery_rate_pct']}%"),
        ("via Razorpay link", lambda r: rupees(r["recovered_via_link_paise"])),
        ("via human (escalations)", lambda r: rupees(r["recovered_via_human_paise"])),
        ("discount written off", lambda r: rupees(r["discount_written_off_paise"])),
        ("contacts sent", lambda r: str(r["contacts_sent"])),
        ("contacts / ₹1L recovered", lambda r: str(r["contacts_per_lakh_recovered"])),
        ("median days to cash", lambda r: str(r["median_days_to_cash"])),
        ("compliance violations", lambda r: str(r["policy_violations_unenforced"])),
        ("policy denials (blocked)", lambda r: str(r["policy_denials"])),
        ("gateway failures handled", lambda r: str(r["gateway_failures_handled"])),
        ("escalated / stopped", lambda r: f"{r['invoices_by_status'].get('escalated', 0)} / {r['invoices_by_status'].get('stopped', 0)}"),
        ("paid in full", lambda r: str(r["invoices_by_status"].get("paid", 0))),
    ]
    for name, fn in rows:
        t.add_row(name, *[fn(runs[m]) for m in ("none", "naive", "agent")])
    console.print(t)
    rm = results["risk_model"]
    h = rm["holdout"]
    console.print(f"risk model (held-out {h['n']} invoices, base rate {rm['base_rate_holdout']}): precision {h['precision']} recall {h['recall']} f1 {h['f1']} @0.5")
    llm = runs["agent"].get("llm")
    if llm:
        console.print(f"LLM: {llm['provider']}/{llm['model']} calls={llm['calls']} fallbacks={llm['fallbacks']} tokens in/out={llm['input_tokens']}/{llm['output_tokens']} cache_read={llm['cache_read_tokens']}")
    if runs["agent"].get("llm_unavailable"):
        console.print(f"[yellow]{runs['agent']['llm_unavailable']} — numbers below are the rules brain's.[/]")
    ex = runs["agent"]["exceptions"]
    console.print(f"[bold]{len(ex)} exceptions for a human[/] (top 5 by outstanding):")
    for e in ex[:5]:
        console.print(f"  {e['invoice']} {e['debtor']:<28} {e['outstanding']:>14}  {e['status']:<9} {e['reason']}")


def write_markdown(results, brain):
    runs = results["runs"]
    rm = results["risk_model"]
    a, n, z = runs["agent"], runs["naive"], runs["none"]
    lines = [
        f"# Baaki run report (brain: {brain})",
        "",
        f"Ledger: {a['invoices']} overdue invoices across {a['debtors']} debtors, {a['total_receivable']} receivable, {a['horizon_days']}-day horizon.",
        "",
        "| metric | do nothing | naive reminders | Baaki agent |",
        "|---|---:|---:|---:|",
        f"| recovered | {z['recovered']} | {n['recovered']} | **{a['recovered']}** |",
        f"| recovery rate | {z['recovery_rate_pct']}% | {n['recovery_rate_pct']}% | **{a['recovery_rate_pct']}%** |",
        f"| via Razorpay link | – | – | {rupees(a['recovered_via_link_paise'])} |",
        f"| via human after escalation | – | – | {rupees(a['recovered_via_human_paise'])} |",
        f"| discount written off | – | – | {rupees(a['discount_written_off_paise'])} |",
        f"| contacts sent | {z['contacts_sent']} | {n['contacts_sent']} | {a['contacts_sent']} |",
        f"| contacts per ₹1L recovered | – | {n['contacts_per_lakh_recovered']} | {a['contacts_per_lakh_recovered']} |",
        f"| compliance violations | 0 | {n['policy_violations_unenforced']} | {a['policy_violations_unenforced']} |",
        f"| out-of-policy proposals blocked | – | – | {a['policy_denials']} |",
        f"| gateway failures handled | – | – | {a['gateway_failures_handled']} |",
        f"| escalated / stopped for a human | – | – | {a['invoices_by_status'].get('escalated', 0)} / {a['invoices_by_status'].get('stopped', 0)} |",
        "",
        f"Incremental recovery vs do-nothing: **{rupees(a['recovered_paise'] - z['recovered_paise'])}**; vs naive: **{rupees(a['recovered_paise'] - n['recovered_paise'])}**.",
        "",
        "## Risk model (held-out)",
        f"Label: {rm['label']}. Split: {rm['split']}. Holdout n={rm['holdout']['n']}, base rate {rm['base_rate_holdout']}.",
        "",
        "| threshold | precision | recall | F1 | TP/FP/FN/TN |",
        "|---|---:|---:|---:|---|",
    ]
    for k in ("holdout", "holdout_at_0.7"):
        h = rm[k]
        lines.append(f"| {h['threshold']} | {h['precision']} | {h['recall']} | {h['f1']} | {h['tp']}/{h['fp']}/{h['fn']}/{h['tn']} |")
    lines += ["", "## Exception list (needs a human)", "", "| invoice | debtor | outstanding | status | reason |", "|---|---|---:|---|---|"]
    for e in a["exceptions"]:
        lines.append(f"| {e['invoice']} | {e['debtor']} | {e['outstanding']} | {e['status']} | {e['reason']} |")
    if a.get("llm"):
        l = a["llm"]
        lines += ["", "## LLM usage", f"{l['provider']} model `{l['model']}`, {l['calls']} calls, {l['fallbacks']} fallbacks to rules, {l['input_tokens']} in / {l['output_tokens']} out tokens ({l['cache_read_tokens']} cache reads)."]
        if l["errors"]:
            lines += ["", "Fallback causes:", *[f"- {e}" for e in l["errors"]]]
    lines += ["", f"Decision sources: {a['decision_sources']}"]
    p = REPORTS / f"REPORT_{brain}.md"
    p.write_text("\n".join(lines) + "\n")
    console.print(f"[green]report[/] {p}")


def cmd_audit(a):
    ok, msg = audit_mod.verify(Path(a.path))
    console.print(("[green]OK[/] " if ok else "[red]FAIL[/] ") + msg)
    sys.exit(0 if ok else 1)


def cmd_serve(a):
    import uvicorn

    uvicorn.run("baaki.server:app", host="127.0.0.1", port=a.port, reload=False)


def main(argv=None):
    p = argparse.ArgumentParser(prog="baaki", description="Bounded, auditable AI receivables recovery for Indian SMEs")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="write the synthetic ledger to data/ledger.json")
    g.add_argument("--seed", type=int, default=7)
    g.set_defaults(fn=cmd_generate)

    r = sub.add_parser("run", help="run do-nothing, naive and agent strategies; write reports/")
    r.add_argument("--brain", choices=["rules", "claude", "openai"], default="rules")
    r.add_argument("--model", default=None, help="override the provider default (e.g. gpt-5.1, claude-opus-5)")
    r.add_argument("--days", type=int, default=45)
    r.add_argument("--seed", type=int, default=7)
    r.add_argument("--effort", default="low", help="reasoning effort (low|medium|high)")
    r.add_argument("--demo-faults", action="store_true", help="inject a Razorpay outage, an LLM outage and a rogue decision")
    r.set_defaults(fn=cmd_run)

    v = sub.add_parser("audit", help="verify an audit log's hash chain")
    v.add_argument("path")
    v.set_defaults(fn=cmd_audit)

    s = sub.add_parser("serve", help="dashboard + Razorpay webhook receiver")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_serve)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
