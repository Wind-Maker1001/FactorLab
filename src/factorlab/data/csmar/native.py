"""Bounded ZIP/XLSX streaming without extracting a library or trusting dimensions.

All values remain native strings (including numeric XML lexemes). This reader
does not evaluate formulas or infer dates, missing sentinels or security IDs.
"""

from contextlib import contextmanager
import hashlib
import ctypes
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import xml.etree.ElementTree as ET
import zipfile

from factorlab.data.evidence import fail, require_hash
from factorlab.data.tdx import safe_path

MAX_WORKBOOK = 192 * 1024 ** 2
MAX_EXPANDED = 4 * 1024 ** 3
MAX_SMALL_XML = 16 * 1024 ** 2
MAX_ROWS = 1048576
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def peak_memory_bytes():
    """OS process lifetime peak; not an estimate of whole-machine memory."""
    if os.name == "nt":
        class Counters(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong)] + [
                (n, ctypes.c_size_t) for n in ("peak_rss", "rss", "peak_pool", "pool", "peak_nonpage", "nonpage", "pagefile", "peak_pagefile")]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError()
        return counters.peak_rss
    if os.name == "posix":
        import resource
        import sys
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024)
    return None


def stat_key(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def digest_stream(stream):
    digest = hashlib.sha256()
    stream.seek(0)
    for block in iter(lambda: stream.read(4 * 1024 ** 2), b""):
        digest.update(block)
    stream.seek(0)
    return digest.hexdigest()


def member_name(name):
    if (not isinstance(name, str) or not name or "\\" in name or ":" in name
            or "\x00" in name or name.startswith("/")
            or any(p in {"", ".", ".."} for p in name.split("/"))
            or PurePosixPath(name).is_absolute()):
        fail("unsafe ZIP member path")
    return name


def check_zip(archive, limit):
    names = set()
    total = 0
    for item in archive.infolist():
        # ZipInfo normalizes backslashes on Windows. Validate original header
        # text before using its normalized filename for any member lookup.
        member_name(item.orig_filename.rstrip("/"))
        name = member_name(item.filename.rstrip("/"))
        if name.casefold() in names:
            fail("duplicate ZIP member")
        names.add(name.casefold())
        if item.flag_bits & 1 or stat.S_ISLNK(item.external_attr >> 16):
            fail("encrypted or linked ZIP member")
        total += item.file_size
        if (item.file_size > limit or total > limit
                or item.file_size > max(1, item.compress_size) * 1000):
            fail("ZIP expansion resource limit")
    if len(names) > 4096:
        fail("ZIP member count limit")


@contextmanager
def source_zip(root, name, expected_sha, expected_bytes):
    # The trust root comes from the operator, never from receipt contents.
    require_hash(expected_sha)
    member_name(name)
    if "/" in name or not name.endswith(".zip"):
        fail("external source must be a ZIP basename under the operator root")
    root = safe_path(Path(root))
    path = safe_path(root / name, root)
    before = path.stat()
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or type(expected_bytes) is not int or before.st_size != expected_bytes):
        fail("source ZIP length/type mismatch")
    with path.open("rb") as handle:
        if stat_key(os.fstat(handle.fileno())) != stat_key(before):
            fail("source changed before open")
        if digest_stream(handle) != expected_sha:
            fail("source ZIP hash mismatch")
        with zipfile.ZipFile(handle) as archive:
            check_zip(archive, 2 * 1024 ** 3)
            yield archive, path
        # Hash the same open object again and check the path after consumption.
        if (digest_stream(handle) != expected_sha
                or stat_key(os.fstat(handle.fileno())) != stat_key(before)
                or stat_key(safe_path(path, root).stat()) != stat_key(before)):
            fail("source changed during parsing")


@contextmanager
def workbook(archive, member, expected_sha, scratch):
    member_name(member)
    if not member.endswith(".xlsx"):
        fail("only native XLSX workbook members supported")
    item = archive.getinfo(member)
    if item.file_size > MAX_WORKBOOK:
        fail("workbook temporary space limit")
    scratch = safe_path(Path(scratch))
    if not scratch.is_dir() or shutil.disk_usage(scratch).free < MAX_WORKBOOK + 1024 ** 3:
        fail("scratch free-space floor")
    # One exclusive temporary file per call; deletion is scoped to this handle.
    with tempfile.TemporaryFile(dir=str(scratch), suffix=".xlsx") as temp:
        digest = hashlib.sha256()
        count = 0
        with archive.open(item) as source:
            for block in iter(lambda: source.read(1024 ** 2), b""):
                count += len(block)
                if count > MAX_WORKBOOK:
                    fail("workbook byte limit")
                digest.update(block)
                temp.write(block)
        if count != item.file_size or digest.hexdigest() != require_hash(expected_sha):
            fail("workbook member hash/length mismatch")
        temp.seek(0)
        with zipfile.ZipFile(temp) as book:
            check_zip(book, MAX_EXPANDED)
            yield book


class SafeXML:
    def __init__(self, stream):
        self.stream = stream
        self.tail = b""
        self.count = 0

    def read(self, size=-1):
        value = self.stream.read(size)
        self.count += len(value)
        combined = (self.tail + value).upper()
        if self.count > MAX_EXPANDED or b"\x00" in combined or b"<!DOCTYPE" in combined or b"<!ENTITY" in combined:
            fail("unsafe XML or expanded byte limit")
        self.tail = combined[-16:]
        return value


def small_xml(book, name):
    if book.getinfo(name).file_size > MAX_SMALL_XML:
        fail("XML metadata/shared strings limit")
    with book.open(name) as stream:
        return ET.fromstring(SafeXML(stream).read())


def sheets(book):
    relationships = small_xml(book, "xl/_rels/workbook.xml.rels")
    targets = {}
    for item in relationships:
        if item.get("TargetMode") == "External":
            fail("external XLSX relationship")
        target = item.get("Target", "")
        # Both standard package-absolute and relative OOXML targets are allowed
        # only after restricting the resulting path to xl/worksheets/*.xml.
        target = target[1:] if target.startswith("/xl/") else "xl/" + target
        member_name(target)
        targets[item.get("Id")] = target
    result = {}
    for item in small_xml(book, "xl/workbook.xml").iter(NS + "sheet"):
        name = item.get("name")
        target = targets.get(item.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"), "")
        if (name in result or not re.fullmatch(r"xl/worksheets/[^/]+\.xml", target)):
            fail("invalid/duplicate sheet relationship")
        result[name] = target
    if not result:
        fail("workbook has no sheets")
    return result


def shared_strings(book):
    if "xl/sharedStrings.xml" not in book.namelist():
        return []
    return ["".join(t.text or "" for t in item.iter(NS + "t"))
            for item in small_xml(book, "xl/sharedStrings.xml")]


def rows(book, xml_member, strings):
    previous = 0
    with book.open(xml_member) as source:
        context = ET.iterparse(SafeXML(source), events=("start", "end"))
        root = None
        sheet_data = None
        for event, node in context:
            if root is None:
                root = node
            if event == "start" and node.tag == NS + "sheetData":
                sheet_data = node
            if event != "end" or node.tag != NS + "row":
                continue
            number = int(node.get("r", "0"))
            if not previous < number <= MAX_ROWS:
                fail("invalid/nonmonotonic native row coordinate")
            previous = number
            values = {}
            for cell in node:
                match = re.fullmatch(r"([A-Z]{1,3})([0-9]+)", cell.get("r", ""))
                if not match or int(match[2]) != number:
                    fail("invalid native cell coordinate")
                column = 0
                for char in match[1]:
                    column = column * 26 + ord(char) - 64
                if column > 256 or column in values or cell.find(NS + "f") is not None:
                    fail("duplicate/unsupported/formula cell")
                kind = cell.get("t", "n")
                if kind == "inlineStr":
                    value = "".join(t.text or "" for t in cell.iter(NS + "t"))
                elif kind in {"n", "s"}:
                    value = cell.findtext(NS + "v")
                    if kind == "s" and value is not None:
                        idx = int(value)
                        if idx < 0 or idx >= len(strings):
                            fail("invalid shared string reference")
                        value = strings[idx]
                else:
                    fail("unsupported XLSX cell type")
                if value is not None and len(value) > 32768:
                    fail("native cell length limit")
                values[column] = value
            yield number, [values.get(i) for i in range(1, max(values, default=0) + 1)]
            node.clear()
            if sheet_data is not None:
                sheet_data.clear()
