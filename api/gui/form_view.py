"""The faults of a refused form of the owner's tabs, by field (#821, #829-3).

A refused save shows every fault at once: each beside its field, tied to it
with `aria-describedby` and marked `aria-invalid`, and all of them in a list
above the form that names the field, links to it and takes the focus
(`templates/fragments/_fehler.html`, `ts/base.ts`). The sentences come from the
domain (`core.errors.FieldErrors`) or the view; this file only orders them.
"""

# A field's key in `errors` -> the id of the input a link jumps to, and the
# field's name in the list. A key the form does not know (a conflict, a reason
# about the whole form) is listed without a link and without a name.
Fields = dict[str, tuple[str, str]]


def summary(errors: dict[str, str], fields: Fields) -> list[tuple[str, str]]:
    """(anchor, text) per fault, in the order of `errors`."""
    items = []
    for key, say in errors.items():
        if key in fields:
            anchor, name = fields[key]
            items.append((anchor, f"{name}: {say}"))
        else:
            items.append(("", say))
    return items
