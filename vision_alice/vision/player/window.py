"""Identite de la fenetre active, lue directement a Windows."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from typing import Protocol


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


@dataclass(frozen=True)
class ForegroundWindow:
    handle: int
    process_id: int
    executable: str
    title: str


class ForegroundProbe(Protocol):
    def read(self) -> ForegroundWindow | None: ...


class WindowsForegroundProbe:
    """Lit le focus sans envoyer de message a la fenetre."""

    def read(self) -> ForegroundWindow | None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = int(user32.GetForegroundWindow())
        if not handle:
            return None
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
        if not process_id.value:
            return None
        process = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, process_id.value,
        )
        if not process:
            return None
        try:
            size = wintypes.DWORD(32_768)
            path = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(
                process, 0, path, ctypes.byref(size),
            ):
                return None
        finally:
            kernel32.CloseHandle(process)
        length = user32.GetWindowTextLengthW(handle)
        title = ctypes.create_unicode_buffer(max(1, length + 1))
        user32.GetWindowTextW(handle, title, len(title))
        return ForegroundWindow(handle, int(process_id.value), path.value, title.value)
