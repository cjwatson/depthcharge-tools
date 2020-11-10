#! /usr/bin/env python3

import argparse
import logging
import platform
import subprocess
import sys

from depthcharge_tools import __version__
from depthcharge_tools.utils import (
    Architecture,
    Path,
    TemporaryDirectory,
    MixedArgumentsAction,
)

logger = logging.getLogger(__name__)


def main(*argv):
    prog, *argv = argv
    parser = argument_parser()
    args = parser.parse_args(*argv)
    kwargs = vars(args)

    try:
        mkdepthcharge(**kwargs)
    except ValueError as err:
        parser.error(err.args[0])


def mkdepthcharge(
    arch=None,
    bootloader=None,
    cmdline=None,
    compress=None,
    devkeys=None,
    dtbs=None,
    image_format=None,
    initramfs=None,
    kern_guid=None,
    keyblock=None,
    name=None,
    output=None,
    signprivate=None,
    verbose=None,
    vmlinuz=None,
):
    # Set defaults
    if arch is None:
        arch = Architecture(platform.machine())

    if image_format is None:
        if arch in Architecture.arm:
            image_format = "fit"
        elif arch in Architecture.x86:
            image_format = "zimage"

    if cmdline is None:
        cmdline = ["--"]

    if devkeys is None:
        if keyblock is None and signprivate is None:
            devkeys = Path("/usr/share/vboot/devkeys")
        elif keyblock is not None and signprivate is not None:
            if keyblock.parent == signprivate.parent:
                devkeys = signprivate.parent
        elif signprivate is not None:
            devkeys = signprivate.parent
        elif keyblock is not None:
            devkeys = keyblock.parent

    if keyblock is None:
        keyblock = devkeys / "kernel.keyblock"
    if signprivate is None:
        signprivate = devkeys / "kernel_data_key.vbprivk"

    # vmlinuz is required but might be missing due to argparse hacks
    if vmlinuz is None:
        msg = "the following arguments are required: vmlinuz"
        raise ValueError(msg)

    # Check incompatible combinations
    if image_format == "zimage":
        if compress is not None:
            msg = "compress argument not supported with zimage format."
            raise ValueError(msg)
        if name is not None:
            msg = "name argument not supported with zimage format."
            raise ValueError(msg)
        if initramfs is not None:
            msg = "Initramfs image not supported with zimage format."
            raise ValueError(msg)
        if dtbs:
            msg = "Device tree files not supported with zimage format."
            raise ValueError(msg)

    with TemporaryDirectory(prefix="mkdepthcharge-") as tmpdir:
        vmlinuz = vmlinuz.copy_to(tmpdir)
        if vmlinuz.is_gzip():
            vmlinuz = vmlinuz.gunzip()

        if initramfs is not None:
            initramfs = initramfs.copy_to(tmpdir)

        dtbs = [dtb.copy_to(tmpdir) for dtb in dtbs]

        if compress == "lz4":
            vmlinuz = vmlinuz.lz4()
        elif compress == "lzma":
            vmlinuz = vmlinuz.lzma()

        if kern_guid:
            cmdline.insert(0, "kern_guid=%U")
        cmdline = " ".join(cmdline)
        cmdline_file = tmpdir / "kernel.args"
        cmdline_file.write_text(cmdline)

        if bootloader is not None:
            bootloader = bootloader.copy_to(tmpdir)
        else:
            bootloader = tmpdir / "bootloader.bin"
            bootloader.write_bytes(bytes(512))

        if image_format == "fit":
            fit_image = tmpdir / "depthcharge.fit"

            if name is None:
                name = "unavailable"
            if compress is None:
                compress = "none"

            mkimage_cmd = [
                "mkimage",
                "-f", "auto",
                "-A", arch.mkimage,
                "-O", "linux",
                "-C", compress,
                "-n", name,
                "-d", vmlinuz,
            ]
            if initramfs:
                mkimage_cmd += ["-i", initramfs]
            for dtb in dtbs:
                mkimage_cmd += ["-b", dtb]
            mkimage_cmd.append(fit_image)
            subprocess.run(mkimage_cmd, check=True)

            vmlinuz_vboot = fit_image

        elif image_format == "zimage":
            vmlinuz_vboot = vmlinuz

        vboot_cmd = [
            "futility", "vbutil_kernel",
            "--version", "1",
            "--arch", arch.vboot,
            "--vmlinuz", vmlinuz_vboot,
            "--config", cmdline_file,
            "--bootloader", bootloader,
            "--keyblock", keyblock,
            "--signprivate", signprivate,
            "--pack", output
        ]
        subprocess.run(vboot_cmd, check=True)

        verify_cmd = [
            "futility", "vbutil_kernel",
            "--verify", output,
        ]


def argument_parser():
    parser = argparse.ArgumentParser(
        description="Build boot images for the ChromeOS bootloader.",
        usage="%(prog)s [options] -o FILE [--] vmlinuz [initramfs] [dtb ...]",
        add_help=False,
    )

    class InputFileAction(MixedArgumentsAction):
        pass

    input_files = parser.add_argument_group(
        title="Input files",
    )
    input_files.add_argument(
        "vmlinuz",
        action=InputFileAction,
        select=Path.is_vmlinuz,
        type=Path,
        help="Kernel executable",
    )
    input_files.add_argument(
        "initramfs",
        nargs="?",
        action=InputFileAction,
        select=Path.is_initramfs,
        type=Path,
        help="Ramdisk image",
    )
    input_files.add_argument(
        "dtbs",
        metavar="dtb",
        nargs="*",
        default=[],
        action=InputFileAction,
        select=Path.is_dtb,
        type=Path,
        help="Device-tree binary file",
    )

    options = parser.add_argument_group(
        title="Options",
    )
    options.add_argument(
        "-h", "--help",
        action='help',
        help="Show this help message.",
    )
    options.add_argument(
        "--version",
        action='version',
        version="depthcharge-tools %(prog)s {}".format(__version__),
        help="Print program version.",
    )
    options.add_argument(
        "-v", "--verbose",
        action='store_true',
        help="Print more detailed output.",
    )
    options.add_argument(
        "-o", "--output",
        metavar="FILE",
        action='store',
        required=True,
        type=Path,
        help="Write resulting image to FILE.",
    )
    options.add_argument(
        "-A", "--arch",
        metavar="ARCH",
        action='store',
        choices=Architecture.all,
        type=Architecture,
        help="Architecture to build for.",
    )
    options.add_argument(
        "--format",
        dest="image_format",
        metavar="FORMAT",
        action='store',
        choices=["fit", "zimage"],
        help="Kernel image format to use.",
    )

    def compress_type(s):
        return None if s == "none" else s

    fit_options = parser.add_argument_group(
        title="FIT image options",
    )
    fit_options.add_argument(
        "-C", "--compress",
        metavar="TYPE",
        action='store',
        choices=[None, "lz4", "lzma"],
        type=compress_type,
        help="Compress vmlinuz file before packing.",
    )
    fit_options.add_argument(
        "-n", "--name",
        metavar="DESC",
        action='store',
        help="Description of vmlinuz to put in the FIT.",
    )

    vboot_options = parser.add_argument_group(
        title="Depthcharge image options",
    )
    vboot_options.add_argument(
        "-c", "--cmdline",
        metavar="CMD",
        action='append',
        help="Command-line parameters for the kernel.",
    )
    vboot_options.add_argument(
        "--no-kern-guid",
        dest='kern_guid',
        action='store_false',
        help="Don't prepend kern_guid=%%U to the cmdline.",
    )
    vboot_options.add_argument(
        "--bootloader",
        metavar="FILE",
        action='store',
        type=Path,
        help="Bootloader stub binary to use.",
    )
    vboot_options.add_argument(
        "--devkeys",
        metavar="DIR",
        action='store',
        type=Path,
        help="Directory containing developer keys to use.",
    )
    vboot_options.add_argument(
        "--keyblock",
        metavar="FILE",
        action='store',
        type=Path,
        help="The key block file (.keyblock).",
    )
    vboot_options.add_argument(
        "--signprivate",
        metavar="FILE",
        action='store',
        type=Path,
        help="Private key (.vbprivk) to sign the image.",
    )

    return parser


if __name__ == "__main__":
    main(sys.argv)
