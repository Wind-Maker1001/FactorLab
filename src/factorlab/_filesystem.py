"""Stable identity for path/handle comparisons across supported CPython builds."""
import os


def stat_identity(info):
    timestamp = (getattr(info, 'st_birthtime_ns', info.st_ctime_ns)
                 if os.name == 'nt' else info.st_ctime_ns)
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, timestamp)
