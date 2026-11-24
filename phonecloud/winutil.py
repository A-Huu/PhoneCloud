"""与 Windows 环境相关的功能：局域网地址、防火墙提示、开机自启、后台运行、停止服务。"""

from __future__ import annotations

import csv
import ctypes
import io
import os
import socket
import subprocess
import sys
import uuid
from ctypes import wintypes
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "PhoneCloud"
BACKGROUND_CHILD_ENV = "PHONECLOUD_BACKGROUND_CHILD"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


_IF_TYPE_IEEE80211 = 71  # 无线网卡
_IF_OPER_UP = 1


def _usable_ipv4(address: str) -> bool:
    return not address.startswith(("127.", "169.254.", "0."))


def _default_route_ipv4() -> str:
    """当前默认出口网卡的 IPv4。UDP connect 不会真的发包。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            return probe.getsockname()[0]
    except OSError:
        return ""


def _adapter_ipv4() -> list[tuple[str, bool]] | None:
    """正在使用的网卡上的 IPv4：(地址, 是否无线)。接口失败时返回 None，交给调用方用旧办法。"""

    class _SockAddr(ctypes.Structure):
        _fields_ = [("sa_family", wintypes.USHORT), ("sa_data", ctypes.c_char * 14)]

    class _SocketAddress(ctypes.Structure):
        _fields_ = [("lpSockaddr", ctypes.POINTER(_SockAddr)), ("iSockaddrLength", ctypes.c_int)]

    class _Unicast(ctypes.Structure):
        pass

    _Unicast._fields_ = [
        ("Length", wintypes.ULONG),
        ("Flags", wintypes.DWORD),
        ("Next", ctypes.POINTER(_Unicast)),
        ("Address", _SocketAddress),
    ]

    class _Adapter(ctypes.Structure):
        pass

    _Adapter._fields_ = [
        ("Length", wintypes.ULONG),
        ("IfIndex", wintypes.DWORD),
        ("Next", ctypes.POINTER(_Adapter)),
        ("AdapterName", ctypes.c_char_p),
        ("FirstUnicastAddress", ctypes.POINTER(_Unicast)),
        ("FirstAnycastAddress", ctypes.c_void_p),
        ("FirstMulticastAddress", ctypes.c_void_p),
        ("FirstDnsServerAddress", ctypes.c_void_p),
        ("DnsSuffix", ctypes.c_wchar_p),
        ("Description", ctypes.c_wchar_p),
        ("FriendlyName", ctypes.c_wchar_p),
        ("PhysicalAddress", ctypes.c_ubyte * 8),
        ("PhysicalAddressLength", wintypes.ULONG),
        ("Flags", wintypes.DWORD),
        ("Mtu", wintypes.DWORD),
        ("IfType", wintypes.DWORD),
        ("OperStatus", wintypes.DWORD),
    ]

    get_adapters = ctypes.windll.iphlpapi.GetAdaptersAddresses
    size = wintypes.ULONG(16 * 1024)
    buffer = ctypes.create_string_buffer(size.value)
    # 只要单播地址，跳过任播、组播和 DNS
    flags = 0x0002 | 0x0004 | 0x0008
    result = get_adapters(socket.AF_INET, flags, None, ctypes.cast(buffer, ctypes.c_void_p), ctypes.byref(size))
    if result == 111:  # ERROR_BUFFER_OVERFLOW
        buffer = ctypes.create_string_buffer(size.value)
        result = get_adapters(socket.AF_INET, flags, None, ctypes.cast(buffer, ctypes.c_void_p), ctypes.byref(size))
    if result != 0:
        return None

    found: list[tuple[str, bool]] = []
    adapter = ctypes.cast(buffer, ctypes.POINTER(_Adapter))
    while adapter:
        item = adapter.contents
        wireless = item.IfType == _IF_TYPE_IEEE80211
        if item.OperStatus == _IF_OPER_UP:
            unicast = item.FirstUnicastAddress
            while unicast:
                sockaddr = unicast.contents.Address.lpSockaddr
                if sockaddr:
                    raw = ctypes.string_at(sockaddr, 16)
                    if int.from_bytes(raw[0:2], "little") == socket.AF_INET:
                        found.append((socket.inet_ntoa(raw[4:8]), wireless))
                unicast = unicast.contents.Next
        adapter = item.Next
    return found


def order_lan_addresses(adapters: list[tuple[str, bool]], current: str) -> list[str]:
    """无线网卡排在最前。同一类里，当前默认出口的地址排第一，其余按地址排序。"""
    wireless: list[str] = []
    other: list[str] = []
    seen: set[str] = set()
    for address, is_wireless in adapters:
        if address in seen or not _usable_ipv4(address):
            continue
        seen.add(address)
        (wireless if is_wireless else other).append(address)

    def lead(group: list[str]) -> list[str]:
        ordered = sorted(group)
        if current in ordered:
            ordered.remove(current)
            ordered.insert(0, current)
        return ordered

    return lead(wireless) + lead(other)


def lan_addresses() -> list[str]:
    """本机的局域网 IPv4。手机默认用无线网卡；默认出口若是 VPN，不会抢到第一位。"""
    current = _default_route_ipv4()
    adapters = _adapter_ipv4()
    if adapters is not None:
        if current and _usable_ipv4(current) and all(address != current for address, _ in adapters):
            adapters.append((current, False))
        return order_lan_addresses(adapters, current)

    addresses: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(str(info[4][0]))
    except OSError:
        pass
    if current:
        addresses.add(current)
    return order_lan_addresses([(address, False) for address in addresses], current)


def qr_svg(text: str) -> str:
    """把访问地址画成二维码 SVG，手机相机可以直接扫。"""
    import segno

    return segno.make(text, error="m").svg_inline(scale=4, dark="#1c2430", light="#ffffff", omitsize=True)


def firewall_command(port: int) -> str:
    return (
        f'netsh advfirewall firewall add rule name="PhoneCloud" dir=in action=allow '
        f"protocol=TCP localport={port} profile=private"
    )


def private_network_command() -> str:
    """把当前网络类别改成“专用”。防火墙放行只对专用网络生效。"""
    return (
        'powershell -NoProfile -Command "Get-NetConnectionProfile | '
        'Set-NetConnectionProfile -NetworkCategory Private"'
    )


def port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


# ---- 启动命令 / 开机自启 ----------------------------------------------


def launch_args(home: Path, *, background: bool) -> list[str]:
    if getattr(sys, "frozen", False):
        args = [sys.executable]
    else:
        args = [sys.executable, str(Path(__file__).resolve().parent.parent / "run.py")]
    args += ["serve", "--home", str(home)]
    if background:
        args.append("--background")
    return args


def install_autostart(home: Path, *, key_path: str = RUN_KEY, value_name: str = RUN_VALUE) -> str:
    """写入当前用户的启动项（登录 Windows 后自动在后台启动），返回写入的命令。"""
    import winreg

    command = subprocess.list2cmdline(launch_args(home, background=True))
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, command)
    return command


def uninstall_autostart(*, key_path: str = RUN_KEY, value_name: str = RUN_VALUE) -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, value_name)
    except FileNotFoundError:
        return False
    return True


def autostart_command(*, key_path: str = RUN_KEY, value_name: str = RUN_VALUE) -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            return str(winreg.QueryValueEx(key, value_name)[0])
    except FileNotFoundError:
        return None


# ---- 后台运行 / 停止 --------------------------------------------------


def spawn_background(args: list[str], cwd: Path) -> int:
    """以无窗口的独立进程重新启动自己，返回子进程 PID。"""
    env = dict(os.environ)
    env[BACKGROUND_CHILD_ENV] = "1"
    # 单文件 exe 运行时会解压到临时目录，父进程退出时会删掉它。
    # 这个变量让子进程当作全新实例自己解压一份，而不是共用父进程即将删除的那份。
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    process = subprocess.Popen(
        args,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        close_fds=True,
    )
    return process.pid


def process_image(pid: int) -> str | None:
    """返回 PID 对应的进程映像名；进程不存在时返回 None。"""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            errors="replace",
            creationflags=_NO_WINDOW,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for row in csv.reader(io.StringIO(result.stdout)):
        if len(row) >= 2 and row[1].strip() == str(pid):
            return row[0].strip()
    return None


class _SHFILEOPSTRUCTW(ctypes.Structure):
    """SHFileOperationW 的参数。默认对齐即可，64 位上 fFlags 后面的空隙由 ctypes 补上。"""

    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_wchar_p),
        ("pTo", ctypes.c_wchar_p),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_wchar_p),
    ]


_FO_DELETE = 3
_FOF_SILENT = 0x0004
_FOF_NOCONFIRMATION = 0x0010
_FOF_ALLOWUNDO = 0x0040
_FOF_NOERRORUI = 0x0400


def recycle(path: Path) -> None:
    """把文件或文件夹移进 Windows 回收站，不直接永久删除。失败时抛 OSError。"""
    if os.name != "nt":
        raise OSError("回收站只在 Windows 上可用")
    target = str(path.resolve())
    # 路径列表以双 NUL 结尾；ctypes 还会再补一个结尾 NUL
    source = ctypes.create_unicode_buffer(target + "\0")
    op = _SHFILEOPSTRUCTW()
    op.wFunc = _FO_DELETE
    op.pFrom = ctypes.cast(source, ctypes.c_wchar_p)
    op.fFlags = _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI
    shell32 = ctypes.windll.shell32
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(_SHFILEOPSTRUCTW)]
    shell32.SHFileOperationW.restype = ctypes.c_int
    code = shell32.SHFileOperationW(ctypes.byref(op))
    if code != 0 or op.fAnyOperationsAborted:
        raise OSError(code or 1223, "无法移到回收站", target)


def attach_parent_console() -> None:
    """窗口版 exe 从命令行启动时，把输出接回那个窗口。双击没有父窗口，就保持安静。"""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    kernel32 = ctypes.windll.kernel32
    kernel32.AttachConsole.argtypes = [wintypes.DWORD]
    kernel32.AttachConsole.restype = wintypes.BOOL
    if not kernel32.AttachConsole(0xFFFFFFFF):
        return
    try:
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        return


def tell(message: str, *, error: bool = False) -> None:
    """没有控制台时用一个小对话框说明结果。有控制台时只打印，不额外弹窗。"""
    stream = sys.stderr if error else sys.stdout
    if stream is not None:
        print(message, file=stream, flush=True)
        return
    if os.name != "nt":
        return
    user32 = ctypes.windll.user32
    user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
    user32.MessageBoxW.restype = ctypes.c_int
    flags = 0x00000010 if error else 0x00000040  # 错误图标 / 信息图标
    user32.MessageBoxW(None, message, "PhoneCloud", flags | 0x00010000)


# 快捷方式：打开（无参数，等于 start）、重新启动、停止。
_SHORTCUTS = (
    ("打开 PhoneCloud.lnk", ""),
    ("重新启动.lnk", "restart"),
    ("停止.lnk", "stop"),
)


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", wintypes.BYTE * 8),
    ]


def _guid(text: str) -> _GUID:
    parsed = uuid.UUID(text)
    value = _GUID()
    value.Data1, value.Data2, value.Data3 = parsed.time_low, parsed.time_mid, parsed.time_hi_version
    for index, byte in enumerate(parsed.bytes[8:]):
        value.Data4[index] = byte
    return value


def _com_method(vtable: ctypes.Array, index: int, restype: type, *argtypes: type):  # type: ignore[type-arg]
    prototype = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return prototype(vtable[index])


def _write_shortcut(path: Path, target: Path, arguments: str, workdir: Path) -> None:
    """用系统自带的 Shell Link 写 .lnk，不额外带库。"""
    ole32 = ctypes.windll.ole32
    ole32.CoInitialize.argtypes = [ctypes.c_void_p]
    ole32.CoInitialize.restype = ctypes.c_long
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(_GUID),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoCreateInstance.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []

    started = ole32.CoInitialize(None)
    if started < 0:
        raise OSError(started, "无法初始化快捷方式")
    shell_link = ctypes.c_void_p()
    try:
        clsid = _guid("00021401-0000-0000-C000-000000000046")
        iid = _guid("000214F9-0000-0000-C000-000000000046")
        created = ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(shell_link))
        if created < 0 or not shell_link.value:
            raise OSError(created, "无法创建快捷方式")
        table = ctypes.cast(shell_link, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        hresult = ctypes.c_long
        _com_method(table, 20, hresult, wintypes.LPCWSTR)(shell_link, str(target))
        _com_method(table, 9, hresult, wintypes.LPCWSTR)(shell_link, str(workdir))
        _com_method(table, 11, hresult, wintypes.LPCWSTR)(shell_link, arguments)
        _com_method(table, 17, hresult, wintypes.LPCWSTR, ctypes.c_int)(shell_link, f"{target},0", 0)
        _com_method(table, 7, hresult, wintypes.LPCWSTR)(shell_link, "PhoneCloud")

        persist = ctypes.c_void_p()
        persist_iid = _guid("0000010B-0000-0000-C000-000000000046")
        queried = _com_method(table, 0, hresult, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))(
            shell_link, ctypes.byref(persist_iid), ctypes.byref(persist)
        )
        if queried < 0 or not persist.value:
            raise OSError(queried, "无法保存快捷方式")
        persist_table = ctypes.cast(persist, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        saved = _com_method(persist_table, 6, hresult, wintypes.LPCWSTR, wintypes.BOOL)(persist, str(path), True)
        _com_method(persist_table, 2, ctypes.c_ulong)(persist)
        if saved < 0:
            raise OSError(saved, "无法保存快捷方式")
    finally:
        if shell_link.value:
            table = ctypes.cast(shell_link, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
            _com_method(table, 2, ctypes.c_ulong)(shell_link)
        ole32.CoUninitialize()


def ensure_shortcuts(exe: Path) -> None:
    """在 exe 旁边放三个快捷方式。每次覆盖，移动 exe 之后下一次启动会指到新位置。"""
    exe = Path(exe)
    folder = exe.parent
    for name, arguments in _SHORTCUTS:
        _write_shortcut(folder / name, exe, arguments, folder)


def kill_process(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"], capture_output=True, creationflags=_NO_WINDOW, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0
