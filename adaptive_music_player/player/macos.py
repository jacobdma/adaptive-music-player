import ctypes
import ctypes.util
import sys

ACTIVATION_POLICY_PROHIBITED = 2


def hide_dock_icon() -> None:
    """Keep libmpv from giving the Python process a bouncing Dock icon."""
    if sys.platform != "darwin":
        return
    objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
    ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AppKit.framework/AppKit")
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.sel_registerName.restype = ctypes.c_void_p
    send = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(("objc_msgSend", objc))
    send_long = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long)(
        ("objc_msgSend", objc))
    app = send(objc.objc_getClass(b"NSApplication"), objc.sel_registerName(b"sharedApplication"))
    send_long(app, objc.sel_registerName(b"setActivationPolicy:"), ACTIVATION_POLICY_PROHIBITED)
