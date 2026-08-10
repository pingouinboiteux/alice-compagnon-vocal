"""Arret F12 verrouille, independant de la boucle de vision."""

from __future__ import annotations

import ctypes
import threading
from dataclasses import dataclass
from typing import Callable, Protocol


VK_F12 = 0x7B


class KeyProbe(Protocol):
    def f12_pressed(self) -> bool: ...


class WindowsKeyProbe:
    def f12_pressed(self) -> bool:
        return bool(ctypes.windll.user32.GetAsyncKeyState(VK_F12) & 0x8000)


@dataclass(frozen=True)
class EmergencyState:
    latched: bool
    generation: int
    reason: str


class EmergencyLatch:
    def __init__(
        self,
        release_all: Callable[[], None],
        *,
        probe: KeyProbe | None = None,
        interval_s: float = 0.005,
    ) -> None:
        self._release_all = release_all
        self._probe = probe or WindowsKeyProbe()
        self._interval_s = interval_s
        self._lock = threading.Lock()
        self._latched = True
        self._generation = 0
        self._reason = "autorisation humaine requise"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._watch, name="alice-f12-gamepad", daemon=True,
        )
        self._thread.start()

    def stop(self, timeout_s: float = 1.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout_s)
        self._thread = None
        self.trigger("pilotage arrete")

    def trigger(self, reason: str) -> None:
        with self._lock:
            first = not self._latched
            self._latched = True
            self._generation += 1
            self._reason = reason[:240]
        try:
            self._release_all()
        except Exception:
            if first:
                raise

    def rearm(self, capability: object, expected: object) -> None:
        if capability is not expected:
            raise PermissionError("reprise humaine refusee")
        with self._lock:
            self._latched = False
            self._reason = ""

    def state(self) -> EmergencyState:
        with self._lock:
            return EmergencyState(self._latched, self._generation, self._reason)

    def apply_if_armed(self, operation: Callable[[], None]) -> bool:
        """Verifie puis applique sous le meme verrou que le marquage F12.

        L'operation doit etre tres courte et ne jamais attendre. Si elle a
        commence avant F12, F12 relache juste apres. Si F12 marque d'abord,
        l'operation ne part pas.
        """
        with self._lock:
            if self._latched:
                return False
            operation()
            return True

    def _watch(self) -> None:
        was_pressed = False
        try:
            while not self._stop.wait(self._interval_s):
                pressed = self._probe.f12_pressed()
                if pressed and not was_pressed:
                    self.trigger("F12")
                was_pressed = pressed
        except Exception:
            # Perdre le surveillant F12 est une anomalie de securite, pas une
            # raison de continuer avec une protection en moins.
            self.trigger("surveillance F12 en panne")
