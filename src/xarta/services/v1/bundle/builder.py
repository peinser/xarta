from __future__ import annotations

import io
import zipfile

from collections.abc import Iterable

from xarta.protocol.document.request import bundle as bundle_protocol


def build_zip(
    members: Iterable[tuple[str, bytes]],
    compression: bundle_protocol.DocumentBundleRequestCompressionOptions,
) -> bytes:
    """Build byte-identical ZIP output for the same named members and options."""
    compression_method = (
        bundle_protocol.DocumentBundleRequestCompressionMethod.interpret(
            compression.method
        )
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w") as archive:
        for filename, data in sorted(members, key=lambda member: member[0]):
            entry = zipfile.ZipInfo(filename, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = compression_method
            entry.create_system = 3
            entry.external_attr = 0o600 << 16
            archive.writestr(
                entry,
                data,
                compress_type=compression_method,
                compresslevel=compression.level,
            )
    return output.getvalue()
