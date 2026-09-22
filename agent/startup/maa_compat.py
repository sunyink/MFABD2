"""Missing ctypes signatures in MaaFw 5.12.2's public post_shell binding."""

import ctypes


def ensure_shell_bindings():
    from maa.define import MaaBool, MaaControllerHandle, MaaCtrlId, MaaStringBufferHandle
    from maa.library import Library

    library = Library.framework()
    # Signatures are from v5.12.2 include/MaaFramework/Instance/MaaController.h.
    # Leave a future SDK's own declarations intact. No library files are edited.
    signatures = (
        ("MaaControllerPostShell", MaaCtrlId, [MaaControllerHandle, ctypes.c_char_p, ctypes.c_int64]),
        ("MaaControllerGetShellOutput", MaaBool, [MaaControllerHandle, MaaStringBufferHandle]),
    )
    for name, result, args in signatures:
        function = getattr(library, name)
        if function.argtypes is None:
            function.restype = result
            function.argtypes = args
