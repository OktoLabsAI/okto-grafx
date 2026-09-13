"""Build trusted table-defined reference procedures; no arbitrary Python code."""

import re

from okto_grafx.extensions import ExtensionRegistry, TabularProcedure

if __package__:
    from tools.tck_values import reference_key, reference_value
else:
    from tck_values import reference_key, reference_value

_SIGNATURE = re.compile(r"there exists a procedure ([A-Za-z_][\w.]*)\((.*?)\) :: \((.*?)\)\s*:\Z")
_FIELD = re.compile(r"([A-Za-z_]\w*)\s*::\s*(INTEGER|STRING|FLOAT|BOOLEAN|NUMBER)\?\Z")
_TYPES = {"INTEGER": "INT64", "STRING": "STRING", "FLOAT": "DOUBLE", "BOOLEAN": "BOOL", "NUMBER": "NUMBER"}


def _fields(text):
    fields = []
    for part in text.split(",") if text.strip() else ():
        match = _FIELD.fullmatch(part.strip())
        if not match:
            raise ValueError("Unadmitted reference procedure signature")
        fields.append((match[1], _TYPES[match[2]]))
    return tuple(fields)


def _implementation(rows, input_width):
    frozen = tuple(rows)

    def invoke(*arguments):
        key = reference_key(arguments)
        return (row[input_width:] for row in frozen if reference_key(row[:input_width]) == key)

    return invoke


def procedure_registry(case):
    """Preflight every fixture signature and row before opening the database."""
    procedures = []
    names = set()
    operation_seen = False
    for step in case.get("steps", ()):
        if step["text"] in {"executing query:", "executing control query:"}:
            operation_seen = True
        if not step["text"].startswith("there exists a procedure "):
            continue
        if operation_seen:
            raise ValueError("Reference procedure must be declared before operation under test")
        match = _SIGNATURE.fullmatch(step["text"])
        if not match:
            raise ValueError("Unadmitted reference procedure declaration")
        inputs, outputs = _fields(match[2]), _fields(match[3])
        if match[1] in names:
            raise ValueError("Duplicate reference procedure declaration")
        names.add(match[1])
        argument = step.get("argument", {})
        if "dataTable" not in argument:
            raise ValueError("Reference procedure requires an explicit fixture table")
        table = [[cell["value"] for cell in row["cells"]] for row in argument["dataTable"]["rows"]]
        if not table or tuple(table[0]) != tuple(name for name, _ in inputs + outputs):
            raise ValueError("Reference procedure table header differs from its signature")
        rows = []
        for row in table[1:]:
            if len(row) != len(inputs) + len(outputs):
                raise ValueError("Reference procedure fixture row has incorrect width")
            values = tuple(reference_value(cell) for cell in row)
            expected = {"INT64": (int,), "STRING": (str,), "DOUBLE": (float,), "BOOL": (bool,), "NUMBER": (int, float)}
            if any(value is not None and type(value) not in expected[kind]
                   for value, (_, kind) in zip(values, inputs + outputs, strict=True)):
                raise ValueError("Reference procedure fixture value differs from declared type")
            rows.append(values)
        if not outputs and (inputs or rows):
            raise ValueError("Unit reference fixture must be a no-argument empty table")
        procedures.append(TabularProcedure(match[1], tuple(kind for _, kind in inputs), outputs,
                                             _implementation(rows, len(inputs)) if outputs else _unit_implementation,
                                             argument_names=tuple(name for name, _kind in inputs)))
    return ExtensionRegistry(trusted=True, procedures=tuple(procedures)) if procedures else None


def _unit_implementation():
    """Implement the reference no-op unit procedure without inventing a result row."""
    return None
