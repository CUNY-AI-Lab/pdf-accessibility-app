"""Give each drawn Form XObject its own stream before inserting MCIDs.

ISO 32000-1, 14.7.2 incorporates form content into the structure tree;
14.7.4.2 identifies it by /Stm and a stream-local MCID. A form containing
MCIDs cannot be invoked more than once (PDF/UA-1, 7.20). Reused forms need
independent streams, including when reused through an enclosing form.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import pikepdf


@contextmanager
def unique_form_instances(source: Path) -> Iterator[Path]:
    """Copy called forms and resources, giving repeated calls distinct names.

    Measurements and tagging read the same normalized input, so even two
    placements of a shared form have their own glyphs in page coordinates.
    Images, fonts and other resources remain shared; rendering is unchanged.
    """
    with pikepdf.open(source) as pdf:
        changed = False

        def visit(owner, resources, ancestors):
            nonlocal changed
            instructions = list(pikepdf.parse_content_stream(owner))
            resources = pikepdf.Dictionary(resources or {})
            xobjects = pikepdf.Dictionary(resources.get("/XObject") or {})
            resources.XObject = xobjects
            used = set()
            touched = False
            for index, instr in enumerate(instructions):
                if str(instr.operator) != "Do" or not instr.operands:
                    continue
                name = str(instr.operands[0])
                form = xobjects.get(name)
                if not isinstance(form, pikepdf.Stream) or form.get("/Subtype") != "/Form":
                    continue
                # Cyclic forms are malformed. Leave the call as an artifact
                # instead of recursing indefinitely.
                if form.objgen in ancestors:
                    continue
                clone = pdf.make_stream(form.read_bytes())
                for key, value in form.items():
                    if key not in {"/Length", "/Filter", "/DecodeParms", "/StructParents"}:
                        clone[key] = value
                visit(clone, form.get("/Resources", resources), ancestors | {form.objgen})
                if name in used:
                    suffix = 1
                    while (new_name := f"{name}_tag{suffix}") in xobjects:
                        suffix += 1
                    name = new_name
                    instructions[index] = pikepdf.ContentStreamInstruction(
                        [pikepdf.Name(name)], pikepdf.Operator("Do")
                    )
                used.add(name)
                xobjects[name] = clone
                touched = changed = True
            if touched or not isinstance(owner, pikepdf.Page):
                owner.Resources = resources
                data = pikepdf.unparse_content_stream(instructions)
                if isinstance(owner, pikepdf.Page):
                    owner.Contents = pdf.make_stream(data)
                else:
                    owner.write(data)

        for page in pdf.pages:
            visit(page, page.get("/Resources"), set())
        if changed:
            with TemporaryDirectory() as directory:
                path = Path(directory) / "forms.pdf"
                pdf.save(path)
                yield path
        else:
            yield source
