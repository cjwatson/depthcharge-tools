#! /usr/bin/env python3

import argparse
import collections
import logging
import os
import pathlib
import re
import shutil
import tempfile

from depthcharge_tools import __version__
from depthcharge_tools.process import (
    gzip,
    lz4,
    lzma,
    cgpt,
    findmnt,
    blockdev,
)

logger = logging.getLogger(__name__)


class Path(pathlib.PosixPath):
    def copy_to(self, dest):
        dest = shutil.copy2(self, dest)
        return Path(dest)

    def is_gzip(self):
        proc = gzip.test(self)
        return proc.returncode == 0

    def gunzip(self, dest=None):
        if dest is None:
            if self.name.endswith(".gz"):
                dest = self.parent / self.name[:-3]
            else:
                dest = self.parent / (self.name + ".gunzip")
        gzip.decompress(self, dest)
        return Path(dest)

    def lz4(self, dest=None):
        if dest is None:
            dest = self.parent / (self.name + ".lz4")
        lz4.compress(self, dest)
        return Path(dest)

    def lzma(self, dest=None):
        if dest is None:
            dest = self.parent / (self.name + ".lzma")
        lzma.compress(self, dest)
        return Path(dest)

    def is_vmlinuz(self):
        return any((
            "vmlinuz" in self.name,
            "vmlinux" in self.name,
            "linux" in self.name,
            "Image" in self.name,
            "kernel" in self.name,
        ))

    def is_initramfs(self):
        return any((
            "initrd" in self.name,
            "initramfs" in self.name,
            "cpio" in self.name,
        ))

    def is_dtb(self):
        return any((
            "dtb" in self.name,
        ))

    def iterdir(self, maybe=True):
        if maybe == False or self.is_dir():
            return super().iterdir()
        else:
            return []

    def read_lines(self, maybe=True):
        if maybe == False or self.is_file():
            return self.read_text().splitlines()
        else:
            return []



class SysDevTree(collections.defaultdict):
    def __init__(self, sys=None, dev=None):
        super().__init__(set)

        sys = Path(sys or "/sys")
        dev = Path(dev or "/dev")

        for sysdir in (sys / "class" / "block").iterdir():
            for device in (sysdir / "dm" / "name").read_lines():
                self.add(dev / "mapper" / device, dev / sysdir.name)

            for device in (sysdir / "slaves").iterdir():
                self.add(dev / sysdir.name, dev / device.name)

            parent = sysdir.resolve().parent
            if parent.parent.name == "block":
                self.add(dev / sysdir.name, dev / parent.name)

        self.sys = sys
        self.dev = dev

    def add(self, child, parent):
        if child.exists() and parent.exists():
            if child != parent:
                self[child].add(parent)

    def leaves(self, *children):
        ls = set()

        if not children:
            ls.update(*self.values())
            ls.difference_update(self.keys())
            return ls

        children = [Path(c).resolve() for c in children]
        while children:
            c = children.pop(0)
            if c in self:
                for p in self[c]:
                    children.append(p)
            else:
                ls.add(c)

        return ls


class Disk:
    tree = SysDevTree()

    def __init__(self, path):
        path = Path(path).resolve()

        if not (path.is_file() or path.is_block_device()):
            fmt = "Disk '{}' is not a file or block device."
            msg = fmt.format(str(path))
            raise ValueError(msg)

        self.path = path

    def disks(*args, bootable=False):
        if bootable:
            boot = Disk.by_mount("/boot")
            if boot is not None:
                args = (*args, boot)

            root = Disk.by_mount("/")
            if root is not None:
                args = (*args, root)

        children = []
        for arg in args:
            if isinstance(arg, Partition):
                children.append(arg.path or arg.disk.path)
            elif isinstance(arg, Disk):
                children.append(arg.path)
            elif arg is not None:
                children.append(arg)

        disks = []
        for path in sorted(Disk.tree.leaves(*children)):
            try:
                dev = Disk(Path("/dev") / path)
                disks.append(dev)
            except ValueError:
                pass

        return disks

    @classmethod
    def by_mount(cls, mnt):
        for fstab in (True, False):
            proc = findmnt.find(mnt, fstab=True)
            if proc.returncode == 0:
                return Path(proc.stdout.strip())

    def partition(self, partno):
        return Partition(self, partno)

    def partitions(self):
        proc = cgpt("find", "-n", "-t", "kernel", self.path)
        return [
            Partition(self, int(n))
            for n in proc.stdout.splitlines()
        ]

    def __repr__(self):
        cls = self.__class__.__name__
        return "{}('{}')".format(cls, self.path)


class Partition:
    def __init__(self, path, partno=None):
        if isinstance(path, Disk):
            disk = path
            path = None
        else:
            disk = None
            path = Path(path).resolve()

        if (
            disk is None
            and partno is None
            and path.parent == Path("/dev")
            and path.is_block_device()
        ):
            match = (
                re.fullmatch("(.*[0-9])p([0-9]+)", path.name)
                or re.fullmatch("(.*[^0-9])([0-9]+)", path.name)
            )
            if match:
                diskname, partno = match.groups()
                partno = int(partno)
                disk = Disk(path.with_name(diskname))

        if disk is None:
            disk = Disk(path)

        if partno is None:
            fmt = "Partition number not given for disk '{}'."
            msg = fmt.format(str(disk))
            raise ValueError(msg)

        elif not (isinstance(partno, int) and partno > 0):
            fmt = "Partition number '{}' must be a positive integer."
            msg = fmt.format(partno)
            raise ValueError(msg)

        elif path is None:
            fmt = "{}p{}" if disk.path.name[-1].isnumeric() else "{}{}"
            name = fmt.format(disk.path.name, partno)
            path = disk.path.with_name(name)

        if not (path.is_file() or path.is_block_device()):
            path = None

        self.disk = disk
        self.path = path
        self.partno = partno

    def attributes(self):
        proc = cgpt("show", "-A", "-i", str(self.partno), self.disk.path)
        attr = int(proc.stdout, 16)
        priority = (attr) & 0xF
        tries = (attr >> 4) & 0xF
        successful = (attr >> 8) & 0x1
        return (priority, tries, successful)

    @property
    def size(self):
        if self.path is not None:
            proc = blockdev("--getsize64", self.path)
            return int(proc.stdout)

        proc = cgpt("show", "-s", "-i", str(self.partno), self.disk.path)
        return int(proc.stdout) * 512

    def __repr__(self):
        cls = self.__class__.__name__
        if self.path is not None:
            return "{}('{}')".format(cls, self.path)
        else:
            return "{}('{}', {})".format(cls, self.disk.path, self.partno)


class Architecture(str):
    arm_32 = ["arm"]
    arm_64 = ["arm64", "aarch64"]
    arm = arm_32 + arm_64
    x86_32 = ["i386", "x86"]
    x86_64 = ["x86_64", "amd64"]
    x86 = x86_32 + x86_64
    all = arm + x86
    groups = (arm_32, arm_64, x86_32, x86_64)

    def __eq__(self, other):
        if isinstance(other, Architecture):
            for group in self.groups:
                if self in group and other in group:
                    return True
        return str(self) == str(other)

    def __ne__(self, other):
        if isinstance(other, Architecture):
            for group in self.groups:
                if self in group and other not in group:
                    return True
        return str(self) != str(other)

    @property
    def mkimage(self):
        if self in self.arm_32:
            return "arm"
        if self in self.arm_64:
            return "arm64"
        if self in self.x86_32:
            return "x86"
        if self in self.x86_64:
            return "x86_64"

    @property
    def vboot(self):
        if self in self.arm_32:
            return "arm"
        if self in self.arm_64:
            return "aarch64"
        if self in self.x86_32:
            return "x86"
        if self in self.x86_64:
            return "amd64"


class TemporaryDirectory(tempfile.TemporaryDirectory):
    def __enter__(self):
        return Path(super().__enter__())


class MixedArgumentsAction(argparse.Action):
    def __init_subclass__(cls, *args, **kwargs):
        super().__init_subclass__(*args, **kwargs)
        cls._selectors = {}
        cls._nargs = {}

    def __init__(self, option_strings, dest, select=None, **kwargs):
        super().__init__(option_strings, dest, **kwargs)
        self._selectors[select] = self.dest
        self._nargs[dest] = self.nargs

    def __call__(self, parser, namespace, values, option_string=None):
        if values is None:
            return
        elif values is getattr(namespace, self.dest):
            return
        elif not isinstance(values, list):
            values = [values]

        for value in values:
            chosen_dest = None
            for select, dest in self._selectors.items():
                if callable(select) and select(value):
                    chosen_dest = dest

            if chosen_dest is not None:
                try:
                    self._set_value(namespace, chosen_dest, value)
                    continue
                except argparse.ArgumentError as err:
                    parser.error(err.message)

            for dest, nargs in self._nargs.items():
                try:
                    self._set_value(namespace, dest, value)
                    break
                except argparse.ArgumentError as err:
                    continue
            else:
                parser.error(err.message)

    def _set_value(self, namespace, dest, value):
        nargs = self._nargs[dest]
        current = getattr(namespace, dest)

        if nargs is None or nargs == "?":
            if current is not None:
                fmt = "Cannot have multiple {} args '{}' and '{}'."
                msg = fmt.format(dest, current, value)
                raise argparse.ArgumentError(self, msg)
            else:
                setattr(namespace, dest, value)

        elif isinstance(nargs, int) and len(current) > nargs:
            fmt = "Cannot have more than {} {} args {}."
            msg = fmt.format(nargs, dest, current + value)
            raise argparse.ArgumentError(self, msg)

        elif current is None:
            setattr(namespace, dest, [value])

        else:
            current.append(value)

