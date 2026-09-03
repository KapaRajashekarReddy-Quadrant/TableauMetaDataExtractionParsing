"""kpi_suggestions.py"""
from __future__ import annotations
import copy
import re
from typing import Any

def apply_kpi_suggestions_to_metadata(
    metadata: dict[str, Any],
    remap: dict[str, str] | None = None,
    remove: list[str] | None = None,
) -> dict[str, Any]:
    remap = remap or {}
    remove = remove or []

    remove_l = {n.strip().lower() for n in remove if n and str(n).strip()}
    remap_l = {
        str(k).strip().lower(): str(v).strip()
        for k, v in remap.items()
        if k and v and str(k).strip() and str(v).strip()
    }
    remove_l |= set(remap_l.keys())

    if not remove_l and not remap_l:
        return metadata

    out = copy.deepcopy(metadata)

    def map_name(name: str | None) -> str | None:
        if name is None:
            return None
        key = str(name).strip()
        if not key:
            return name
        return remap_l.get(key.lower(), name)

    def rewrite_formula(formula: str | None) -> str | None:
        if not formula:
            return formula
        text = formula
        for old, new in sorted(remap_l.items(), key=lambda kv: -len(kv[0])):
            text = re.sub(rf"\[{re.escape(old)}\]", f"[{new}]", text, flags=re.IGNORECASE)
        return text

    # calculations
    new_calcs = []
    for c in out.get("calculations") or []:
        name = (c.get("name") or "").strip()
        if name.lower() in remove_l:
            continue
        c = dict(c)
        c["name"] = map_name(name) or name
        if "formula" in c:
            c["formula"] = rewrite_formula(c.get("formula"))
        new_calcs.append(c)
    out["calculations"] = new_calcs

    # worksheets
    for ws in out.get("worksheets") or []:
        for field in ws.get("fields") or []:
            if "name" in field:
                field["name"] = map_name(field.get("name")) or field.get("name")
            if "column" in field:
                field["column"] = map_name(field.get("column")) or field.get("column")
            if "formula" in field:
                field["formula"] = rewrite_formula(field.get("formula"))

        for enc in ws.get("encodings") or []:
            if "name" in enc:
                enc["name"] = map_name(enc.get("name")) or enc.get("name")
            if "formula" in enc:
                enc["formula"] = rewrite_formula(enc.get("formula"))

        for flt in ws.get("filters") or []:
            if "name" in flt:
                flt["name"] = map_name(flt.get("name")) or flt.get("name")
            if "column" in flt:
                flt["column"] = map_name(flt.get("column")) or flt.get("column")

        for col in ws.get("columns") or []:
            if isinstance(col, dict) and "column" in col:
                col["column"] = map_name(col.get("column")) or col.get("column")

        for shelf_key in ("rows", "columnsShelf"):
            for field in ws.get(shelf_key) or []:
                if "name" in field:
                    field["name"] = map_name(field.get("name")) or field.get("name")
                if "column" in field:
                    field["column"] = map_name(field.get("column")) or field.get("column")

    return out