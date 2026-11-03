"""XML-Ebene Zugriff auf Legacy-FORMTEXT/FORMCHECKBOX-Felder via Bookmarks (ADR-10).

python-docx hat keine API für w:fldChar-Formularfelder. Felder werden über
die Bookmark-Namen aus docs/field-mapping.md adressiert, die jeweils ein
Feld zwischen w:bookmarkStart und w:bookmarkEnd umschließen.
"""
import copy
import zipfile
from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"


def wtag(name):
    return f"{{{W}}}{name}"


DOCUMENT_XML = "word/document.xml"


def load_document_tree(docx_path):
    with zipfile.ZipFile(docx_path) as z:
        raw_xml = z.read(DOCUMENT_XML)
        if b"<!DOCTYPE" in raw_xml.upper():
            from .docx_safety import UnsafeDocxError
            raise UnsafeDocxError("doctype")
        from .docx_safety import SAFE_PARSER
        return etree.fromstring(raw_xml, parser=SAFE_PARSER)


def save_document_tree(src_docx_path, tree, dst_docx_path):
    new_xml = etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=True)
    with zipfile.ZipFile(src_docx_path) as zin, \
            zipfile.ZipFile(dst_docx_path, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = new_xml if item.filename == DOCUMENT_XML else zin.read(item.filename)
            zout.writestr(item, data)


def _field_runs(root, bookmark_name):
    """Liefert (begin_run, result_runs, end_run) für das Feld unter `bookmark_name`."""
    bm_start = root.find(f'.//{wtag("bookmarkStart")}[@{wtag("name")}="{bookmark_name}"]')
    if bm_start is None:
        raise KeyError(f"Bookmark {bookmark_name!r} nicht gefunden")
    bm_id = bm_start.get(wtag("id"))
    parent = bm_start.getparent()
    bm_end = root.find(f'.//{wtag("bookmarkEnd")}[@{wtag("id")}="{bm_id}"]')

    # Fast path: bookmarkEnd ist ein Geschwister-Element im selben Absatz
    if bm_end is not None and bm_end.getparent() == parent:
        siblings = list(parent)
        start_idx = siblings.index(bm_start)
        begin_run = None
        result_runs = []
        end_run = None
        state = "before_begin"
        for el in siblings[start_idx + 1:]:
            if etree.QName(el.tag).localname == "bookmarkEnd" and el.get(wtag("id")) == bm_id:
                break
            if etree.QName(el.tag).localname != "r":
                continue
            fld = el.find(wtag("fldChar"))
            if fld is not None:
                ftype = fld.get(wtag("fldCharType"))
                if ftype == "begin":
                    begin_run = el
                    state = "in_instr"
                elif ftype == "separate":
                    state = "in_result"
                elif ftype == "end":
                    end_run = el
                    state = "done"
                continue
            if state == "in_result":
                result_runs.append(el)
        return begin_run, result_runs, end_run

    # Multi-paragraph path: Bookmark umspannt mehrere Absätze (z. B. via Enter oder set_formtext_list)
    begin_run = None
    result_runs = []
    end_run = None
    state = "before_begin"
    started = False
    for el in root.iter():
        if not started:
            if el == bm_start:
                started = True
            continue
        if etree.QName(el.tag).localname == "bookmarkEnd" and el.get(wtag("id")) == bm_id:
            break
        if etree.QName(el.tag).localname != "r":
            continue
        fld = el.find(wtag("fldChar"))
        if fld is not None:
            ftype = fld.get(wtag("fldCharType"))
            if ftype == "begin":
                begin_run = el
                state = "in_instr"
            elif ftype == "separate":
                state = "in_result"
            elif ftype == "end":
                end_run = el
                state = "done"
            continue
        if state == "in_result":
            result_runs.append(el)
    return begin_run, result_runs, end_run


def get_formtext(root, bookmark_name):
    _, result_runs, _ = _field_runs(root, bookmark_name)
    parts = []
    last_parent = None
    for r in result_runs:
        current_parent = r.getparent()
        if last_parent is not None and current_parent is not None and current_parent != last_parent:
            if parts and not parts[-1].endswith("\n"):
                parts.append("\n")
        last_parent = current_parent
        t = r.find(wtag("t"))
        if t is not None and t.text:
            parts.append(t.text)
    return "".join(parts)


def _set_run_text(run, value):
    """Setzt den Text eines Runs (legt ein w:t mit xml:space=preserve an)."""
    t = run.find(wtag("t"))
    if t is None:
        t = etree.SubElement(run, wtag("t"))
    t.set(f"{{{XML_NS}}}space", "preserve")
    t.text = value


def set_formtext(root, bookmark_name, value):
    _, result_runs, _ = _field_runs(root, bookmark_name)
    if not result_runs:
        raise ValueError(f"Feld {bookmark_name!r} hat keine Ergebnis-Runs")
    _set_run_text(result_runs[0], value)
    for r in result_runs[1:]:
        t = r.find(wtag("t"))
        if t is not None:
            t.text = ""


def set_formtext_list(root, bookmark_name, items):
    """Rendert ein FORMTEXT-Feld als mehrere Listen-Absätze — ein Absatz
    (= ein Aufzählungspunkt) pro Eintrag.

    Der Absatz des Feldes trägt die Listen-Formatierung (w:numPr). Ein einzelnes
    Feld lebt in genau einem Absatz und damit unter einem einzigen Bullet; um
    pro Eintrag einen eigenen Bullet zu erhalten, muss das Feld mehrere Absätze
    umspannen — genau das erzeugt Word, wenn man im Feld Enter drückt. Begin/
    separate bleiben im ersten Absatz, fldChar-end + bookmarkEnd wandern in den
    letzten neu erzeugten Absatz; die Zwischen-Absätze klonen die pPr (inkl.
    numPr) des Originalabsatzes und tragen je einen Eintrag.
    """
    items = [i for i in items if i is not None and str(i).strip() != ""]
    if not items:
        return
    if len(items) == 1:
        set_formtext(root, bookmark_name, items[0])
        return

    _, result_runs, end_run = _field_runs(root, bookmark_name)
    if not result_runs:
        raise ValueError(f"Feld {bookmark_name!r} hat keine Ergebnis-Runs")

    bm_start = root.find(f'.//{wtag("bookmarkStart")}[@{wtag("name")}="{bookmark_name}"]')
    bm_id = bm_start.get(wtag("id"))
    para = bm_start.getparent()
    bm_end = para.find(f'{wtag("bookmarkEnd")}[@{wtag("id")}="{bm_id}"]')
    ppr = para.find(wtag("pPr"))

    # Format-Vorlage für neue Runs (vor dem Überschreiben des Textes klonen).
    template_run = copy.deepcopy(result_runs[0])

    # Erster Eintrag bleibt im Originalabsatz; übrige Ergebnis-Runs leeren.
    _set_run_text(result_runs[0], items[0])
    for r in result_runs[1:]:
        t = r.find(wtag("t"))
        if t is not None:
            t.text = ""

    # Feldende vorübergehend aus dem ersten Absatz herauslösen.
    para.remove(end_run)
    para.remove(bm_end)

    # Pro weiterem Eintrag einen Listen-Absatz hinter dem vorherigen einfügen.
    prev = para
    for item in items[1:]:
        new_p = etree.Element(wtag("p"))
        if ppr is not None:
            new_p.append(copy.deepcopy(ppr))
        run = copy.deepcopy(template_run)
        _set_run_text(run, item)
        new_p.append(run)
        prev.addnext(new_p)
        prev = new_p

    # Feldende + bookmarkEnd schließen das Feld im letzten Absatz ab.
    prev.append(end_run)
    prev.append(bm_end)


def get_checkbox(root, bookmark_name):
    begin_run, _, _ = _field_runs(root, bookmark_name)
    checkbox = begin_run.find(f'{wtag("fldChar")}/{wtag("ffData")}/{wtag("checkBox")}')
    checked = checkbox.find(wtag("checked"))
    if checked is not None:
        return checked.get(wtag("val")) == "1"
    default = checkbox.find(wtag("default"))
    return default is not None and default.get(wtag("val")) == "1"


def set_checkbox(root, bookmark_name, value):
    begin_run, _, _ = _field_runs(root, bookmark_name)
    checkbox = begin_run.find(f'{wtag("fldChar")}/{wtag("ffData")}/{wtag("checkBox")}')
    checked = checkbox.find(wtag("checked"))
    if checked is None:
        checked = etree.SubElement(checkbox, wtag("checked"))
    checked.set(wtag("val"), "1" if value else "0")
