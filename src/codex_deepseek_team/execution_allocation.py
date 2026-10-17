"""Conservative materialization budgets shared by consumers of one filesystem.

These reservations prevent concurrent known allocations from all spending the
same free-space sample. They are admission budgets, not filesystem quotas for
arbitrary future build output. Every affected temporary filesystem is admitted
separately and the operating system releases crashed owners' reservations.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import stat

from . import state_storage, storage_budget

BLOCK = 4096
RUNTIME_BYTES = 256 * 1024 * 1024
RUNTIME_INODES = 8192


def checkout_requirement(source, head):
    """Bound checkout, fetched objects, indexes and transient Git pack storage."""
    from . import workspace
    listing = workspace.git(source, 'ls-tree', '-r', '-l', '-z', head)
    files, payload, directories = 0, 0, set()
    for row in listing.split(b'\0'):
        if not row:
            continue
        header, separator, name = row.partition(b'\t')
        fields = header.split()
        if not separator or len(fields) != 4 or not fields[3].isdigit():
            raise workspace.WorkspaceError('Cannot estimate the pinned source allocation safely.')
        size = int(fields[3])
        payload += ((size + BLOCK - 1) // BLOCK) * BLOCK
        files += 1
        parent = Path(os.fsdecode(name)).parent
        directories.update(parent.parents)
        directories.add(parent)
    # Git can simultaneously hold incoming pack/index and final objects. The
    # per-entry allowance also covers empty blobs, tree objects and directories.
    return {'disk_bytes': 3 * payload + BLOCK * (files + 3 * len(directories) + 256),
            'inodes': 4 * files + 3 * len(directories) + 256}


@contextmanager
def reserve(target, *, disk_bytes, inodes):
    target = Path(target)
    pool = storage_budget.shared_pool_dir(target)
    with storage_budget.reserve(target, allocation_path=target, pool_dir=pool,
                                disk_bytes=disk_bytes, inodes=inodes) as reservation:
        yield reservation


@contextmanager
def checkout(source, head, state):
    requirement = checkout_requirement(source, head)
    with reserve(state, **requirement):
        yield requirement


@contextmanager
def runtime(*targets):
    """Reserve scratch headroom on each actual allocation filesystem."""
    devices = set()
    with ExitStack() as stack:
        for target in targets:
            target = Path(target)
            fd = state_storage.open_private_directory(target)
            try:
                device = os.fstat(fd).st_dev
            finally:
                os.close(fd)
            if device not in devices:
                stack.enter_context(reserve(target, disk_bytes=RUNTIME_BYTES,
                                            inodes=RUNTIME_INODES))
                devices.add(device)
        yield
