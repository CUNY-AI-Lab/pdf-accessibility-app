"""Build structure elements, MCIDs, annotations and the ParentTree."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pikepdf

from app.pipeline.tagger_geometry import _as_positive_int, _obj_key
from app.pipeline.tagger_link_labels import _infer_link_contents
from app.pipeline.tagger_tables import (
    _complete_table_grid_cells,
    _set_list_numbering_attr,
    _table_summary_text,
)
from app.services.pdf_objects import pdf_text


def _source_key(source_element: dict[str, Any]) -> Any:
    """The structure element an element's content goes to: its own, or, for
    text Docling continues on a later page, the one it continues
    (continued_ref, see structure)."""
    return source_element.get("continued_ref") or id(source_element)


class StructTreeBuilder:
    """Manages MCID allocation, StructElem creation, and ParentTree construction."""

    def __init__(self, pdf: pikepdf.Pdf, doc_lang: str = "en"):
        self.pdf = pdf
        self.doc_lang = doc_lang.lower().strip()
        self.struct_tree_root = None
        self.doc_elem = None
        self._content_owner_mcid_counter: dict[tuple[Any, ...], int] = {}
        self._content_owner_entries: dict[
            tuple[Any, ...],
            dict[str, Any],
        ] = {}
        self._content_owner_order = 0
        self._struct_parents_counter = 0
        self._object_parent_entries: list[tuple[pikepdf.Object, pikepdf.Object]] = []
        self._source_element_structs: dict[Any, pikepdf.Object] = {}
        self._headings: list[dict] = []
        self._struct_elems_created = 0
        # The chosen reading-order position (see _page_reading_order) of the
        # element whose content is being tagged, and that of each element
        # added to /Document, so a page's elements can be put in reading
        # order (see order_page_kids).
        self.reading_order: float | None = None
        self._reading_order_of: dict[tuple[int, int], float] = {}
        self._link_structs: dict[tuple[int, int], pikepdf.Object] = {}
        self._list_cache: dict[str, tuple[pikepdf.Object, pikepdf.Object | None]] = {}
        self._note_counter = 0
        self._table_counter = 0
        self._fragmented_table_cache: dict[int, dict[tuple[int, int], pikepdf.Object]] = {}
        self._toc_cache: dict[str, pikepdf.Object] = {}

    @property
    def headings(self) -> list[dict]:
        """The heading entries recorded for bookmark planning."""
        return list(self._headings)

    @property
    def struct_elems_created(self) -> int:
        """Number of structure elements created by this builder."""
        return self._struct_elems_created

    @contextmanager
    def at_reading_order(self, position: float | None) -> Iterator[None]:
        """Scope the position used by nested structure allocations.

        A source element can allocate several children (table cells, list
        bodies or links). All inherit its position, including in Form
        XObjects; restore the caller's position even if allocation fails.
        """
        previous = self.reading_order
        self.reading_order = position
        try:
            yield
        finally:
            self.reading_order = previous

    def setup(self):
        """Create StructTreeRoot and Document element."""
        self.struct_tree_root = self.pdf.make_indirect(pikepdf.Dictionary({
            "/Type": pikepdf.Name("/StructTreeRoot"),
            "/K": pikepdf.Array([]),
            "/ParentTree": self.pdf.make_indirect(pikepdf.Dictionary({
                "/Nums": pikepdf.Array([]),
            })),
        }))
        self.pdf.Root["/StructTreeRoot"] = self.struct_tree_root

        self.doc_elem = self.pdf.make_indirect(pikepdf.Dictionary({
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Document"),
            "/P": self.struct_tree_root,
            "/K": pikepdf.Array([]),
        }))
        self.struct_tree_root["/K"] = pikepdf.Array([self.doc_elem])

    def _content_owner_key(self, owner: pikepdf.Object) -> tuple[Any, ...]:
        key = _obj_key(owner)
        if key is not None:
            return ("obj", *key)
        return ("direct", id(owner))

    def _alloc_mcid(self, owner: pikepdf.Object) -> int:
        """Allocate a new MCID for one content stream owner."""
        owner_key = self._content_owner_key(owner)
        mcid = self._content_owner_mcid_counter.get(owner_key, 0)
        self._content_owner_mcid_counter[owner_key] = mcid + 1
        return mcid

    def _alloc_struct_parent_key(self) -> int:
        """Allocate a StructParent key for ParentTree entries."""
        key = self._struct_parents_counter
        self._struct_parents_counter += 1
        return key

    def _register_mcid(
        self,
        *,
        owner: pikepdf.Object,
        page_index: int,
        mcid: int,
        elem: pikepdf.Object | None,
    ) -> None:
        """Register an MCID-to-StructElem mapping for one page or form stream owner."""
        owner_key = self._content_owner_key(owner)
        entry = self._content_owner_entries.get(owner_key)
        if entry is None:
            entry = {
                "owner": owner,
                "page_index": page_index,
                "mcids": [],
                "order": self._content_owner_order,
            }
            self._content_owner_entries[owner_key] = entry
            self._content_owner_order += 1
        entry["mcids"].append((mcid, elem))

    def _make_mcr(
        self,
        mcid: int,
        page_ref: pikepdf.Object,
        *,
        stream_owner: pikepdf.Object | None = None,
    ) -> pikepdf.Dictionary:
        """Create a Marked Content Reference (MCR) dictionary."""
        mcr = pikepdf.Dictionary({
            "/Type": pikepdf.Name("/MCR"),
            "/Pg": page_ref,
            "/MCID": mcid,
        })
        if stream_owner is not None and _obj_key(stream_owner) != _obj_key(page_ref):
            mcr["/Stm"] = stream_owner
        return mcr

    def _append_child(self, parent: pikepdf.Object, child: pikepdf.Object):
        """Append a child element to a parent's /K array."""
        if self.reading_order is not None:
            self._reading_order_of[child.objgen] = self.reading_order
        parent_k = parent.get("/K")
        if isinstance(parent_k, pikepdf.Array):
            parent_k.append(child)
        elif parent_k is not None:
            parent["/K"] = pikepdf.Array([parent_k, child])
        else:
            parent["/K"] = pikepdf.Array([child])

    def order_page_kids(self, start: int) -> None:
        """Put the elements added to /Document since position ``start`` (one
        page's) in reading order. The content stream draws them in whatever
        order its producer chose; the page vote selects their reading order. An
        element without a position keeps its place after the one before it.
        """
        kids = self.doc_elem.get("/K")
        if not isinstance(kids, pikepdf.Array):
            return
        def position(kid):
            order = self._reading_order_of.get(kid.objgen)
            if kid.get("/S") == "/L":
                items = list(kid.get("/K") or [])
                items.sort(key=lambda item: self._reading_order_of.get(item.objgen, -1))
                kid["/K"] = pikepdf.Array(items)
                positions = [position(item) for item in items]
                positions = [value for value in positions if value is not None]
                if positions:
                    order = min(positions)
            return order

        page_kids = [kids[i] for i in range(start, len(kids))]
        keys: list[float] = []
        for kid in page_kids:
            order = position(kid)
            keys.append(order if order is not None else (keys[-1] if keys else -1))
        ordered = [kid for _key, _i, kid in sorted(zip(keys, range(len(keys)), page_kids))]
        self.doc_elem["/K"] = pikepdf.Array([kids[i] for i in range(start)] + ordered)

    def _struct_elem_for_mcid(self, owner: pikepdf.Object, mcid: int) -> pikepdf.Object | None:
        owner_key = self._content_owner_key(owner)
        entry = self._content_owner_entries.get(owner_key)
        if not entry:
            return None
        for registered_mcid, elem in entry["mcids"]:
            if registered_mcid == mcid:
                return elem
        return None

    def remember_source_element(
        self,
        source_element: dict[str, Any],
        owner: pikepdf.Object,
        mcid: int,
    ) -> None:
        """Remember which StructElem represents a structure-json element."""
        elem = self._struct_elem_for_mcid(owner, mcid)
        if elem is not None:
            self._source_element_structs[_source_key(source_element)] = elem

    def source_element_struct(self, source_element: dict[str, Any] | None) -> pikepdf.Object | None:
        if source_element is None:
            return None
        return self._source_element_structs.get(_source_key(source_element))

    def add_mcr_to_struct_elem(
        self,
        elem: pikepdf.Object,
        page_index: int,
        page_ref: pikepdf.Object,
        *,
        stream_owner: pikepdf.Object | None = None,
    ) -> int:
        """Append another marked-content reference to an existing StructElem."""
        owner = stream_owner or page_ref
        mcid = self._alloc_mcid(owner)
        mcr = self._make_mcr(mcid, page_ref, stream_owner=stream_owner)
        kids = elem.get("/K")
        if isinstance(kids, pikepdf.Array):
            kids.append(mcr)
        elif kids is not None:
            elem["/K"] = pikepdf.Array([kids, mcr])
        else:
            elem["/K"] = mcr
        self._register_mcid(owner=owner, page_index=page_index, mcid=mcid, elem=elem)
        return mcid

    def move_fragment_to_list_label(
        self, body: pikepdf.Object, owner: pikepdf.Object, mcid: int
    ) -> None:
        """Read a recovered hanging marker before the body's fragments.

        Keep the existing LBody (and its deliberate ActualText) intact; the
        label is a sibling, so a correction cannot swallow the marker.
        """
        item = body["/P"]
        label = next((kid for kid in item.K if kid.get("/S") == "/Lbl"), None)
        if label is None:
            label = self.pdf.make_indirect(
                pikepdf.Dictionary(
                    Type=pikepdf.Name.StructElem, S=pikepdf.Name.Lbl, P=item, K=pikepdf.Array([])
                )
            )
            item.K = pikepdf.Array([label, *list(item.K)])
            self._struct_elems_created += 1
        kids = body.get("/K")
        kids = list(kids) if isinstance(kids, pikepdf.Array) else [kids]
        # The allocated fragment is always last, even when it is the first
        # fragment and the body held a single MCR rather than an array.
        label.K.append(kids.pop())
        body.K = pikepdf.Array(kids)
        entry = self._content_owner_entries[self._content_owner_key(owner)]
        entry["mcids"] = [(key, label if key == mcid else elem) for key, elem in entry["mcids"]]

    def _add_struct_elem(
        self,
        struct_type: str,
        page_index: int,
        page_ref: pikepdf.Object,
        parent: pikepdf.Object | None = None,
        alt_text: str | None = None,
        lang: str | None = None,
        stream_owner: pikepdf.Object | None = None,
    ) -> int:
        """Create a StructElem and return its MCID."""
        if parent is None:
            parent = self.doc_elem

        owner = stream_owner or page_ref
        mcid = self._alloc_mcid(owner)

        elem_dict = {
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name(f"/{struct_type}"),
            "/P": parent,
            "/K": self._make_mcr(mcid, page_ref, stream_owner=stream_owner),
        }
        if alt_text:
            elem_dict["/Alt"] = pikepdf.String(alt_text)
        if lang and lang.lower().strip() != self.doc_lang:
            elem_dict["/Lang"] = pikepdf.String(lang)

        elem = self.pdf.make_indirect(pikepdf.Dictionary(elem_dict))
        self._register_mcid(owner=owner, page_index=page_index, mcid=mcid, elem=elem)
        self._append_child(parent, elem)
        self._struct_elems_created += 1
        return mcid

    def add_heading(
        self, level: int, page_index: int, page_ref: pikepdf.Object,
        text: str, lang: str | None = None, stream_owner: pikepdf.Object | None = None,
    ) -> int:
        level = max(1, min(6, level))
        mcid = self._add_struct_elem(
            f"H{level}",
            page_index,
            page_ref,
            lang=lang,
            stream_owner=stream_owner,
        )
        self._headings.append({"level": level, "text": text, "page_index": page_index})
        return mcid

    def add_paragraph(
        self, page_index: int, page_ref: pikepdf.Object,
        lang: str | None = None,
        stream_owner: pikepdf.Object | None = None,
    ) -> int:
        return self._add_struct_elem("P", page_index, page_ref, lang=lang, stream_owner=stream_owner)

    def add_figure(
        self,
        page_index: int,
        page_ref: pikepdf.Object,
        alt_text: str | None = None,
        stream_owner: pikepdf.Object | None = None,
    ) -> int:
        return self._add_struct_elem(
            "Figure",
            page_index,
            page_ref,
            alt_text=alt_text,
            stream_owner=stream_owner,
        )

    def add_code(
        self,
        page_index: int,
        page_ref: pikepdf.Object,
        lang: str | None = None,
        stream_owner: pikepdf.Object | None = None,
    ) -> int:
        return self._add_struct_elem("Code", page_index, page_ref, lang=lang, stream_owner=stream_owner)

    def add_formula(
        self,
        page_index: int,
        page_ref: pikepdf.Object,
        alt_text: str | None = None,
        stream_owner: pikepdf.Object | None = None,
    ) -> int:
        return self._add_struct_elem(
            "Formula",
            page_index,
            page_ref,
            alt_text=alt_text,
            stream_owner=stream_owner,
        )

    def add_note(
        self,
        page_index: int,
        page_ref: pikepdf.Object,
        *,
        stream_owner: pikepdf.Object | None = None,
    ) -> int:
        owner = stream_owner or page_ref
        mcid = self._alloc_mcid(owner)
        self._note_counter += 1
        note_elem = self.pdf.make_indirect(pikepdf.Dictionary({
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Note"),
            "/P": self.doc_elem,
            "/ID": pikepdf.String(f"note-{self._note_counter}"),
            "/K": self._make_mcr(mcid, page_ref, stream_owner=stream_owner),
        }))
        self._register_mcid(owner=owner, page_index=page_index, mcid=mcid, elem=note_elem)
        self._append_child(self.doc_elem, note_elem)
        self._struct_elems_created += 1
        return mcid

    def add_caption(
        self,
        page_index: int,
        page_ref: pikepdf.Object,
        *,
        stream_owner: pikepdf.Object | None = None,
    ) -> int:
        return self._add_struct_elem(
            "Caption",
            page_index,
            page_ref,
            stream_owner=stream_owner,
        )

    def add_reference(
        self,
        page_index: int,
        page_ref: pikepdf.Object,
        *,
        stream_owner: pikepdf.Object | None = None,
    ) -> int:
        return self._add_struct_elem(
            "Reference",
            page_index,
            page_ref,
            stream_owner=stream_owner,
        )

    def add_bib_entry(
        self,
        page_index: int,
        page_ref: pikepdf.Object,
        *,
        stream_owner: pikepdf.Object | None = None,
    ) -> int:
        return self._add_struct_elem(
            "BibEntry",
            page_index,
            page_ref,
            stream_owner=stream_owner,
        )

    def _get_or_create_toc(self, toc_group_ref: str | None) -> pikepdf.Object:
        if toc_group_ref and toc_group_ref in self._toc_cache:
            return self._toc_cache[toc_group_ref]

        toc_elem = self.pdf.make_indirect(pikepdf.Dictionary({
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/TOC"),
            "/P": self.doc_elem,
            "/K": pikepdf.Array([]),
        }))
        self._append_child(self.doc_elem, toc_elem)
        self._struct_elems_created += 1
        if toc_group_ref:
            self._toc_cache[toc_group_ref] = toc_elem
        return toc_elem

    def add_toc_caption(
        self,
        page_index: int,
        page_ref: pikepdf.Object,
        toc_group_ref: str | None,
        *,
        stream_owner: pikepdf.Object | None = None,
    ) -> int:
        toc_elem = self._get_or_create_toc(toc_group_ref)
        owner = stream_owner or page_ref
        mcid = self._alloc_mcid(owner)
        caption_elem = self.pdf.make_indirect(pikepdf.Dictionary({
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Caption"),
            "/P": toc_elem,
            "/K": self._make_mcr(mcid, page_ref, stream_owner=stream_owner),
        }))
        toc_k = toc_elem.get("/K")
        if isinstance(toc_k, pikepdf.Array):
            toc_elem["/K"] = pikepdf.Array([caption_elem, *list(toc_k)])
        else:
            toc_elem["/K"] = pikepdf.Array([caption_elem])
        self._register_mcid(owner=owner, page_index=page_index, mcid=mcid, elem=caption_elem)
        self._struct_elems_created += 1
        return mcid

    def add_toc_item(
        self,
        page_index: int,
        page_ref: pikepdf.Object,
        toc_group_ref: str | None,
        *,
        stream_owner: pikepdf.Object | None = None,
    ) -> int:
        toc_elem = self._get_or_create_toc(toc_group_ref)
        owner = stream_owner or page_ref
        mcid = self._alloc_mcid(owner)
        toci_elem = self.pdf.make_indirect(pikepdf.Dictionary({
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/TOCI"),
            "/P": toc_elem,
            "/K": self._make_mcr(mcid, page_ref, stream_owner=stream_owner),
        }))
        self._append_child(toc_elem, toci_elem)
        self._register_mcid(owner=owner, page_index=page_index, mcid=mcid, elem=toci_elem)
        self._struct_elems_created += 1
        return mcid

    def _ensure_fragmented_table(
        self,
        table_data: dict,
    ) -> dict[tuple[int, int], pikepdf.Object]:
        """Create a Table/TR/TH/TD skeleton for fragmented OCR cell MCRs."""
        table_key = id(table_data)
        cached = self._fragmented_table_cache.get(table_key)
        if cached is not None:
            return cached

        num_rows = _as_positive_int(table_data.get("num_rows", 0), default=0)
        num_cols = _as_positive_int(table_data.get("num_cols", 0), default=0)
        raw_cells = table_data.get("cells", [])
        cells = _complete_table_grid_cells(raw_cells, num_rows=num_rows, num_cols=num_cols)
        if not cells:
            self._fragmented_table_cache[table_key] = {}
            return {}

        self._table_counter += 1
        table_n = self._table_counter
        table_elem_dict: dict = {
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Table"),
            "/P": self.doc_elem,
            "/K": pikepdf.Array([]),
        }
        table_summary = _table_summary_text(table_data, num_rows=num_rows, num_cols=num_cols)
        if table_summary:
            table_elem_dict["/A"] = pikepdf.Dictionary({
                "/O": pikepdf.Name("/Table"),
                "/Summary": pikepdf.String(table_summary),
            })
        table_elem = self.pdf.make_indirect(pikepdf.Dictionary(table_elem_dict))
        self._append_child(self.doc_elem, table_elem)
        self._struct_elems_created += 1

        rows: dict[int, list[dict]] = {}
        for cell in cells:
            row_idx = _as_positive_int(cell.get("row", 0), default=0)
            rows.setdefault(row_idx, []).append(cell)

        cell_structs: dict[tuple[int, int], pikepdf.Object] = {}
        th_info: list[tuple[int, int, int, int, str, str, bool]] = []
        td_info: list[tuple[int, int, pikepdf.Object]] = []

        for row_idx in sorted(rows.keys()):
            row_elem = self.pdf.make_indirect(pikepdf.Dictionary({
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/TR"),
                "/P": table_elem,
                "/K": pikepdf.Array([]),
            }))
            table_elem["/K"].append(row_elem)
            self._struct_elems_created += 1

            for cell in sorted(rows[row_idx], key=lambda c: c.get("col", 0)):
                col_idx = _as_positive_int(cell.get("col", 0), default=0)
                cell_type = "TH" if any(cell.get(flag) for flag in ("is_header", "column_header", "row_header")) else "TD"
                cell_elem_dict: dict = {
                    "/Type": pikepdf.Name("/StructElem"),
                    "/S": pikepdf.Name(f"/{cell_type}"),
                    "/P": row_elem,
                    "/K": pikepdf.Array([]),
                }
                attrs = pikepdf.Dictionary({"/O": pikepdf.Name("/Table")})
                row_span = _as_positive_int(cell.get("row_span", 1), default=1)
                col_span = _as_positive_int(cell.get("col_span", 1), default=1)
                if row_span > 1:
                    attrs["/RowSpan"] = row_span
                if col_span > 1:
                    attrs["/ColSpan"] = col_span

                if cell_type == "TH":
                    scope = "/Column"
                    if bool(cell.get("row_header", False)):
                        scope = "/Row"
                    elif bool(cell.get("column_header", False)):
                        scope = "/Column"
                    elif col_idx == 0:
                        scope = "/Row"
                    attrs["/Scope"] = pikepdf.Name(scope)
                    cell_id = f"t{table_n}-r{row_idx}-c{col_idx}"
                    cell_elem_dict["/ID"] = pikepdf.String(cell_id)
                    th_info.append((row_idx, col_idx, row_span, col_span, cell_id, scope,
                                    bool(cell.get("_projected_row_header"))))

                if len(attrs) > 1:
                    cell_elem_dict["/A"] = attrs

                cell_elem = self.pdf.make_indirect(pikepdf.Dictionary(cell_elem_dict))
                row_elem["/K"].append(cell_elem)
                cell_structs[(row_idx, col_idx)] = cell_elem
                self._struct_elems_created += 1

                if cell_type == "TD":
                    td_info.append((row_idx, col_idx, cell_elem))

        if th_info and td_info:
            for td_row, td_col, td_elem in td_info:
                header_ids: list[str] = []
                for th_row, th_col, th_rspan, th_cspan, th_id, scope, projected in th_info:
                    if projected:
                        next_group = min((r for r, _, _, _, _, _, p in th_info if p and r > th_row), default=num_rows)
                        if th_row < td_row < next_group:
                            header_ids.append(th_id)
                    elif scope == "/Column" and th_col <= td_col < th_col + th_cspan and th_row + th_rspan <= td_row:
                        header_ids.append(th_id)
                    elif scope == "/Row" and th_row <= td_row < th_row + th_rspan and th_col + th_cspan <= td_col:
                        header_ids.append(th_id)
                if header_ids:
                    td_attrs = td_elem.get("/A")
                    if td_attrs is None:
                        td_attrs = pikepdf.Dictionary({"/O": pikepdf.Name("/Table")})
                        td_elem["/A"] = td_attrs
                    td_attrs["/Headers"] = pikepdf.Array(
                        [pikepdf.String(h) for h in header_ids],
                    )

        self._fragmented_table_cache[table_key] = cell_structs
        return cell_structs

    def add_table_cell_fragment(
        self,
        page_index: int,
        page_ref: pikepdf.Object,
        table_data: dict,
        cell: dict,
        *,
        stream_owner: pikepdf.Object | None = None,
    ) -> tuple[str, int] | None:
        """Append one fragmented OCR marked-content run to a table cell."""
        cell_structs = self._ensure_fragmented_table(table_data)
        row_idx = _as_positive_int(cell.get("row", 0), default=0)
        col_idx = _as_positive_int(cell.get("col", 0), default=0)
        cell_elem = cell_structs.get((row_idx, col_idx))
        if cell_elem is None:
            return None

        mcid = self.add_mcr_to_struct_elem(
            cell_elem,
            page_index,
            page_ref,
            stream_owner=stream_owner,
        )
        tag = str(cell_elem["/S"]).lstrip("/")
        return tag, mcid

    def add_list_item(
        self,
        page_index: int,
        page_ref: pikepdf.Object,
        list_group_ref: str | None,
        parent_list_group_ref: str | None = None,
        marker: Any = None,
        enumerated: bool = False,
        *,
        stream_owner: pikepdf.Object | None = None,
    ) -> int:
        """Add a single list item to a list group, creating the list on first use.

        List items sharing the same list_group_ref are grouped under a single
        /L StructElem. The list is created lazily on the first item and reused
        for subsequent items in the same group.

        For nested lists, parent_list_group_ref identifies the enclosing list
        group. The nested /L is attached as a child of the parent list's most
        recent /LI element, producing the PDF structure::

            /L (outer)
              /LI
                /LBody …
                /L (nested)      ← attached here
                  /LI
                    /LBody …
        """
        if list_group_ref and list_group_ref in self._list_cache:
            list_elem, _ = self._list_cache[list_group_ref]
        else:
            list_elem = self.pdf.make_indirect(pikepdf.Dictionary({
                "/Type": pikepdf.Name("/StructElem"),
                "/S": pikepdf.Name("/L"),
                "/K": pikepdf.Array([]),
            }))

            # Determine where to attach this /L element.
            # If it belongs to a nested list, place it inside the parent
            # list's most recent /LI; otherwise attach to the document root.
            attach_parent = self.doc_elem
            if parent_list_group_ref and parent_list_group_ref in self._list_cache:
                _, parent_last_li = self._list_cache[parent_list_group_ref]
                if parent_last_li is not None:
                    attach_parent = parent_last_li

            list_elem["/P"] = attach_parent
            self._append_child(attach_parent, list_elem)
            self._struct_elems_created += 1
            if list_group_ref:
                self._list_cache[list_group_ref] = (list_elem, None)
        _set_list_numbering_attr(list_elem, marker, enumerated=enumerated)

        li_elem = self.pdf.make_indirect(pikepdf.Dictionary({
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/LI"),
            "/P": list_elem,
            "/K": pikepdf.Array([]),
        }))
        self._append_child(list_elem, li_elem)
        self._struct_elems_created += 1

        # Track the most recent LI so nested lists can attach to it.
        if list_group_ref and list_group_ref in self._list_cache:
            self._list_cache[list_group_ref] = (
                self._list_cache[list_group_ref][0],
                li_elem,
            )

        owner = stream_owner or page_ref
        mcid = self._alloc_mcid(owner)
        lbody_elem = self.pdf.make_indirect(pikepdf.Dictionary({
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/LBody"),
            "/P": li_elem,
            "/K": self._make_mcr(mcid, page_ref, stream_owner=stream_owner),
        }))
        li_elem["/K"].append(lbody_elem)
        self._register_mcid(owner=owner, page_index=page_index, mcid=mcid, elem=lbody_elem)
        self._struct_elems_created += 1

        return mcid

    def add_link_annotation(
        self,
        page_ref: pikepdf.Object,
        annotation: pikepdf.Object,
        parent: pikepdf.Object | None = None,
    ) -> bool:
        """Attach a /Link StructElem to an existing link annotation via OBJR."""
        try:
            if annotation.get("/Subtype") != pikepdf.Name("/Link"):
                return False
            if not getattr(annotation, "is_indirect", False):
                return False
        except Exception:
            return False

        if annotation.objgen in self._link_structs:
            return True
        if parent is None:
            parent = self.doc_elem
        # ActualText replaces an element's whole subtree for a screen reader,
        # so it would hide the link; the parent reads from its glyphs instead.
        if "/ActualText" in parent:
            del parent["/ActualText"]
        annotation["/P"] = page_ref
        if not pdf_text(annotation.get("/Contents")).strip():
            annotation["/Contents"] = pikepdf.String(_infer_link_contents(annotation))

        link_elem = self.pdf.make_indirect(pikepdf.Dictionary({
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Link"),
            "/P": parent,
            "/Pg": page_ref,
            "/K": pikepdf.Dictionary({
                "/Type": pikepdf.Name("/OBJR"),
                "/Obj": annotation,
                "/Pg": page_ref,
            }),
        }))
        self._append_child(parent, link_elem)
        self._link_structs[annotation.objgen] = link_elem
        self._object_parent_entries.append((annotation, link_elem))
        self._struct_elems_created += 1
        return True

    def put_mcid_in_link(
        self, owner: pikepdf.Object, mcid: int,
        page_ref: pikepdf.Object, annotation: pikepdf.Object,
    ) -> None:
        """Move a just-allocated text fragment into its inline Link, keeping
        the Link at that fragment's position among the paragraph's children."""
        parent = self._struct_elem_for_mcid(owner, mcid)
        if parent is None:
            return
        kids = parent.get("/K")
        kids = list(kids) if isinstance(kids, pikepdf.Array) else [kids]
        # The allocator just appended this MCR; earlier fragments stay put.
        mcr = kids.pop()
        parent["/K"] = pikepdf.Array(kids)
        self.add_link_annotation(page_ref, annotation, parent=parent)
        link = self._link_structs[annotation.objgen]
        link_kids = link.get("/K")
        link_kids = list(link_kids) if isinstance(link_kids, pikepdf.Array) else [link_kids]
        # Keep the OBJR once, after the Link's marked content.
        link["/K"] = pikepdf.Array([*link_kids[:-1], mcr, link_kids[-1]])
        entries = self._content_owner_entries[self._content_owner_key(owner)]["mcids"]
        for index, (registered_mcid, _elem) in enumerate(entries):
            if registered_mcid == mcid:
                entries[index] = (mcid, link)
                break

    def add_widget_annotation(self, page_ref: pikepdf.Object, annotation: pikepdf.Object) -> bool:
        """Attach a /Form StructElem to a widget annotation via OBJR."""
        try:
            if annotation.get("/Subtype") != pikepdf.Name("/Widget"):
                return False
            if not getattr(annotation, "is_indirect", False):
                return False
        except Exception:
            return False

        annotation["/P"] = page_ref

        form_elem = self.pdf.make_indirect(pikepdf.Dictionary({
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Form"),
            "/P": self.doc_elem,
            "/Pg": page_ref,
            "/K": pikepdf.Dictionary({
                "/Type": pikepdf.Name("/OBJR"),
                "/Obj": annotation,
                "/Pg": page_ref,
            }),
        }))
        self._append_child(self.doc_elem, form_elem)
        self._object_parent_entries.append((annotation, form_elem))
        self._struct_elems_created += 1
        return True

    def add_generic_annotation(self, page_ref: pikepdf.Object, annotation: pikepdf.Object) -> bool:
        """Attach a generic /Annot StructElem to a non-link, non-widget annotation."""
        try:
            subtype = annotation.get("/Subtype")
            if subtype in (pikepdf.Name("/Link"), pikepdf.Name("/Widget"), pikepdf.Name("/PrinterMark")):
                return False
            if not getattr(annotation, "is_indirect", False):
                return False
        except Exception:
            return False

        annotation["/P"] = page_ref

        annot_elem = self.pdf.make_indirect(pikepdf.Dictionary({
            "/Type": pikepdf.Name("/StructElem"),
            "/S": pikepdf.Name("/Annot"),
            "/P": self.doc_elem,
            "/Pg": page_ref,
            "/K": pikepdf.Dictionary({
                "/Type": pikepdf.Name("/OBJR"),
                "/Obj": annotation,
                "/Pg": page_ref,
            }),
        }))
        self._append_child(self.doc_elem, annot_elem)
        self._object_parent_entries.append((annotation, annot_elem))
        self._struct_elems_created += 1
        return True

    def finalize(self):
        """Build the ParentTree NumberTree and set StructParents on content owners."""
        parent_tree_nums = pikepdf.Array([])

        owner_entries = sorted(
            self._content_owner_entries.values(),
            key=lambda entry: (int(entry["page_index"]), int(entry["order"])),
        )
        for entry in owner_entries:
            owner = entry["owner"]
            struct_parents_idx = self._alloc_struct_parent_key()
            owner["/StructParents"] = struct_parents_idx

            mcid_entries = entry["mcids"]
            elem_array = pikepdf.Array([])
            for _mcid, elem in sorted(mcid_entries, key=lambda x: x[0]):
                elem_array.append(elem)

            parent_tree_nums.append(struct_parents_idx)
            parent_tree_nums.append(self.pdf.make_indirect(elem_array))

        for annotation, elem in self._object_parent_entries:
            struct_parent_key = self._alloc_struct_parent_key()
            annotation["/StructParent"] = struct_parent_key
            parent_tree_nums.append(struct_parent_key)
            parent_tree_nums.append(elem)

        parent_tree = self.pdf.make_indirect(pikepdf.Dictionary({
            "/Nums": parent_tree_nums,
        }))
        self.struct_tree_root["/ParentTree"] = parent_tree
        self.struct_tree_root["/ParentTreeNextKey"] = self._struct_parents_counter
