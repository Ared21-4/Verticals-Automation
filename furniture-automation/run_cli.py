"""Command-line runner for a monthly estimation run.

    python run_cli.py --player nykaa --month 2026-09 \
        --reference "Reference sheet and Final output sheet.xlsx" \
        --mis "Nykaa MIS.xlsx" \
        --transcript "Nykaa_BPC_September_Expert_Call_Insights.docx"

Runs the same pipeline the web app uses, so the engine can be exercised and
scripted without the interface.
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from furniture_intel.domain import Severity, month_label
from furniture_intel.pipeline import list_players, load_player_config, run_month


def _run_all(a, extra) -> int:
    """Run every runnable player in the workbook, one folder each."""
    rows = [r for r in list_players(a.reference) if r.get("usable", True)]
    print(f"\nRunning {len(rows)} players for {month_label(a.month)}\n")
    print(f"{'player':<24}{'gross GMV':>13}{'shipped':>12}{'AOV':>10}"
          f"{'err':>5}{'warn':>6}  outputs")
    print("-" * 86)
    failed = 0
    for r in rows:
        name = r["name"]
        try:
            res = run_month(player=name, month=a.month, reference_path=a.reference,
                            mis_path=None, transcript_path=None, out_dir=a.out,
                            extra_sources=extra, seasonality_mode=a.seasonality,
                            save_history=a.save_history)
        except Exception as e:
            print(f"{name:<24}FAILED: {type(e).__name__}: {e}")
            failed += 1
            continue
        if res.estimate is None:
            print(f"{name:<24}{'blocked':>13}  "
                  f"{'; '.join(f.code for f in res.errors)[:40]}")
            failed += 1
            continue
        P = load_player_config(name, a.reference)["platform_rows"]

        def fmt(key):
            v = res.estimate.num(P.get(key, -1))
            return f"{v:,.1f}" if v is not None else "n/a"

        print(f"{name:<24}{fmt('gross_gmv'):>13}{fmt('shipped_gmv'):>12}{fmt('aov'):>10}"
              f"{len(res.errors):>5}{len(res.warnings):>6}  "
              f"{Path(res.outputs['deliverable']).parent.name}")
    print(f"\n{len(rows) - failed} of {len(rows)} completed. Output under {a.out}\\\n")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Monthly BPC player estimation run")
    ap.add_argument("--player", default="nykaa",
                    help="player name or sheet, e.g. nykaa, Purplle, Mamaearth. "
                         "Use --list-players to see what a workbook contains.")
    ap.add_argument("--list-players", action="store_true",
                    help="list the players in the reference workbook and exit")
    ap.add_argument("--all-players", action="store_true",
                    help="run every runnable player in the reference workbook")
    ap.add_argument("--month", required=True, help="reporting month as YYYY-MM, e.g. 2026-09")
    ap.add_argument("--reference", default=None, help="reference workbook (default: the built-in copy)")
    ap.add_argument("--mis", help="player MIS workbook path")
    ap.add_argument("--transcript", help="expert call transcript (.docx)")
    ap.add_argument("--traffic", help="traffic data workbook")
    ap.add_argument("--logistics", help="logistics / shipped-volume workbook")
    ap.add_argument("--social", help="social media data workbook")
    ap.add_argument("--out", default="outputs", help="output directory")
    ap.add_argument("--seasonality", default="auto", choices=["auto", "bau", "festive"],
                    help="auto: suppress the calendar factor when the call shows the sale "
                         "event moved out of this month; bau: always suppress; "
                         "festive: always apply")
    ap.add_argument("--save-history", action="store_true",
                    help="save the run as that month's history so next month builds on it")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    if a.list_players:
        rows = list_players(a.reference)
        print(f"\n{'player':<24}{'sheet':<26}{'metrics':>8}{'data to':>10}  "
              f"{'tuned':<7}runnable")
        print("-" * 86)
        for r in rows:
            print(f"{r['name']:<24}{str(r.get('sheet') or ''):<26}"
                  f"{str(r.get('metrics') or ''):>8}"
                  f"{month_label(r['latest_month']) if r.get('latest_month') else '':>10}  "
                  f"{'yes' if r.get('configured') else '-':<7}"
                  f"{'yes' if r.get('usable', True) else 'no (' + r.get('missing_core', '') + ')'}")
        print(f"\n{len(rows)} players, {sum(1 for r in rows if r.get('usable', True))} runnable\n")
        return 0

    extra = {k: v for k, v in (("traffic", a.traffic), ("logistics", a.logistics),
                               ("social", a.social)) if v}

    if a.all_players:
        return _run_all(a, extra)

    res = run_month(
        player=a.player, month=a.month, reference_path=a.reference,
        mis_path=a.mis, transcript_path=a.transcript,
        out_dir=a.out, extra_sources=extra, seasonality_mode=a.seasonality,
        save_history=a.save_history,
    )

    print(f"\n{'=' * 78}")
    print(f"  {a.player.title()} — {month_label(a.month)}   run {res.run_id}")
    print(f"{'=' * 78}")

    if not res.estimate:
        print("\nRUN BLOCKED. Fix the following before re-running:\n")
        for f in res.errors:
            print(f"  [{f.code}] {f.message}")
            if f.detail:
                print(f"      {f.detail}")
        return 2

    cfg_rows = res.estimate.rows
    from furniture_intel.pipeline import load_player_config
    P = load_player_config(a.player)["platform_rows"]

    print("\nHEADLINE")
    for label, key, unit in (("Shipped GMV", "shipped_gmv", "INR Cr"),
                             ("Gross GMV", "gross_gmv", "INR Cr"),
                             ("Net GMV", "net_gmv", "INR Cr"),
                             ("Cancellation", "cancellation_pct", "%"),
                             ("Returns", "returns_pct", "%"),
                             ("ASP", "asp", "INR"),
                             ("AOV", "aov", "INR"),
                             ("Basket size", "basket_size", "#"),
                             ("Gross transactions", "gross_transactions", "Mn"),
                             ("MTU", "mtu", "Mn")):
        v = cfg_rows.get(P[key])
        if v is None or v.is_null:
            print(f"  {label:<20} unavailable")
            continue
        shown = f"{v.value * 100:,.2f}%" if unit == "%" else f"{v.value:,.2f}"
        print(f"  {label:<20} {shown:>14}  [{v.provenance.value}]")

    errs = res.errors
    warns = res.warnings
    print(f"\nFINDINGS  errors={len(errs)}  warnings={len(warns)}  "
          f"confidence={res.weights.confidence.value}")
    if not a.quiet:
        for f in errs + warns:
            print(f"  [{f.severity.value:<7}] {f.code}: {f.message}")

    if res.corroboration and res.corroboration.checks:
        print("\nINDEPENDENT CROSS-CHECKS OF THE MIS")
        for c in res.corroboration.checks:
            print(f"  {c.target:<22} call implies {c.expected:>9,.1f} Cr  |  "
                  f"MIS {c.actual:>9,.1f} Cr  |  {c.deviation:+7.2%}  {c.verdict}")

    if res.calibration and res.calibration.implied:
        c = res.calibration
        print(f"\nCALIBRATION  {c.n} categories  MAPE {c.mape:.1%}  "
              f"signed {c.mpe:+.1%}  ({c.systematic})")

    print("\nOUTPUTS")
    for k, v in res.outputs.items():
        print(f"  {k:<14} {v}")
    print()
    return 0 if res.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

