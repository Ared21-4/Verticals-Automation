"""Pre-flight validation of the uploaded inputs.

Runs before any estimate is produced. Errors block the run; warnings do not,
but they are carried into the audit sheet. Nothing here repairs data.
"""

from __future__ import annotations

from ..domain import Finding, Severity, month_label, shift_months
from .mis_adapter import headline_check
from ..readers.mis import MisData
from ..readers.reference import ReferenceModel
from ..readers.transcript import TranscriptData


def validate_inputs(
    cfg: dict,
    month: str,
    ref: ReferenceModel | None,
    mis: MisData | None,
    transcript: TranscriptData | None,
) -> list[Finding]:
    f: list[Finding] = []
    label = month_label(month)

    # ---- reference workbook ------------------------------------------------
    if ref is None:
        f.append(Finding("REF_MISSING", Severity.ERROR,
                         "No reference workbook supplied.",
                         "The reference workbook defines the output format and the metric "
                         "dependencies. The run cannot proceed without it.", blocking=True))
        return f

    if not ref.months:
        f.append(Finding("REF_NO_MONTHS", Severity.ERROR,
                         f"No month columns found in sheet '{ref.model_sheet}'.",
                         "Check header_row and first_month_col in the player configuration.",
                         blocking=True))
        return f

    if not ref.has_month(month):
        last = ref.months[-1]
        if shift_months(last, 1) == month:
            # The normal case for a new month: history runs up to the month before.
            f.append(Finding("NEW_MONTH", Severity.INFO,
                             f"{label} is a new month; history runs to {month_label(last)}.",
                             "The estimate is built from history through "
                             f"{month_label(last)}. Save this run to history afterwards so the "
                             "next month can build on it."))
        elif month > last:
            f.append(Finding(
                "REF_HISTORY_GAP", Severity.ERROR,
                f"History ends at {month_label(last)}, so {label} is more than one month "
                "ahead.",
                f"Run and save {month_label(shift_months(last, 1))} first. Estimating several "
                "months ahead compounds error, so the system will not skip months.",
                blocking=True))
        else:
            f.append(Finding(
                "REF_MONTH_ABSENT", Severity.ERROR,
                f"{label} has no data in '{ref.model_sheet}', which covers "
                f"{month_label(ref.months[0])}–{month_label(last)} with a gap here.",
                "A month inside the covered range cannot be rebuilt from a gap.",
                blocking=True))

    prior = shift_months(month, -1)
    if not ref.has_month(prior):
        f.append(Finding("REF_PRIOR_ABSENT", Severity.WARNING,
                         f"Prior month {month_label(prior)} is absent; MoM checks are unavailable.",
                         metric="mom"))

    yoy_month = shift_months(month, -12)
    if not ref.has_month(yoy_month):
        f.append(Finding("REF_YOY_ABSENT", Severity.WARNING,
                         f"Year-ago month {month_label(yoy_month)} is absent; "
                         "YoY and calibration against the call are unavailable.", metric="yoy"))

    hist = len([m for m in ref.months if m < month])
    if hist < 15:
        f.append(Finding("REF_SHORT_HISTORY", Severity.WARNING,
                         f"Only {hist} historical months before {label}.",
                         "Seasonal factors need at least two prior years to be meaningful; "
                         "estimator confidence is reduced."))

    if not ref.template:
        f.append(Finding("REF_NO_TEMPLATE", Severity.ERROR,
                         f"Output template "
                         f"'{cfg['reference'].get('output_template_file') or cfg['reference'].get('output_template_sheet')}' not found or empty.",
                         blocking=True))
    else:
        unmapped = [t for t in ref.template if t.par_id not in ref.par_to_row]
        if unmapped:
            f.append(Finding("TEMPLATE_PARTIAL", Severity.INFO,
                             f"{len(unmapped)} of {len(ref.template)} template parameters have no "
                             f"row in '{ref.model_sheet}'.",
                             "These are emitted as null rather than estimated. "
                             + ", ".join(f"{t.par_id} {t.parameter_name}" for t in unmapped[:8])
                             + (" …" if len(unmapped) > 8 else "")))

    # ---- MIS ---------------------------------------------------------------
    if mis is None:
        f.append(Finding("MIS_MISSING", Severity.WARNING,
                         "No player MIS supplied.",
                         "Weight redistributes to the remaining sources and confidence falls. "
                         "Reported actuals will be unavailable for this month."))
    else:
        if month not in mis.months:
            f.append(Finding("MIS_MONTH_MISMATCH", Severity.ERROR,
                             f"MIS contains {', '.join(month_label(m) for m in mis.months)} "
                             f"but the selected reporting month is {label}.",
                             "Select the matching month or upload the correct MIS.",
                             blocking=True))
        else:
            missing = mis.missing_metrics(month)
            if missing:
                f.append(Finding("MIS_BLANKS", Severity.WARNING,
                                 f"{len(missing)} MIS metrics are blank for {label}.",
                                 ", ".join(f"{s} / {m}" for s, m in missing)
                                 + ". These are left unavailable, not defaulted to zero."))

            secs = cfg.get("mis_sections", {})
            names = cfg.get("mis_metric_names", {})
            cat_gmv = mis.section_values(month, secs.get("category_gmv", "Category GMV"))
            shipped = mis.value(month, names.get("shipped_gmv", "Shipped GMV"),
                                secs.get("overall", "Overall"))
            if cat_gmv and shipped:
                total = sum(cat_gmv.values())
                drift = abs(total / shipped - 1)
                sev = Severity.INFO if drift < 0.005 else (
                    Severity.WARNING if drift < 0.02 else Severity.ERROR)
                f.append(Finding(
                    "MIS_CATEGORY_RECONCILIATION", sev,
                    f"MIS category GMV total {total:,.1f} vs reported Shipped GMV "
                    f"{shipped:,.1f} ({drift:+.2%}).",
                    "Category values reconcile to the platform total."
                    if sev is Severity.INFO else
                    "Category values do not reconcile to the reported platform total.",
                    metric="shipped_gmv"))

            lay = _layout_mismatch(cfg, mis, month)
            if lay:
                f.append(lay)

            hv, hp, hs, hb = headline_check(mis, month, cfg, ref)
            if hv and hp:
                ratio = hv / hp
                msg = (f"The MIS shows {hv:,.1f} Cr of platform GMV against {hp:,.1f} Cr last "
                       f"month ({ratio:.2f}x).")
                how = ("A swing this large usually means another player's MIS or the wrong month. "
                       "Check the file belongs to the selected player.")
                lb = cfg.get("mis_level_bounds") or {}
                stop_lo, stop_hi = lb.get("stop", [0.2, 5.0])
                warn_lo, warn_hi = lb.get("warn", [0.33, 3.0])
                if not stop_lo <= ratio <= stop_hi:
                    f.append(Finding("MIS_LEVEL_MISMATCH", Severity.ERROR, msg,
                                     how + " The run is stopped rather than report this level.",
                                     metric="gross_gmv", blocking=True))
                elif not warn_lo <= ratio <= warn_hi:
                    f.append(Finding("MIS_LEVEL_MISMATCH", Severity.WARNING, msg, how,
                                     metric="gross_gmv"))

            cat_asp = mis.section_values(month, secs.get("category_asp", "Category ASP"))
            only_gmv = set(cat_gmv) - set(cat_asp)
            only_asp = set(cat_asp) - set(cat_gmv)
            if only_gmv or only_asp:
                f.append(Finding("MIS_CATEGORY_ASYMMETRY", Severity.WARNING,
                                 "MIS category GMV and ASP lists do not match.",
                                 f"GMV only: {sorted(only_gmv)}; ASP only: {sorted(only_asp)}."))

            geo = mis.section_values(month, secs.get("geography", "Geographic Split"))
            if geo:
                s = sum(geo.values())
                if abs(s - 1) > 1e-6:
                    norm = ", ".join(f"{k} {v / s:.1%}" for k, v in sorted(geo.items()))
                    f.append(Finding(
                        "MIS_GEO_SUM", Severity.ERROR,
                        f"MIS geographic split sums to {s:.4f}, not 1 "
                        f"({', '.join(f'{k} {v:.0%}' for k, v in sorted(geo.items()))}).",
                        "The MIS is internally inconsistent here, so the shares are exported as "
                        "supplied rather than silently rescaled. Either correct the MIS, or "
                        f"override the three shares to their normalised values ({norm}). "
                        "Normalising changes a reported figure and so is left as an explicit "
                        "analyst decision.",
                        metric="geo_split"))

            pay = mis.section_values(month, secs.get("payment", "Payment Split"))
            if pay and abs(sum(pay.values()) - 1) > 1e-6:
                f.append(Finding("MIS_PAYMENT_SUM", Severity.WARNING,
                                 f"MIS payment split sums to {sum(pay.values()):.4f}, not 1.",
                                 metric="payment_split"))

            for name, c in mis.section_cells(month, secs.get("overall", "Overall")).items():
                if c.value is not None and c.value < 0:
                    f.append(Finding("MIS_NEGATIVE", Severity.ERROR,
                                     f"MIS '{name}' is negative ({c.value}).", metric=name))

    # ---- transcript --------------------------------------------------------
    if transcript is None:
        f.append(Finding("CALL_MISSING", Severity.WARNING,
                         "No expert call transcript supplied.",
                         "Weight redistributes to the remaining sources, no category "
                         "corroboration is possible and confidence falls."))
    else:
        if not transcript.platform_gmv_given:
            f.append(Finding("CALL_NO_PLATFORM_GMV", Severity.INFO,
                             "The expert gave no platform GMV, MoM or YoY figure.",
                             "No platform-level number is attributed to the call. The platform "
                             "estimate rests on the MIS and the model's own history."))
        if transcript.low_confidence:
            f.append(Finding("CALL_LOW_CONFIDENCE", Severity.WARNING,
                             "Transcription confidence is low.",
                             "Extracted figures are discounted."))
        elif transcript.transcription_quality == "mixed":
            f.append(Finding("CALL_MIXED_QUALITY", Severity.INFO,
                             f"Transcript flags {len(transcript.clarifications)} garbled or "
                             "inconsistent passages.",
                             "Affected figures carry low confidence and are listed in the "
                             "clarifications log."))
        if not transcript.category_yoy:
            f.append(Finding("CALL_NO_CATEGORY_YOY", Severity.WARNING,
                             "No category YoY figures were extracted from the call."))

    return f


def blocking(findings: list[Finding]) -> list[Finding]:
    return [x for x in findings if x.blocking]


def _norm(s: object) -> str:
    return " ".join(str(s).replace("\u2019", "'").strip().lower().split())


def _layout_mismatch(cfg: dict, mis: MisData, month: str) -> Finding | None:
    """Catch a MIS built on a different template, such as another player's.

    Each player's MIS names its categories. If most of the non-zero ones are unknown to
    the template this player is read with, the file is not this player's, and reading it
    would produce confident-looking nonsense.
    """
    secs = cfg.get("mis_sections", {})
    cells = mis.section_cells(month, secs.get("category_gmv", "Category GMV"))
    named = [c.metric for c in cells.values() if c.value]
    if len(named) < 3:
        return None
    if cfg.get("mis_taxonomy_map"):                       # tuned player: Myntra's own list
        known = {_norm(k) for k in cfg["mis_taxonomy_map"] if not k.startswith("_")}
        tmpl = "this player's category list"
    else:
        known = {_norm(k) for k in (cfg.get("mis_generic") or {}).get("categories", {})
                 if not k.startswith("_")}
        tmpl = "the standard BPC MIS template"
    miss = [n for n in named if _norm(n) not in known]
    if len(miss) / len(named) < 0.5:
        return None
    return Finding(
        "MIS_LAYOUT_MISMATCH", Severity.ERROR,
        f"{len(miss)} of {len(named)} categories in this MIS are not in {tmpl} "
        f"(e.g. {', '.join(miss[:4])}).",
        "This looks like another player's MIS or a different template. The run is stopped "
        "rather than read it. Check the file belongs to the selected player.",
        metric="category_gmv", blocking=True)
