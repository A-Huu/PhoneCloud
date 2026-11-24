from __future__ import annotations

import os
import subprocess

import pytest

from phonecloud import cli, winutil


def test_no_arguments_means_start_in_background():
    """双击 exe 不带任何参数：后台启动并打开浏览器。"""
    args = cli.parse_args([])
    assert (args.command, args.no_browser, args.home, args.port) == ("start", False, None, None)
    # 只带选项、没写子命令时同样按 start 处理
    assert cli.parse_args(["--port", "9000"]).command == "start"
    assert cli.parse_args(["--port", "9000"]).port == 9000


def test_explicit_commands_are_unchanged():
    serve = cli.parse_args(["serve", "--background"])
    assert (serve.command, serve.background) == ("serve", True)
    assert cli.parse_args(["serve"]).background is False
    assert cli.parse_args(["stop"]).command == "stop"
    assert cli.parse_args(["restart"]).command == "restart"
    assert cli.parse_args(["reset-admin"]).username == "admin"


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    """把"启动后台进程"和"打开浏览器"换成记录调用的替身。"""
    calls = {"spawned": [], "opened": []}
    state = {"running": False, "port_free": True, "starts_ok": True}

    def spawn(args, cwd):
        calls["spawned"].append(args)
        state["running"] = state["starts_ok"]
        return 1234

    monkeypatch.setattr(cli, "_ping", lambda settings: state["running"])
    monkeypatch.setattr(cli, "_wait_until_up", lambda settings, timeout=30.0: state["running"])
    monkeypatch.setattr(winutil, "port_available", lambda host, port: state["port_free"])
    monkeypatch.setattr(winutil, "spawn_background", spawn)
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: calls["opened"].append(url) or True)
    calls["state"] = state
    calls["home"] = tmp_path / "home"
    return calls


def test_start_launches_background_service_then_opens_admin_page(launcher, capsys):
    assert cli.main(["--home", str(launcher["home"])]) == 0
    (spawned,) = launcher["spawned"]
    # 后台进程走的是 serve --background，并带上同一个主目录
    assert spawned[-4:] == ["serve", "--home", str(launcher["home"].resolve()), "--background"]
    assert launcher["opened"] == ["http://127.0.0.1:8765/admin/"]
    assert "已在后台运行" in capsys.readouterr().out


def test_start_passes_port_override_to_the_background_service(launcher):
    assert cli.main(["start", "--home", str(launcher["home"]), "--port", "9123"]) == 0
    assert launcher["spawned"][0][-2:] == ["--port", "9123"]
    assert launcher["opened"] == ["http://127.0.0.1:9123/admin/"]


def test_start_when_already_running_only_opens_the_browser(launcher, capsys):
    """重复双击不会启动第二个实例。"""
    launcher["state"]["running"] = True
    launcher["state"]["port_free"] = False
    assert cli.main(["--home", str(launcher["home"])]) == 0
    assert launcher["spawned"] == []
    assert launcher["opened"] == ["http://127.0.0.1:8765/admin/"]
    assert "已经在后台运行" in capsys.readouterr().out


def test_start_reports_port_taken_by_another_program(launcher, capsys):
    launcher["state"]["port_free"] = False
    assert cli.main(["--home", str(launcher["home"])]) == 2
    assert launcher["spawned"] == [] and launcher["opened"] == []
    assert "已被其他程序占用" in capsys.readouterr().err


def test_start_reports_failure_without_opening_browser(launcher, capsys):
    launcher["state"]["starts_ok"] = False
    assert cli.main(["--home", str(launcher["home"])]) == 2
    assert len(launcher["spawned"]) == 1 and launcher["opened"] == []
    assert "phonecloud.log" in capsys.readouterr().err


def test_restart_starts_when_nothing_is_running(launcher, capsys):
    assert cli.main(["restart", "--home", str(launcher["home"])]) == 0
    assert len(launcher["spawned"]) == 1
    assert launcher["opened"] == ["http://127.0.0.1:8765/admin/"]
    assert "没有找到" not in capsys.readouterr().out


def test_restart_stops_the_running_process_first(launcher, monkeypatch):
    home = launcher["home"]
    data = home / "data"
    data.mkdir(parents=True)
    (data / "phonecloud.pid").write_text("4321", encoding="ascii")
    killed = []

    def kill(pid):
        killed.append(pid)
        launcher["state"]["running"] = False
        return True

    monkeypatch.setattr(winutil, "process_image", lambda pid: "phonecloud.exe")
    monkeypatch.setattr(winutil, "kill_process", kill)
    launcher["state"]["running"] = True

    assert cli.main(["restart", "--home", str(home), "--no-browser"]) == 0
    assert killed == [4321]
    assert launcher["spawned"] and launcher["opened"] == []


def test_shortcuts_point_at_the_exe(tmp_path):
    exe = tmp_path / "phonecloud.exe"
    exe.write_bytes(b"MZ")
    winutil.ensure_shortcuts(exe)
    shell = (
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK); "
            "Write-Output ('TARGET=' + $s.TargetPath); "
            "Write-Output ('ARGS=' + $s.Arguments); "
            "Write-Output ('DIR=' + $s.WorkingDirectory)"
        )
    found = {}
    for name, arguments in (
        ("打开 PhoneCloud.lnk", ""),
        ("重新启动.lnk", "restart"),
        ("停止.lnk", "stop"),
    ):
        path = tmp_path / name
        assert path.is_file()
        env = dict(**os.environ)
        env["LNK"] = str(path)
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", shell],
            capture_output=True,
            env=env,
            check=False,
        )
        data = result.stdout
        text = data.decode("utf-16") if b"\x00" in data[:8] else data.decode("utf-8", errors="replace")
        fields = {}
        for line in text.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                fields[key.strip()] = value.strip()
        found[name] = fields
        assert fields["TARGET"].lower() == str(exe).lower()
        assert fields["ARGS"] == arguments
        assert fields["DIR"].lower() == str(tmp_path).lower()
    assert set(found) == {"打开 PhoneCloud.lnk", "重新启动.lnk", "停止.lnk"}


def test_icon_contains_the_blue_cloud(tmp_path):
    from PIL import Image

    from phonecloud.make_icon import write_icon

    path = tmp_path / "phonecloud.ico"
    write_icon(path)
    image = Image.open(path).convert("RGBA")
    colors = image.getcolors(maxcolors=256 * 256) or []
    pixels = {color for _count, color in colors}
    assert (37, 99, 235, 255) in pixels
    assert (255, 255, 255, 255) in pixels


def test_start_no_browser(launcher, capsys):
    assert cli.main(["start", "--home", str(launcher["home"]), "--no-browser"]) == 0
    assert len(launcher["spawned"]) == 1 and launcher["opened"] == []
    assert "http://127.0.0.1:8765/admin/" in capsys.readouterr().out
