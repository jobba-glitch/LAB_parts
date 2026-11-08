#!/usr/bin/env python3
"""
lab_libgen.py - build an atomic-parts KiCad symbol library from a CSV.

Copies the base symbols (R_Small, C_Small, ...) out of KiCad's stock Device
library into the output, then writes one derived symbol per CSV row, with the
footprint and all part fields locked in. Regenerate the library whenever the
CSV changes; never hand-edit the output.

Usage:
  python lab_libgen.py --device /path/to/Device.kicad_sym \
                       --csv parts.csv --out LAB_Parts.kicad_sym

CSV columns (header names, case-insensitive for the reserved ones):
  base        required  parent symbol in Device lib, e.g. R_Small, C_Small
  value       required  e.g. 10k, 100n, 4u7
  package     required* imperial size, e.g. 0402, 0603 (*unless footprint given)
  name        optional  symbol name; auto-built if blank
  description optional  auto-built if blank
  keywords    optional  extra search keywords
  datasheet   optional  URL
  footprint   optional  overrides the package -> footprint mapping
  Any other column becomes a hidden symbol field named exactly as its header
  (e.g. Manufacturer, MPN, DigiKey_PN, Bin). Blank cells are not written.
  Tolerance, Power, Voltage and Dielectric columns are also used in auto names.
"""
import argparse
import copy
import csv
import sys

# ---------------------------------------------------------------- s-expr I/O

class Q(str):
    """A token written as a quoted string."""


def tokenize(text):
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c in "()":
            yield c
            i += 1
        elif c == '"':
            i += 1
            buf = []
            while text[i] != '"':
                if text[i] == "\\":
                    i += 1
                    buf.append("\n" if text[i] == "n" else text[i])
                else:
                    buf.append(text[i])
                i += 1
            i += 1
            yield Q("".join(buf))
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n()"':
                j += 1
            yield text[i:j]
            i = j


def parse(text):
    stack = [[]]
    for tok in tokenize(text):
        if type(tok) is str and tok == "(":
            stack.append([])
        elif type(tok) is str and tok == ")":
            node = stack.pop()
            stack[-1].append(node)
        else:
            stack[-1].append(tok)
    return stack[0][0]


def fmt_atom(a):
    if isinstance(a, Q):
        return '"' + a.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    return a


def dump(node, indent=0):
    pad = "\t" * indent
    if not any(isinstance(x, list) for x in node):
        return pad + "(" + " ".join(fmt_atom(x) for x in node) + ")"
    head, rest = [], []
    for x in node:
        if not rest and not isinstance(x, list):
            head.append(fmt_atom(x))
        else:
            rest.append(x)
    lines = [pad + "(" + " ".join(head)]
    for x in rest:
        lines.append(dump(x, indent + 1) if isinstance(x, list)
                     else "\t" * (indent + 1) + fmt_atom(x))
    lines.append(pad + ")")
    return "\n".join(lines)


def find(node, key):
    return [x for x in node if isinstance(x, list) and x and x[0] == key]

# ------------------------------------------------------------ part knowledge

METRIC = {"01005": "0402", "0201": "0603", "0402": "1005", "0603": "1608",
          "0805": "2012", "1206": "3216", "1210": "3225", "2010": "5025",
          "2512": "6332"}

# base symbol -> (footprint template, name prefix, description word)
BASES = {
    "R_Small":           ("Resistor_SMD:R_{i}_{m}Metric",  "R",   "Resistor"),
    "R_Small_US":        ("Resistor_SMD:R_{i}_{m}Metric",  "R",   "Resistor"),
    "C_Small":           ("Capacitor_SMD:C_{i}_{m}Metric", "C",   "Capacitor"),
    "L_Small":           ("Inductor_SMD:L_{i}_{m}Metric",  "L",   "Inductor"),
    "FerriteBead_Small": ("Inductor_SMD:L_{i}_{m}Metric",  "FB",  "Ferrite bead"),
    "LED_Small":         ("LED_SMD:LED_{i}_{m}Metric",     "LED", "LED"),
    "D_Small":           ("Diode_SMD:D_{i}_{m}Metric",     "D",   "Diode"),
}

RESERVED = {"base", "value", "package", "name", "description",
            "keywords", "datasheet", "footprint"}
NAME_FIELDS = ["tolerance", "power", "voltage", "dielectric"]


def clean_name(s):
    return s.replace(" ", "").replace("/", "-").replace(":", "-")

# ------------------------------------------------------------------- builder

def build_derived(row, extras, parent, base, errors, ln):
    props = {p[1]: p for p in find(parent, "property")}
    hidden_tpl = props.get("Datasheet")

    def prop(name, value, template_key=None):
        tpl = props.get(template_key or name) or hidden_tpl
        p = copy.deepcopy(tpl)
        p[1], p[2] = Q(name), Q(value)
        return p

    value, package = row["value"], row["package"]
    fp_tpl, prefix, kind = BASES.get(base, (None, base.split("_")[0], base))

    footprint = row["footprint"]
    if not footprint:
        if fp_tpl and package in METRIC:
            footprint = fp_tpl.format(i=package, m=METRIC[package])
        else:
            errors.append(f"line {ln}: no footprint column and no mapping for "
                          f"base '{base}' package '{package}'")
            return None, None

    lower = {k.lower(): row[k] for k in extras}
    spec = [lower[f] for f in NAME_FIELDS if lower.get(f)]

    name = clean_name(row["name"] or "_".join([prefix, value, package] + spec))
    desc = row["description"] or " ".join([kind, value, package] + spec)
    mpn = lower.get("mpn", "")
    keywords = " ".join(x for x in [kind.lower(), value, package,
                                    row["keywords"], mpn] if x)

    node = ["symbol", Q(name), ["extends", Q(base)]]
    node.append(prop("Reference", props["Reference"][2]))
    node.append(prop("Value", value))
    node.append(prop("Footprint", footprint))
    node.append(prop("Datasheet", row["datasheet"]))
    node.append(prop("Description", desc))
    node.append(prop("ki_keywords", keywords))
    if "ki_fp_filters" in props:
        node.append(copy.deepcopy(props["ki_fp_filters"]))
    for col in extras:
        if row[col]:
            node.append(prop(col, row[col], "Datasheet"))
    return name, node


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--device", required=True, help="KiCad stock Device.kicad_sym")
    ap.add_argument("--csv", required=True, help="parts list")
    ap.add_argument("--out", required=True, help="output .kicad_sym")
    args = ap.parse_args()

    with open(args.device, encoding="utf-8") as f:
        lib = parse(f.read())
    if lib[0] != "kicad_symbol_lib":
        sys.exit(f"{args.device} is not a KiCad symbol library")
    stock = {s[1]: s for s in find(lib, "symbol")}

    with open(args.csv, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = [h.strip() for h in reader.fieldnames]
        raw_rows = list(reader)

    extras = [h for h in headers if h.lower() not in RESERVED]
    errors, used_bases, derived, seen = [], [], [], {}

    for ln, raw in enumerate(raw_rows, start=2):
        r = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        if not any(r.values()):
            continue
        row = {k: "" for k in RESERVED}
        for k, v in r.items():
            row[k.lower() if k.lower() in RESERVED else k] = v

        base = row["base"]
        if not base or not row["value"]:
            errors.append(f"line {ln}: 'base' and 'value' are required")
            continue
        if base not in stock:
            errors.append(f"line {ln}: base symbol '{base}' not in Device library")
            continue
        parent = stock[base]
        if find(parent, "extends"):
            errors.append(f"line {ln}: '{base}' is itself derived; pick its parent")
            continue
        if not row["package"] and not row["footprint"]:
            errors.append(f"line {ln}: need 'package' or 'footprint'")
            continue

        name, node = build_derived(row, extras, parent, base, errors, ln)
        if node is None:
            continue
        if name in seen:
            errors.append(f"line {ln}: duplicate name '{name}' (first on line {seen[name]})")
            continue
        if name in stock and name not in used_bases:
            errors.append(f"line {ln}: name '{name}' collides with a base symbol")
            continue
        seen[name] = ln
        if base not in used_bases:
            used_bases.append(base)
        derived.append(node)

    if errors:
        print("Not written - fix these first:", file=sys.stderr)
        for e in errors:
            print("  " + e, file=sys.stderr)
        sys.exit(1)

    out = ["kicad_symbol_lib"] + find(lib, "version")[:1] + \
          [["generator", Q("lab_libgen")]]
    out += [stock[b] for b in used_bases] + derived

    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(dump(out) + "\n")
    print(f"Wrote {len(derived)} parts on {len(used_bases)} base symbols to {args.out}")


if __name__ == "__main__":
    main()
