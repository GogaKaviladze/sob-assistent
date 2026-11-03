"""RAM-only Fallklammer (CaseSession) für den Fachdialog (Issue #133 / ADR-4 / ADR-10).

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
    csv: Optional[CsvData] = None
    plan: Optional[PlanData] = None
    plan_context: Optional[str] = None
    plan_info: Dict[str, Any] = field(default_factory=dict)
    messages: List[Dict[str, str]] = field(default_factory=list)
    area_state: Dict[str, Any] = field(default_factory=dict)
    last_activity: float = field(default_factory=time.time)

    def touch(self, now: Optional[float] = None) -> None:
        """Aktualisiert den Zeitstempel der letzten Fall-Aktivität."""
        self.last_activity = now if now is not None else time.time()

    def clear(self) -> None:
        """Setzt sämtliche Falldaten zurück."""
        self.csv = None
        self.plan = None
        self.plan_context = None
        self.plan_info.clear()
        self.messages.clear()
        self.area_state.clear()
        self.touch()
