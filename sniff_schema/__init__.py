#!/usr/bin/env python3
"""
sniff-schema — infer schema from JSON/CSV data.
Outputs TypeScript interfaces, Pydantic models, or Markdown tables.

Usage:
    python sniff_schema.py data.json
    python sniff_schema.py data.csv --format pydantic
    cat data.json | python sniff_schema.py - --format markdown
    python sniff_schema.py api.json --sample 100 --format typescript
"""

from __future__ import annotations

import csv
import json
import sys
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, IO

import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

app = typer.Typer(help="Infer schema from JSON or CSV data.")
console = Console()

# ── Type inference ──────────────────────────────────────────────────────────

ISO_DATE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?)?$"
)

def infer_primitive(value: Any) -> str:
    """Return the most specific type name for a scalar value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        if ISO_DATE_RE.match(value):
            return "date"
        if value.lower() in ("true", "false"):
            return "boolean-string"
        try:
            int(value)
            return "integer-string"
        except ValueError:
            pass
        try:
            float(value)
            return "number-string"
        except ValueError:
            pass
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


# ── Stats accumulator ───────────────────────────────────────────────────────

@dataclass
class FieldStats:
    name: str
    seen: int = 0
    null_count: int = 0
    types: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    samples: list[Any] = field(default_factory=list)
    max_sample: int = 5

    def observe(self, value: Any) -> None:
        self.seen += 1
        t = infer_primitive(value)
        if t == "null":
            self.null_count += 1
        self.types[t] += 1
        if len(self.samples) < self.max_sample and value is not None:
            self.samples.append(value)

    @property
    def nullable(self) -> bool:
        return self.null_count > 0

    @property
    def dominant_type(self) -> str:
        if not self.types:
            return "unknown"
        # Remove null from dominant type calculation
        non_null = {k: v for k, v in self.types.items() if k != "null"}
        if not non_null:
            return "null"
        return max(non_null, key=non_null.__getitem__)

    @property
    def is_mixed(self) -> bool:
        non_null = {k: v for k, v in self.types.items() if k != "null"}
        return len(non_null) > 1

    def null_pct(self, total: int) -> float:
        return round(self.null_count / total * 100, 1) if total else 0.0


# ── Data loading ────────────────────────────────────────────────────────────

def load_records(source: str, sample: int) -> list[dict]:
    """Load up to `sample` records from a JSON or CSV source."""
    if source == "-":
        raw = sys.stdin.read().strip()
        return _parse_json_or_csv_text(raw, source="<stdin>", sample=sample)

    path = Path(source)
    if not path.exists():
        console.print(f"[red]Error:[/red] File not found: {source}")
        raise typer.Exit(1)

    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8", errors="replace")

    if suffix == ".csv":
        return _parse_csv_text(text, sample)
    if suffix in (".json", ".jsonl", ".ndjson"):
        return _parse_json_or_csv_text(text, source=source, sample=sample)

    # Auto-detect by content
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        return _parse_json_or_csv_text(text, source=source, sample=sample)
    return _parse_csv_text(text, sample)


def _parse_json_or_csv_text(text: str, source: str, sample: int) -> list[dict]:
    stripped = text.lstrip()
    try:
        data = json.loads(text)
        if isinstance(data, list):
            records = [r for r in data if isinstance(r, dict)]
        elif isinstance(data, dict):
            # Single object — treat as one record, or unwrap common wrappers
            for key in ("data", "results", "items", "records", "rows"):
                if key in data and isinstance(data[key], list):
                    records = [r for r in data[key] if isinstance(r, dict)]
                    break
            else:
                records = [data]
        else:
            console.print("[red]Error:[/red] JSON root must be an array or object.")
            raise typer.Exit(1)
    except json.JSONDecodeError:
        # Try NDJSON / JSON Lines
        records = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    records.append(obj)
            except json.JSONDecodeError:
                pass
        if not records:
            console.print(f"[red]Error:[/red] Could not parse JSON from {source}")
            raise typer.Exit(1)

    return records[:sample]


def _parse_csv_text(text: str, sample: int) -> list[dict]:
    lines = text.splitlines()
    if not lines:
        console.print("[red]Error:[/red] CSV is empty.")
        raise typer.Exit(1)

    # Sniff dialect safely
    try:
        dialect = csv.Sniffer().sniff(text[:4096])
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(lines, dialect=dialect)
    records = []
    for i, row in enumerate(reader):
        if i >= sample:
            break
        records.append(dict(row))
    return records


# ── Analysis ────────────────────────────────────────────────────────────────

def analyse(records: list[dict]) -> tuple[dict[str, FieldStats], int]:
    """Walk all records and accumulate field statistics."""
    stats: dict[str, FieldStats] = {}
    total = len(records)

    for record in records:
        for key, value in record.items():
            if key not in stats:
                stats[key] = FieldStats(name=key)
            stats[key].observe(value)

    # Mark fields not present in every row as nullable
    for key, s in stats.items():
        missing = total - s.seen
        if missing > 0:
            s.null_count += missing
            s.types["null"] = s.types.get("null", 0) + missing

    return stats, total


# ── Type mapping helpers ────────────────────────────────────────────────────

TS_MAP = {
    "string": "string", "integer": "number", "number": "number",
    "boolean": "boolean", "date": "string", "null": "null",
    "array": "unknown[]", "object": "Record<string, unknown>",
    "integer-string": "string", "number-string": "string",
    "boolean-string": "string", "unknown": "unknown",
}

PY_MAP = {
    "string": "str", "integer": "int", "number": "float",
    "boolean": "bool", "date": "str", "null": "None",
    "array": "list", "object": "dict",
    "integer-string": "str", "number-string": "str",
    "boolean-string": "str", "unknown": "Any",
}


def ts_type(s: FieldStats) -> str:
    base = TS_MAP.get(s.dominant_type, "unknown")
    if s.is_mixed:
        non_null = {k for k in s.types if k != "null"}
        parts = sorted({TS_MAP.get(t, "unknown") for t in non_null})
        base = " | ".join(parts)
    if s.nullable:
        return f"{base} | null"
    return base


def py_type(s: FieldStats) -> str:
    base = PY_MAP.get(s.dominant_type, "Any")
    if s.is_mixed:
        non_null = {k for k in s.types if k != "null"}
        parts = sorted({PY_MAP.get(t, "Any") for t in non_null})
        base = "Union[" + ", ".join(parts) + "]"
    if s.nullable:
        return f"Optional[{base}]"
    return base


# ── Output renderers ────────────────────────────────────────────────────────

def render_typescript(stats: dict[str, FieldStats], total: int) -> str:
    lines = ["interface InferredSchema {"]
    for name, s in stats.items():
        t = ts_type(s)
        optional = "?" if s.nullable else ""
        safe_name = name if re.match(r"^[a-zA-Z_$][a-zA-Z0-9_$]*$", name) else f'"{name}"'
        lines.append(f"  {safe_name}{optional}: {t};")
    lines.append("}")
    lines.append(f"\n// Inferred from {total} record(s)")
    return "\n".join(lines)


def render_pydantic(stats: dict[str, FieldStats], total: int) -> str:
    needs_any = any("Any" in py_type(s) for s in stats.values())
    needs_union = any(s.is_mixed for s in stats.values())
    needs_optional = any(s.nullable for s in stats.values())

    imports = ["from pydantic import BaseModel"]
    typing_imports = []
    if needs_any:
        typing_imports.append("Any")
    if needs_union:
        typing_imports.append("Union")
    if needs_optional:
        typing_imports.append("Optional")
    if typing_imports:
        imports.append(f"from typing import {', '.join(typing_imports)}")

    lines = imports + ["", "", "class InferredSchema(BaseModel):"]
    for name, s in stats.items():
        t = py_type(s)
        safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", name)
        default = " = None" if s.nullable else ""
        lines.append(f"    {safe_name}: {t}{default}")
    lines.append(f"\n# Inferred from {total} record(s)")
    return "\n".join(lines)


def render_markdown(stats: dict[str, FieldStats], total: int) -> str:
    lines = [
        f"# Inferred Schema\n",
        f"_Based on {total} record(s)_\n",
        "| Field | Type | Nullable | Null % | Sample Values |",
        "|-------|------|----------|--------|---------------|",
    ]
    for name, s in stats.items():
        t = s.dominant_type + (" (mixed)" if s.is_mixed else "")
        nullable = "yes" if s.nullable else "no"
        pct = f"{s.null_pct(total)}%"
        samples = ", ".join(str(v)[:30] for v in s.samples[:3])
        lines.append(f"| `{name}` | {t} | {nullable} | {pct} | {samples} |")
    return "\n".join(lines)


def render_rich_table(stats: dict[str, FieldStats], total: int) -> None:
    """Pretty terminal output (default when no --format is given)."""
    table = Table(
        title=f"Schema — {total} record(s) analysed",
        box=box.ROUNDED,
        show_lines=True,
        highlight=True,
    )
    table.add_column("Field", style="bold cyan", no_wrap=True)
    table.add_column("Type", style="green")
    table.add_column("Nullable", justify="center")
    table.add_column("Null %", justify="right")
    table.add_column("Mixed?", justify="center")
    table.add_column("Sample values", style="dim")

    for name, s in stats.items():
        nullable_str = "[yellow]yes[/yellow]" if s.nullable else "[green]no[/green]"
        mixed_str = "[red]yes[/red]" if s.is_mixed else "[green]no[/green]"
        pct = f"{s.null_pct(total)}%"
        samples = "  |  ".join(str(v)[:25] for v in s.samples[:3])
        table.add_row(name, s.dominant_type, nullable_str, pct, mixed_str, samples)

    console.print(table)
    console.print(
        "\n[dim]Tip:[/dim] Use [bold]--format typescript[/bold], "
        "[bold]pydantic[/bold], or [bold]markdown[/bold] to export.\n"
    )


# ── CLI entry point ─────────────────────────────────────────────────────────

FORMAT_CHOICES = ["typescript", "pydantic", "markdown", "table"]

@app.command()
def main(
    source: str = typer.Argument(
        ..., help="Path to JSON/CSV file, or '-' to read from stdin."
    ),
    format: str = typer.Option(
        "table",
        "--format", "-f",
        help="Output format: typescript | pydantic | markdown | table",
    ),
    sample: int = typer.Option(
        200,
        "--sample", "-s",
        help="Max records to sample (default 200).",
        min=1,
        max=100_000,
    ),
    output: str = typer.Option(
        None,
        "--output", "-o",
        help="Write output to this file instead of stdout.",
    ),
) -> None:
    """Infer schema from a JSON or CSV file and output as TypeScript, Pydantic, Markdown, or a rich table."""

    if format not in FORMAT_CHOICES:
        console.print(
            f"[red]Unknown format:[/red] {format!r}. "
            f"Choose from: {', '.join(FORMAT_CHOICES)}"
        )
        raise typer.Exit(1)

    with console.status(f"Loading [bold]{source}[/bold]…"):
        records = load_records(source, sample)

    if not records:
        console.print("[red]Error:[/red] No records found in input.")
        raise typer.Exit(1)

    console.print(f"[dim]Analysing {len(records)} record(s)…[/dim]")
    stats, total = analyse(records)

    if format == "table":
        render_rich_table(stats, total)
        return

    renderers = {
        "typescript": render_typescript,
        "pydantic": render_pydantic,
        "markdown": render_markdown,
    }
    text = renderers[format](stats, total)

    if output:
        Path(output).write_text(text, encoding="utf-8")
        console.print(f"[green]✓[/green] Written to [bold]{output}[/bold]")
    else:
        console.print(
            Panel(text, title=f"[bold]{format}[/bold]", border_style="cyan")
        )


if __name__ == "__main__":
    app()
