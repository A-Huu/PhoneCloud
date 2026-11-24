"""命令行入口：启动服务、开机自启、重置管理员、停止服务。"""

from __future__ import annotations

import argparse
import getpass
import http.client
import json
import logging
import os
import sys
import time
import urllib.request
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import APP_NAME, __version__, winutil
from .config import ConfigError, Settings, load_settings

log = logging.getLogger("phonecloud")
_COMMANDS = {"start", "restart", "serve", "install-autostart", "uninstall-autostart", "reset-admin", "stop"}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="phonecloud", description=f"{APP_NAME} {__version__}：局域网私有云盘")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    sub = parser.add_subparsers(dest="command")

    def add_home(p: argparse.ArgumentParser) -> None:
        p.add_argument("--home", type=Path, help="主目录（存放 accounts、data、config.json），默认是 exe 所在目录")

    def add_listen(p: argparse.ArgumentParser) -> None:
        p.add_argument("--port", type=int, help="监听端口，覆盖 config.json")
        p.add_argument("--host", help="监听地址，覆盖 config.json")

    start = sub.add_parser("start", help="在后台启动并打开浏览器（双击 exe、不带任何参数时的默认命令）")
    add_home(start)
    add_listen(start)
    start.add_argument("--no-browser", action="store_true", help="只在后台启动，不打开浏览器")

    restart = sub.add_parser("restart", help="先停止再后台启动，并打开管理页")
    add_home(restart)
    add_listen(restart)
    restart.add_argument("--no-browser", action="store_true", help="重新启动后不打开浏览器")

    serve = sub.add_parser("serve", help="在当前窗口前台运行，关闭窗口即停止")
    add_home(serve)
    add_listen(serve)
    serve.add_argument("--background", action="store_true", help="在后台无窗口运行")

    install = sub.add_parser("install-autostart", help="登录 Windows 后自动在后台启动")
    add_home(install)
    sub.add_parser("uninstall-autostart", help="取消开机自启")

    reset = sub.add_parser("reset-admin", help="创建管理员或重置其密码")
    add_home(reset)
    reset.add_argument("--username", default="admin", help="管理员账号名，默认 admin")

    stop = sub.add_parser("stop", help="停止正在后台运行的服务")
    add_home(stop)
    return parser


def _setup_logging(settings: Settings) -> None:
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    file_handler = RotatingFileHandler(
        settings.logs_dir / "phonecloud.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        root.addHandler(console)


def _say(message: str) -> None:
    if sys.stdout is not None:
        print(message, flush=True)


def _pause() -> None:
    # 从命令行运行且出错时，停一下让用户看得到提示。双击没有控制台，不走这里。
    if getattr(sys, "frozen", False) and sys.stdin is not None and sys.stdin.isatty():
        try:
            input("按回车键退出...")
        except EOFError:
            pass


def _fail(message: str, *, pause: bool = True) -> int:
    if sys.stderr is not None:
        print(f"\n[错误] {message}", file=sys.stderr, flush=True)
        if pause:
            _pause()
    else:
        winutil.tell(message, error=True)
    return 2


def _local_url(settings: Settings) -> str:
    host = "127.0.0.1" if settings.host in ("0.0.0.0", "") else settings.host
    return f"http://{host}:{settings.port}"


def _ping(settings: Settings) -> bool:
    """配置的端口上是否有一个 PhoneCloud 在应答（而不是别的程序恰好占了这个端口）。"""
    # 不走系统代理：探测的是本机地址
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"{_local_url(settings)}/api/ping", timeout=2) as response:
            body = json.load(response)
    except (OSError, ValueError, http.client.HTTPException):
        return False
    return isinstance(body, dict) and body.get("name") == APP_NAME


def _wait_until_up(settings: Settings, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _ping(settings):
            return True
        time.sleep(0.5)
    return False


def _launch_in_background(settings: Settings, args: argparse.Namespace) -> bool:
    """以无窗口的独立进程启动服务，等到它能应答请求为止。"""
    winutil.spawn_background(winutil.launch_args(settings.home, background=True) + _overrides(args), settings.home)
    return _wait_until_up(settings)


def _start(args: argparse.Namespace) -> int:
    """双击 exe 的默认行为：服务放到后台运行，然后打开浏览器让用户知道它已经在跑。"""
    settings = load_settings(args.home, port=args.port, host=args.host)
    if _ping(settings):
        _say(f"{APP_NAME} 已经在后台运行。")  # 重复双击：不再启动第二个，直接打开页面
    elif not winutil.port_available(settings.host, settings.port):
        return _fail(f"端口 {settings.port} 已被其他程序占用，请在 {settings.config_path} 里把 port 换成别的端口。")
    else:
        _say(f"{APP_NAME} 正在后台启动，请稍候...")
        if not _launch_in_background(settings, args):
            return _fail(f"启动失败，请查看日志：{settings.logs_dir / 'phonecloud.log'}")
        _say(f"{APP_NAME} 已在后台运行。旁边的「停止」可以关掉它。")

    # 打开管理页：首次使用在这里创建管理员，平时在"概览"里能看到手机该访问的地址
    url = f"{_local_url(settings)}/admin/"
    if args.no_browser:
        _say(f"管理页：{url}")
    elif not webbrowser.open(url):
        return _fail(f"无法自动打开浏览器，请手动访问：{url}", pause=False)
    return 0


def _print_banner(settings: Settings, needs_setup: bool) -> None:
    lines = [
        "",
        f"  {APP_NAME} {__version__} 已启动",
        f"  主目录：{settings.home}",
        f"  本机访问：{_local_url(settings)}",
    ]
    for ip in winutil.lan_addresses():
        lines.append(f"  手机/其他电脑访问：http://{ip}:{settings.port}")
    lines.append(f"  管理页：{_local_url(settings)}/admin/")
    if needs_setup:
        lines.append("  首次使用：请在本机浏览器打开上面的管理页，创建管理员账号。")
    lines += [
        "  如果手机打不开，请在 Windows 防火墙中放行该端口（管理员权限的命令行执行）：",
        f"    {winutil.firewall_command(settings.port)}",
        "  关闭本窗口或按 Ctrl+C 即可停止服务。",
        "",
    ]
    _say("\n".join(lines))


def _serve(args: argparse.Namespace) -> int:
    settings = load_settings(args.home, port=args.port, host=args.host)
    is_child = os.environ.get(winutil.BACKGROUND_CHILD_ENV) == "1"

    if not winutil.port_available(settings.host, settings.port):
        return _fail(
            f"端口 {settings.port} 已被占用。{APP_NAME} 可能已在运行；也可以在 config.json 里换一个端口。",
            pause=not args.background,  # 开机自启时不要留一个等回车的窗口
        )

    if args.background and not is_child:
        if _launch_in_background(settings, args):
            _say(f"{APP_NAME} 已在后台运行：{_local_url(settings)}")
            return 0
        return _fail(f"后台启动失败，请查看日志：{settings.logs_dir / 'phonecloud.log'}", pause=False)

    # 这些模块较重，放到真正要启动服务时再导入，其它子命令可以秒开
    import uvicorn

    from .app import create_app

    _setup_logging(settings)
    app = create_app(settings)
    core = app.state.core
    removed = core.cleanup_stale_uploads()
    if removed:
        log.info("清理了 %d 个上次未完成的上传临时文件", removed)
    settings.pid_path.write_text(str(os.getpid()), encoding="ascii")
    log.info("%s %s 启动，主目录 %s，端口 %d", APP_NAME, __version__, settings.home, settings.port)
    if not is_child:
        _print_banner(settings, needs_setup=not core.has_admin())
    try:
        uvicorn.run(
            app,
            host=settings.host,
            port=settings.port,
            log_config=None,
            access_log=settings.access_log,
            server_header=False,
            # 直接面向局域网、前面没有反向代理：不采信 X-Forwarded-For，
            # "是否本机访问"的判断只看真实的连接来源
            proxy_headers=False,
        )
    finally:
        try:
            settings.pid_path.unlink()
        except OSError:
            pass
    return 0


def _overrides(args: argparse.Namespace) -> list[str]:
    extra: list[str] = []
    if args.port is not None:
        extra += ["--port", str(args.port)]
    if args.host is not None:
        extra += ["--host", args.host]
    return extra


def _install_autostart(args: argparse.Namespace) -> int:
    settings = load_settings(args.home)
    command = winutil.install_autostart(settings.home)
    _say("已设置开机自启：登录 Windows 后会自动在后台启动。")
    _say(f"启动命令：{command}")
    _say("注意：之后如果移动了 exe 或主目录，需要重新执行本命令。")
    return 0


def _uninstall_autostart(args: argparse.Namespace) -> int:
    _say("已取消开机自启。" if winutil.uninstall_autostart() else "当前没有设置开机自启。")
    return 0


def _reset_admin(args: argparse.Namespace) -> int:
    from .core import AppError, Core

    settings = load_settings(args.home)
    core = Core(settings)
    core.init()
    password = getpass.getpass(f"请输入管理员 {args.username} 的新密码：")
    if password != getpass.getpass("请再输入一次："):
        return _fail("两次输入的密码不一致")
    try:
        user = core.upsert_admin(args.username, password)
    except AppError as exc:
        return _fail(exc.message)
    _say(f"管理员 {user.username} 的密码已设置，原有登录已全部失效。")
    return 0


def _stop(args: argparse.Namespace, *, quiet: bool = False) -> int:
    settings = load_settings(args.home, create=False)

    def report(message: str) -> None:
        if quiet:
            return
        if sys.stdout is not None:
            print(message, flush=True)
        else:
            winutil.tell(message)

    try:
        pid = int(settings.pid_path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        report("没有找到正在运行的服务。")
        return 1
    image = (winutil.process_image(pid) or "").lower()
    # PID 可能已被系统分配给别的程序，核对进程名后再结束，避免误杀
    if not image.startswith(("phonecloud", "python")):
        settings.pid_path.unlink(missing_ok=True)
        report("服务并未在运行（已清理残留的 PID 文件）。")
        return 1
    if not winutil.kill_process(pid):
        return _fail(f"无法结束进程 {pid}，请在任务管理器中手动结束。")
    settings.pid_path.unlink(missing_ok=True)
    report(f"{APP_NAME} 已停止。")
    return 0


def _restart(args: argparse.Namespace) -> int:
    if _stop(args, quiet=True) == 2:
        return 2
    return _start(args)


def parse_args(argv: list[str]) -> argparse.Namespace:
    argv = list(argv)
    # 双击 exe（不带参数）或只带选项时，默认执行 start：后台启动并打开浏览器
    if not argv or (argv[0] not in _COMMANDS and argv[0] not in ("-h", "--help", "--version")):
        argv.insert(0, "start")
    return _build_parser().parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    winutil.attach_parent_console()
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")  # 控制台编码不支持的字符不至于让程序崩溃
    if getattr(sys, "frozen", False):
        try:
            winutil.ensure_shortcuts(Path(sys.executable))
        except OSError:
            pass

    args = parse_args(sys.argv[1:] if argv is None else argv)
    handlers = {
        "start": _start,
        "restart": _restart,
        "serve": _serve,
        "install-autostart": _install_autostart,
        "uninstall-autostart": _uninstall_autostart,
        "reset-admin": _reset_admin,
        "stop": _stop,
    }
    try:
        return handlers[args.command](args)
    except ConfigError as exc:
        return _fail(str(exc))
    except KeyboardInterrupt:
        return 130
