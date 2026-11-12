"""Learning from month to month: the model's category mix is corrected by its own past errors.

Each saved month records, per category, how far the model's history-only view was from what
was reported (the MIS) or, where nothing was reported, from what the expert said. The next
month starts from that correction, so a bias that repeats shrinks instead of repeating.

- Truth ranks MIS first, expert second. A category reported in the MIS is compared with the
  model's independent history-only share of the reported categories; a category with no MIS
  is compared with the expert's category YoY against the output's.
- The correction is a mix correction only. It is applied when categories are built from
  history, the mix is rescaled onto the platform total afterwards, so the level and the
  MoM/YoY trend are untouched.
- What is stored is the *residual* after the correction already applied, so corrections build
  up until the residual is gone, and stop there. They move half of the residual per month and
  never more than +/-10% in total.
"""

from __future__ import annotations

from typing import Any

CAP = 0.10          # largest total correction on a category
STEP = 0.50         # share of the latest residual taken each month
MIN_MONTHS = 1      # corrections start after one saved month of evidence


def corrections(past: list[dict], cap: float = CAP, step: float = STEP) -> dict[str, float]:
    """Per-category corrections for the coming month, from earlier saved months' records."""
    recs = [p["categories"] for p in past if p.get("categories")]
    if len(recs) < MIN_MONTHS:
        return {}
    last, prev = recs[-1], (recs[-2] if len(recs) > 1 else {})
    out: dict[str, float] = {}
    for key, d in last.items():
        res = [r for r in (d.get("resid"), (prev.get(key) or {}).get("resid")) if r is not None]
        applied = d.get("applied") or 0.0
        c = applied + step * (sum(res) / len(res)) if res else applied
        out[key] = max(-cap, min(cap, c))
    return out


def measure(actual, predicted, expert: dict[str, dict], applied: dict[str, float],
            platform_gap: float | None = None) -> dict[str, Any]:
    """The month's learning record.

    actual     - the run's estimate (its reported categories are the truth)
    predicted  - a run on history alone, with last month's correction already applied
    expert     - {l2 key: {"gap": float, "estimated": bool}} from the month's call
    applied    - the correction used for this month
    """
    rep = {k: v["gmv"].value for k, v in actual.l2.items()
           if v.get("gmv") is not None and v["gmv"].value and v["gmv"].provenance.is_actual}
    pred = {k: predicted.l2[k]["gmv"].value for k in rep
            if k in predicted.l2 and predicted.l2[k].get("gmv") is not None
            and predicted.l2[k]["gmv"].value}
    if not rep:        # the standard template keeps categories as shares of GMV
        a_sh, p_sh = getattr(actual, "cat_shares", {}), getattr(predicted, "cat_shares", {})
        rep = {f"par{p}": a_sh[p] for p in getattr(actual, "cat_reported", ()) if a_sh.get(p)}
        pred = {k: p_sh[int(k[3:])] for k in rep if p_sh.get(int(k[3:]))}
    tot_a, tot_p = sum(rep[k] for k in pred), sum(pred.values())
    mis_resid = {k: (rep[k] / tot_a) / (pred[k] / tot_p) - 1 for k in pred} \
        if len(pred) >= 3 and tot_a > 0 and tot_p > 0 else {}

    cats: dict[str, dict[str, Any]] = {}
    for key in set(mis_resid) | set(expert) | set(applied):
        d: dict[str, Any] = {"applied": round(applied.get(key, 0.0), 5)}
        if key in mis_resid:
            d["mis_resid"] = round(mis_resid[key], 5)
        if key in expert:
            d["expert_gap"] = round(expert[key]["gap"], 5)
        if key in mis_resid:
            d["resid"], d["src"] = d["mis_resid"], "mis"
        elif key in expert and expert[key]["estimated"]:
            d["resid"], d["src"] = d["expert_gap"], "expert"
        cats[key] = d

    ab = lambda xs: (sum(abs(x) for x in xs) / len(xs)) if xs else None
    out: dict[str, Any] = {"categories": cats,
                           "mis_cat_gap": ab(list(mis_resid.values())),
                           "expert_cat_gap": ab([e["gap"] for e in expert.values()]),
                           "n_mis": len(mis_resid), "n_expert": len(expert)}
    if platform_gap is not None:
        out["expert_platform_gap"] = round(platform_gap, 5)
    return out


def trend(past: list[dict]) -> list[dict[str, Any]]:
    """One row per saved month: how far the model was from the MIS and from the expert."""
    rows = []
    for p in past:
        if not p.get("categories") and p.get("history_gap") is None and p.get("analyst_gap") is None:
            continue
        rows.append({"month": p["month"],
                     "platform vs MIS": p.get("history_gap"),
                     "categories vs MIS": p.get("mis_cat_gap"),
                     "categories vs expert": p.get("expert_cat_gap"),
                     "platform vs expert": (abs(p["expert_platform_gap"])
                                            if p.get("expert_platform_gap") is not None else None),
                     "edited vs model": p.get("analyst_gap"),
                     "correction in use": (sum(abs(d.get("applied", 0)) for d in p["categories"].values())
                                           / max(1, len(p["categories"]))) if p.get("categories") else None})
    return rows
