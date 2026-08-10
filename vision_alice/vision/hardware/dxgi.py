"""Enumeration DXGI en lecture seule pour identifier les GPU sans leur nom."""

from __future__ import annotations

import ctypes
import sys
import uuid
from dataclasses import dataclass


DXGI_ERROR_NOT_FOUND = 0x887A0002
IID_IDXGI_FACTORY1 = "770aae78-f26f-4dba-a829-253c83d1b387"


class _Guid(ctypes.Structure):
    _fields_ = (
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    )

    @classmethod
    def parse(cls, value: str) -> "_Guid":
        raw = uuid.UUID(value).bytes_le
        return cls.from_buffer_copy(raw)


class _Luid(ctypes.Structure):
    _fields_ = (("low", ctypes.c_uint32), ("high", ctypes.c_int32))


class _AdapterDescription(ctypes.Structure):
    _fields_ = (
        ("description", ctypes.c_wchar * 128),
        ("vendor_id", ctypes.c_uint32),
        ("device_id", ctypes.c_uint32),
        ("subsystem_id", ctypes.c_uint32),
        ("revision", ctypes.c_uint32),
        ("dedicated_video_memory", ctypes.c_size_t),
        ("dedicated_system_memory", ctypes.c_size_t),
        ("shared_system_memory", ctypes.c_size_t),
        ("luid", _Luid),
        ("flags", ctypes.c_uint32),
    )


@dataclass(frozen=True)
class DxgiAdapter:
    index: int
    name: str
    vendor_id: int
    device_id: int
    subsystem_id: int
    dedicated_video_memory: int
    luid_high: int
    luid_low: int
    flags: int = 0

    def __post_init__(self) -> None:
        for name, value in (
            ("index", self.index),
            ("vendor_id", self.vendor_id),
            ("device_id", self.device_id),
            ("subsystem_id", self.subsystem_id),
            ("dedicated_video_memory", self.dedicated_video_memory),
            ("luid_low", self.luid_low),
            ("flags", self.flags),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} DXGI invalide")
        if isinstance(self.luid_high, bool) or not isinstance(self.luid_high, int):
            raise ValueError("luid_high DXGI invalide")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("nom DXGI invalide")

    @property
    def luid(self) -> str:
        return f"0x{self.luid_high & 0xFFFFFFFF:08x}_0x{self.luid_low:08x}"


def choose_directml_device(
    adapters: tuple[DxgiAdapter, ...],
    *,
    subsystem_id: int,
) -> int:
    """Retourne un index seulement si l'identite materielle est unique."""

    if not isinstance(adapters, tuple):
        raise TypeError("les adaptateurs DXGI doivent etre figes")
    if any(not isinstance(adapter, DxgiAdapter) for adapter in adapters):
        raise TypeError("adaptateur DXGI invalide")
    if (
        isinstance(subsystem_id, bool)
        or not isinstance(subsystem_id, int)
        or subsystem_id <= 0
    ):
        raise ValueError("identite materielle cible invalide")
    matches = tuple(
        adapter for adapter in adapters
        if adapter.subsystem_id == subsystem_id and adapter.flags == 0
    )
    if len(matches) != 1:
        raise RuntimeError(
            "la carte DirectML cible n'est pas identifiable de facon unique"
        )
    return matches[0].index


def _method(instance: ctypes.c_void_p, slot: int, prototype):
    table = ctypes.cast(
        instance, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
    ).contents
    return prototype(table[slot])


def enumerate_dxgi_adapters() -> tuple[DxgiAdapter, ...]:
    """Lit l'ordre DXGI, qui est aussi l'ordre ``device_id`` de DirectML."""

    if sys.platform != "win32":
        raise RuntimeError("DXGI est disponible uniquement sous Windows")
    library = ctypes.WinDLL("dxgi.dll")
    create_factory = library.CreateDXGIFactory1
    create_factory.argtypes = (
        ctypes.POINTER(_Guid), ctypes.POINTER(ctypes.c_void_p),
    )
    create_factory.restype = ctypes.c_long
    identifier = _Guid.parse(IID_IDXGI_FACTORY1)
    factory = ctypes.c_void_p()
    result = create_factory(ctypes.byref(identifier), ctypes.byref(factory))
    if result < 0:
        raise OSError(result, "CreateDXGIFactory1 a echoue")

    enum_adapter = _method(
        factory,
        12,
        ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ),
    )
    release = ctypes.WINFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)
    adapters: list[DxgiAdapter] = []
    try:
        index = 0
        while True:
            pointer = ctypes.c_void_p()
            result = enum_adapter(factory, index, ctypes.byref(pointer))
            if result & 0xFFFFFFFF == DXGI_ERROR_NOT_FOUND:
                break
            if result < 0:
                raise OSError(result, "EnumAdapters1 a echoue")
            try:
                description = _AdapterDescription()
                get_description = _method(
                    pointer,
                    10,
                    ctypes.WINFUNCTYPE(
                        ctypes.c_long,
                        ctypes.c_void_p,
                        ctypes.POINTER(_AdapterDescription),
                    ),
                )
                result = get_description(pointer, ctypes.byref(description))
                if result < 0:
                    raise OSError(result, "GetDesc1 a echoue")
                adapters.append(
                    DxgiAdapter(
                        index=index,
                        name=description.description,
                        vendor_id=description.vendor_id,
                        device_id=description.device_id,
                        subsystem_id=description.subsystem_id,
                        dedicated_video_memory=description.dedicated_video_memory,
                        luid_high=description.luid.high,
                        luid_low=description.luid.low,
                        flags=description.flags,
                    )
                )
            finally:
                _method(pointer, 2, release)(pointer)
            index += 1
    finally:
        _method(factory, 2, release)(factory)
    return tuple(adapters)


def select_directml_device(subsystem_id: int) -> int:
    return choose_directml_device(
        enumerate_dxgi_adapters(), subsystem_id=subsystem_id,
    )
