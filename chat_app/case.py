"""RAM-only Fallklammer (CaseSession) für den Fachdialog (Issue #133 / #136 / ADR-4 / ADR-10).

Verwaltet alle Falldaten (CSV, Vorbericht/Plan, Dialogverlauf und Bereichszustände)
ausschließlich im flüchtigen Arbeitsspeicher (Zero Persistence).
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from chat_app.csv_loader import CsvData
from chat_app.plan_loader import PlanData


@dataclass
class CaseSession:
    """Hält alle Daten eines einzelnen Klientenfalls strikt im RAM."""

    fall_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    template: str = "hamburg"
    csv: Optional[CsvData] = None
    plan: Optional[PlanData] = None
    plan_context: Optional[str] = None
    plan_info: Dict[str, Any] = field(default_factory=dict)
    messages: List[Dict[str, Any]] = field(default_factory=list)
    area_state: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    selected_bereich_id: Optional[str] = None
    last_activity: float = field(default_factory=time.time)

    def touch(self, now: Optional[float] = None) -> None:
        """Aktualisiert den Zeitstempel der letzten Fall-Aktivität."""
        self.last_activity = now if now is not None else time.time()

    def get_area_state(self, bereich_id: str) -> Dict[str, Any]:
        """Liefert den isolierten RAM-Zustand eines Lebensbereichs."""
        if bereich_id not in self.area_state:
            self.area_state[bereich_id] = {
                "confirmed": False,
                "messages": [],
            }
        return self.area_state[bereich_id]

    def get_area_messages(self, bereich_id: str) -> List[Dict[str, Any]]:
        """Gibt die Liste der Chat-Nachrichten des angegebenen Bereichs zurück."""
        return self.get_area_state(bereich_id)["messages"]

    def is_area_confirmed(self, bereich_id: str) -> bool:
        """Prüft, ob der Bereich im aktuellen Fall bestätigt wurde."""
        if bereich_id not in self.area_state:
            return False
        return bool(self.area_state[bereich_id].get("confirmed", False))

    def confirm_area(self, bereich_id: str) -> None:
        """Setzt das Bestätigungs-Flag (Bereich geprüft) für einen Lebensbereich."""
        self.get_area_state(bereich_id)["confirmed"] = True
        self.touch()

    def clear(self) -> None:
        """Setzt sämtliche Falldaten zurück."""
        self.template = "hamburg"
        self.csv = None
        self.plan = None
        self.plan_context = None
        self.plan_info.clear()
        self.messages.clear()
        self.area_state.clear()
        self.selected_bereich_id = None
        self.touch()
