"""The sheet's "Category Shipped GMV / Category ASP / Category Shipped Items" blocks (Snapdeal, Tata CLiQ, ...).

Items (Mn) = category shipped GMV (INR Cr) x 10 / category ASP (INR); the total is their sum and the platform ASP
is shipped GMV x 10 / total items. Rows the sheet leaves without a par_id get one from their sheet row."""
from __future__ import annotations

GMV_BASE, ASP_BASE, ITEMS_BASE = 720000, 730000, 700000


def item_blocks(ref):
    cached = getattr(ref, "_item_blocks", False)
    if cached is not False:
        return cached
    names = {r: str(n).strip().lower() for r, n in ref.row_names.items() if n}

    def find(prefix, after=0):
        return next((r for r, n in sorted(names.items()) if r > after and n.startswith(prefix)), None)

    hg = find("category shipped gmv")
    ha = find("category asp", hg or 0) or find("category  asp", hg or 0)
    hi = find("category shipped items", ha or 0)
    out = None
    if hg and ha and hi and hg < ha < hi:
        tot = next((r for r, n in sorted(names.items()) if r > hi and n == "total"), None)
        n = (tot - hi - 1) if tot else 0
        ents = []
        if n > 0 and ha - hg > n and hi - ha > n:
            for k in range(1, n + 1):
                lab = names.get(hi + k)
                if not lab or names.get(hg + k, lab) != lab or names.get(ha + k) != lab:    # a blank GMV label is allowed
                    ents = []
                    break
                ents.append({"label": str(ref.row_names[hi + k]).strip(),
                             "grow": hg + k, "gpar": ref.row_to_par.get(hg + k) or GMV_BASE + hg + k,
                             "arow": ha + k, "apar": ref.row_to_par.get(ha + k) or ASP_BASE + ha + k,
                             "irow": hi + k, "ipar": ref.row_to_par.get(hi + k) or ITEMS_BASE + hi + k})
        if ents:
            out = {"entries": ents,
                   "total": {"row": tot, "par": ref.row_to_par.get(tot) or ITEMS_BASE + tot,
                             "label": "Total items shipped"}}
    ref._item_blocks = out
    return out
