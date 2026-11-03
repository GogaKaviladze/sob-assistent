"""Strukturprüfung für das ausführbare Lernnotebook zum App-Durchlauf."""
import ast
import json
from pathlib import Path


NOTEBOOK_PATH = Path(__file__).resolve().parents[1] / "notebooks" / "app_walkthrough.ipynb"


def test_walkthrough_notebook_is_valid_and_covers_the_complete_flow():
    notebook = json.loads(NOTEBOOK_PATH.read_text(encoding="utf-8"))
    cell_ids = [cell.get("id") for cell in notebook["cells"]]
    assert all(cell_ids)
    assert len(cell_ids) == len(set(cell_ids))
    markdown = "\n".join(
        "".join(cell.get("source", []))
        for cell in notebook["cells"]
        if cell["cell_type"] == "markdown"
    )
    code = "\n".join(
        "".join(cell.get("source", []))
        for cell in notebook["cells"]
        if cell["cell_type"] == "code"
    )

    for heading in (
        "App-Durchlauf im Jupyter-Notebook",
        "Testdateien festlegen",
        "Echte App-Sitzung starten",
        "Lebensfelder prüfen",
        "Fortlaufenden Fachkraftdialog führen",
        "Internen Zustand prüfen",
        "Lebensfeld freigeben",
        "Bericht exportieren",
    ):
        assert heading in markdown

    for operation in (
        "start_session(",
        "process_message(",
        "select_section(",
        "confirm_section(",
        "export_docx(",
        "export_markdown(",
    ):
        assert operation in code

    for index, cell in enumerate(notebook["cells"]):
        if cell["cell_type"] == "code":
            ast.parse("".join(cell["source"]), filename=f"{NOTEBOOK_PATH}:cell-{index}")


def test_walkthrough_uses_real_model_by_default_and_synthetic_files():
    notebook = json.loads(NOTEBOOK_PATH.read_text(encoding="utf-8"))
    code = "\n".join(
        "".join(cell.get("source", []))
        for cell in notebook["cells"]
        if cell["cell_type"] == "code"
    )

    assert "ECHTES_MODELL = True" in code
    assert "Max_Mustermann_Vivendi_Export.csv" in code
    assert "Max_Mustermann_SVB_ausgefuellt.docx" in code
    assert "Hans_Peter" not in code


def test_walkthrough_contains_editable_multi_turn_professional_dialogue():
    notebook = json.loads(NOTEBOOK_PATH.read_text(encoding="utf-8"))
    code = "\n".join(
        "".join(cell.get("source", []))
        for cell in notebook["cells"]
        if cell["cell_type"] == "code"
    )

    assert "async def sende_fachkraftnachricht" in code
    assert code.count("chat_session = await sende_fachkraftnachricht(NACHRICHT)") == 10
    assert "Wechsel zu Feld 1.2." in code
    assert "31.12.2026" in code
    assert "Korrektur:" in code
    assert "kann ich noch nicht beurteilen" in code
    assert "bearbeitbaren Entwurf" in code
