"""Precisely classify a denied Windows lock creation without guessing from errno.

Used only after os.open(O_CREAT | O_EXCL) reports PermissionError. NtCreateFile
returns the status of *this* exclusive creation, avoiding stale thread last-status
or a racy metadata probe. FILE_CREATE never opens/truncates an existing lock.
"""
import ctypes
from ctypes import wintypes
from functools import lru_cache
import os


class DeletePendingError(OSError):
    """The exclusive native creation returned STATUS_DELETE_PENDING."""


@lru_cache(maxsize=1)
def _api():
    class UnicodeString(ctypes.Structure):
        _fields_ = [('length', wintypes.USHORT), ('maximum_length', wintypes.USHORT),
                    ('buffer', ctypes.c_void_p)]

    class ObjectAttributes(ctypes.Structure):
        _fields_ = [('length', wintypes.ULONG), ('root', wintypes.HANDLE),
                    ('name', ctypes.POINTER(UnicodeString)), ('attributes', wintypes.ULONG),
                    ('security', ctypes.c_void_p), ('qos', ctypes.c_void_p)]

    class IoStatusBlock(ctypes.Structure):
        _fields_ = [('status_or_pointer', ctypes.c_void_p), ('information', ctypes.c_size_t)]

    nt = ctypes.WinDLL('ntdll')
    nt.NtCreateFile.argtypes = [ctypes.POINTER(wintypes.HANDLE), wintypes.ULONG,
                               ctypes.POINTER(ObjectAttributes), ctypes.POINTER(IoStatusBlock),
                               ctypes.c_void_p, wintypes.ULONG, wintypes.ULONG,
                               wintypes.ULONG, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG]
    nt.NtCreateFile.restype = wintypes.LONG
    nt.RtlNtStatusToDosError.argtypes = [wintypes.LONG]
    nt.RtlNtStatusToDosError.restype = wintypes.ULONG
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return nt, kernel, UnicodeString, ObjectAttributes, IoStatusBlock


def _create(path):
    nt, kernel, UnicodeString, ObjectAttributes, IoStatusBlock = _api()
    absolute = os.path.abspath(path)
    if absolute.startswith('\\\\?\\'):
        name = '\\??\\' + absolute[4:]
    elif absolute.startswith('\\\\'):
        name = '\\??\\UNC\\' + absolute[2:]
    else:
        name = '\\??\\' + absolute
    length = len(name.encode('utf-16-le'))
    if length > 65532:
        raise ValueError('native lock path exceeds UNICODE_STRING capacity')
    buffer = ctypes.create_unicode_buffer(name)
    string = UnicodeString(length, length + 2, ctypes.cast(buffer, ctypes.c_void_p))
    attributes = ObjectAttributes(ctypes.sizeof(ObjectAttributes), None, ctypes.pointer(string),
                                  0x40, None, None)  # OBJ_CASE_INSENSITIVE; inherited DACL
    handle, io = wintypes.HANDLE(), IoStatusBlock()
    # GENERIC_WRITE | SYNCHRONIZE, share READ | WRITE (no DELETE), FILE_CREATE,
    # synchronous non-alertable I/O and non-directory file. No inheritable handle.
    status = nt.NtCreateFile(ctypes.byref(handle), 0x40100000, ctypes.byref(attributes),
                             ctypes.byref(io), None, 0x80, 3, 2, 0x60, None, 0) & 0xffffffff
    return status, handle.value


def create_exclusive(path):
    """Return an owned CRT descriptor, or a status-bound error; never retry here."""
    import msvcrt

    status, handle = _create(path)
    if status != 0:
        if status == 0xC0000056:  # STATUS_DELETE_PENDING, not STATUS_ACCESS_DENIED
            exc = DeletePendingError('exclusive signal lock is pending deletion')
        else:
            exc = ctypes.WinError(_api()[0].RtlNtStatusToDosError(ctypes.c_long(status).value))
        exc.ntstatus = status
        raise exc
    try:
        # Ownership transfers to the CRT only on success. The context manager
        # writes/fsyncs/closes this descriptor exactly like the os.open descriptor.
        return msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY | os.O_NOINHERIT)
    except BaseException:
        _api()[1].CloseHandle(handle)
        # Leave any created lock in place: conversion failure cannot authorize a writer.
        raise
