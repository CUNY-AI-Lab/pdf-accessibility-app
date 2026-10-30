"""Shared font-resource traversal and pikepdf object helpers."""

from collections.abc import Iterator
from dataclasses import dataclass

import pikepdf


def _obj_key(obj) -> tuple[int, int] | None:
    """Return the (objgen) identity tuple for a pikepdf indirect object."""
    try:
        obj_num, gen_num = obj.objgen
        if isinstance(obj_num, int) and isinstance(gen_num, int) and obj_num > 0:
            return obj_num, gen_num
    except Exception:
        return None
    return None


def _resolve_dictionary(obj):
    """Resolve a pikepdf reference to a Dictionary (or dict-like) object."""
    if obj is None:
        return None
    try:
        if isinstance(obj, pikepdf.Dictionary):
            return obj
        if hasattr(obj, "keys") and hasattr(obj, "get"):
            return obj
        return obj.get_object()
    except Exception:
        return None


def _walk_pdf_resource_graph(
    pdf,
    *,
    resolve_dictionary,
    walk_resources,
    walk_appearance_object,
) -> None:
    """Compatibility entry point for callers with resource/appearance callbacks."""
    for kind, obj, _ in _iter_pdf_resource_roots(
        pdf, DEFAULT_RESOURCES, resolve_dictionary=resolve_dictionary
    ):
        if kind == "resources":
            walk_resources(obj)
        else:
            walk_appearance_object(obj)


def _is_pdf_mapping(obj) -> bool:
    return obj is not None and hasattr(obj, "get") and hasattr(obj, "keys")


def _is_form_xobject(obj) -> bool:
    """A Form XObject is a stream, not a bare dictionary (ISO 32000-1, 8.10)."""
    return isinstance(obj, pikepdf.Stream) and obj.get("/Subtype") == pikepdf.Name("/Form")


@dataclass(frozen=True)
class FontResourcePolicy:
    """Preserve the small traversal differences between existing font passes.

    Font deduplication belongs to each pass: dictionary/width repair deduplicates
    descendants, while diagnostics, embedding, and ToUnicode deduplicate parents.
    """

    mapping_fonts: bool = False
    catch_xobject_errors: bool = True
    direct_fields_share_identity: bool = True
    field_dictionaries_only: bool = False
    acroform_dictionary_only: bool = False
    include_fields: bool = True
    recurse_fields: bool = True
    annotation_children_only: bool = False
    inherit_resources: bool = False


DEFAULT_RESOURCES = FontResourcePolicy()
DIAGNOSTIC_RESOURCES = FontResourcePolicy(
    catch_xobject_errors=False,
    direct_fields_share_identity=False,
)
TOUNICODE_RESOURCES = FontResourcePolicy(
    direct_fields_share_identity=False,
    field_dictionaries_only=True,
    acroform_dictionary_only=True,
)
TAGGER_RESOURCES = FontResourcePolicy(
    mapping_fonts=True,
    catch_xobject_errors=False,
    include_fields=False,
    annotation_children_only=True,
)
ZERO_BYTE_RESOURCES = FontResourcePolicy(
    catch_xobject_errors=False,
    direct_fields_share_identity=False,
    acroform_dictionary_only=True,
    recurse_fields=False,
    inherit_resources=True,
)


def _has_embedded_font(descriptor) -> bool:
    if not isinstance(descriptor, pikepdf.Dictionary):
        return False
    return any(
        key in descriptor
        for key in (
            pikepdf.Name("/FontFile"),
            pikepdf.Name("/FontFile2"),
            pikepdf.Name("/FontFile3"),
        )
    )


def _iter_pdf_resource_roots(pdf, policy, *, resolve_dictionary=None):
    """Yield resources and appearances in the original page/AcroForm order."""
    if resolve_dictionary is None:
        resolve_dictionary = _resolve_dictionary
    seen_fields = set()

    def _walk_field(field_obj):
        field = resolve_dictionary(field_obj)
        is_field = (
            isinstance(field, pikepdf.Dictionary)
            if policy.field_dictionaries_only
            else _is_pdf_mapping(field)
        )
        if not is_field:
            return
        field_key = (
            getattr(field, "objgen", None)
            if policy.direct_fields_share_identity
            else _obj_key(field)
        )
        if field_key and field_key in seen_fields:
            return
        if field_key:
            seen_fields.add(field_key)
        resources = resolve_dictionary(field.get("/DR"))
        yield "resources", resources, None
        yield "appearance", field.get("/AP"), resources
        if policy.recurse_fields:
            kids = field.get("/Kids")
            if isinstance(kids, pikepdf.Array):
                for kid in kids:
                    yield from _walk_field(kid)

    for page in pdf.pages:
        yield "resources", resolve_dictionary(page.get("/Resources")), None
        annots = page.get("/Annots")
        if isinstance(annots, pikepdf.Array):
            for annot in annots:
                if policy.annotation_children_only:
                    ap = resolve_dictionary(annot.get("/AP")) if hasattr(annot, "get") else None
                    if isinstance(ap, pikepdf.Dictionary):
                        for key in ("/N", "/R", "/D"):
                            child = ap.get(key)
                            if child is not None:
                                yield "appearance", child, None
                else:
                    fallback = (
                        resolve_dictionary(annot.get("/Resources"))
                        if policy.inherit_resources
                        else None
                    )
                    yield "appearance", annot.get("/AP"), fallback

    acroform = resolve_dictionary(pdf.Root.get("/AcroForm"))
    is_acroform = (
        isinstance(acroform, pikepdf.Dictionary)
        if policy.acroform_dictionary_only
        else _is_pdf_mapping(acroform)
    )
    if is_acroform:
        yield "resources", resolve_dictionary(acroform.get("/DR")), None
        if policy.include_fields:
            fields = acroform.get("/Fields")
            if isinstance(fields, pikepdf.Array):
                for field in fields:
                    yield from _walk_field(field)


class _FontResourceWalker:
    def __init__(self, policy, object_key):
        self.policy = policy
        self.object_key = object_key
        self.seen_resources = set()
        self.seen_appearances = set()

    def _font_mapping(self, obj):
        return (
            _is_pdf_mapping(obj)
            if self.policy.mapping_fonts
            else isinstance(obj, pikepdf.Dictionary)
        )

    def _walk_resources(self, resources) -> Iterator[object]:
        if not _is_pdf_mapping(resources):
            return
        resources_key = self.object_key(resources)
        if resources_key and resources_key in self.seen_resources:
            return
        if resources_key:
            self.seen_resources.add(resources_key)
        fonts = _resolve_dictionary(resources.get("/Font"))
        if self._font_mapping(fonts):
            for _, font_obj in fonts.items():
                font_dict = _resolve_dictionary(font_obj)
                if self._font_mapping(font_dict):
                    yield font_dict
        xobjects = _resolve_dictionary(resources.get("/XObject"))
        if not self._font_mapping(xobjects):
            return
        for _, xobject in xobjects.items():
            xobject_dict = _resolve_dictionary(xobject)
            if not _is_pdf_mapping(xobject_dict):
                continue
            if self.policy.catch_xobject_errors:
                try:
                    subtype = xobject_dict.get("/Subtype")
                except Exception:
                    continue
            else:
                subtype = xobject_dict.get("/Subtype")
            if subtype != pikepdf.Name("/Form"):
                continue
            child_resources = _resolve_dictionary(xobject_dict.get("/Resources"))
            if self.policy.inherit_resources:
                child_resources = child_resources or resources
            yield from self._walk_resources(child_resources)

    def _walk_appearance_object(self, obj, fallback_resources=None) -> Iterator[object]:
        appearance_obj = _resolve_dictionary(obj)
        if not _is_pdf_mapping(appearance_obj):
            return
        appearance_key = self.object_key(appearance_obj)
        if appearance_key and appearance_key in self.seen_appearances:
            return
        if appearance_key:
            self.seen_appearances.add(appearance_key)
        resources = _resolve_dictionary(appearance_obj.get("/Resources"))
        if self.policy.inherit_resources:
            resources = resources or fallback_resources
        yield from self._walk_resources(resources)
        for key in ("/N", "/R", "/D"):
            child = appearance_obj.get(key)
            if child is not None:
                yield from self._walk_appearance_object(child, resources)


def _iter_pdf_fonts(pdf, *, policy=DEFAULT_RESOURCES, object_key=None) -> Iterator[object]:
    """Walk font resources, Form XObjects, and annotation/field appearances.

    Indirect resource and appearance cycles are suppressed. Direct fonts are
    deliberately yielded on every encounter, as in the original passes.
    """
    if object_key is None:
        object_key = _obj_key
    walker = _FontResourceWalker(policy, object_key)
    for kind, obj, fallback in _iter_pdf_resource_roots(pdf, policy):
        if kind == "resources":
            yield from walker._walk_resources(obj)
        else:
            yield from walker._walk_appearance_object(obj, fallback)


def _font_stream_bytes(descriptor) -> bytes | None:
    if not isinstance(descriptor, pikepdf.Dictionary):
        return None
    for key in ("/FontFile", "/FontFile2", "/FontFile3"):
        stream_obj = _resolve_dictionary(descriptor.get(key))
        if stream_obj is None:
            continue
        try:
            return bytes(stream_obj.read_bytes())
        except Exception:
            continue
    return None
