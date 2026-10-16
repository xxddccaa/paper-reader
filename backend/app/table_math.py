"""Reproducible arithmetic on original structured table cells, without model math."""
from decimal import Decimal, InvalidOperation, localcontext
import re

from .source_tools import SourceError


def table_statistics(library, table_id: str, source_id: str = "main") -> dict:
    document = library.documents.get(source_id)
    tables = [t for t in document.coverage.get("tables", []) if t.get("id") == table_id] if document else []
    if len(tables) != 1:
        raise SourceError("Unknown structured table")
    rows = []
    with localcontext() as ctx:
        ctx.prec = 28
        for cells in tables[0]["rows"]:
            numeric = []
            for cell in cells:
                value = cell["text"].strip()
                if cell.get("header") or not re.fullmatch(r"[+−-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+−-]?\d{1,2})?%?", value):
                    continue
                try:
                    number = Decimal(value.rstrip("%").replace("−", "-"))
                except InvalidOperation:
                    continue
                if not number.is_finite() or abs(number) > Decimal("1e30"):
                    raise SourceError("Table number exceeds the arithmetic bound")
                numeric.append({"row": cell["row"], "column": cell["col"], "text": cell["text"], "value": str(number),
                                "unit": "%" if value.endswith("%") else "", "headers": cell.get("headers", [])})
            if not numeric or len({c["unit"] for c in numeric}) != 1:
                continue
            values = [Decimal(c["value"]) for c in numeric]
            total = sum(values, Decimal(0))
            rows.append({"row": numeric[0]["row"], "cells": numeric, "count": len(values),
                         "sum": str(total), "mean": str(total / len(values))})
        comparisons = []
        for baseline in rows:
            for current in rows:
                if baseline is current or baseline["count"] != current["count"]:
                    continue
                a, b = baseline["cells"], current["cells"]
                # This groups rows by their retained header paths, not by an
                # inferred scientific comparability verdict. Models still check
                # hardware, datasets, splits and budgets against source prose.
                if any(x["column"] != y["column"] or x["unit"] != y["unit"]
                       or not x["headers"] or x["headers"][:-1] != y["headers"][:-1] for x, y in zip(a, b)):
                    continue
                base_sum = Decimal(baseline["sum"])
                if not base_sum or any(Decimal(c["value"]) == 0 for c in a):
                    continue
                ratios = [(Decimal(y["value"]) / Decimal(x["value"]) - 1) * 100 for x, y in zip(a, b)]
                comparisons.append({"baseline_row": baseline["row"], "row": current["row"],
                    "relative_change_of_mean_percent": str((Decimal(current["sum"]) / base_sum - 1) * 100),
                    "mean_relative_change_percent": str(sum(ratios, Decimal(0)) / len(ratios))})
    return {"source_id": source_id, "table_id": table_id, "arithmetic_only": True, "rows": rows,
            "comparisons_with_matching_header_prefixes": comparisons,
            "note": "Exact cell arithmetic. A percentage of means differs from a mean of percentages. Header matching alone does not establish experimental comparability."}


def all_table_statistics(library) -> list[dict]:
    main = library.documents.get("main")
    return [table_statistics(library, t["id"]) for t in main.coverage.get("tables", [])] if main else []
