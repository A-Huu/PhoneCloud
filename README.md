# PhoneCloud

只支持 Windows。把家里的电脑当成一部局域网里的私有云盘：手机用自带浏览器看照片、放视频、上传和下载。不用安装 App，文件也不经过外面的网盘。

直接拿来用，看 [快速使用](快速使用.md)。本页留给要改程序或自己打包的人。

页面走局域网 HTTP，只给家里同一个 Wi-Fi 用。不要把端口映射到公网。

交付物是一个 `phonecloud.exe`。目标电脑不用安装 Python，用户页和管理页都包在程序里。

## 适合用来做什么

- 把手机里的照片、视频传到家里电脑，腾出手机空间。
- 家人一起看同一本相册。照片就在电脑的某个文件夹里，资源管理器也能直接打开。
- 人在家里，用手机翻这台电脑上的照片和视频。
- 每人一个账号。共享文件夹大家都能传，个人文件夹只有主人能传。

电脑保持开机，手机和电脑连同一个 Wi-Fi。

## 三分钟开始

1. 新建一个文件夹当主目录，例如 `D:\PhoneCloud\`。把 `phonecloud.exe` 放进去，双击。
   服务在后台启动，并自动打开管理页。再双击一次不会多开，只是重新打开管理页。
   Windows 第一次弹出防火墙时，允许「专用网络」。
2. 在管理页 `http://127.0.0.1:8765/admin/` 创建管理员。这一步只接受本机访问，避免局域网里别人抢先创建。
3. 在「概览」里给家人建账号。要一起用的文件夹，到「空间」里点「创建」。默认建在主目录下，根目录可以改。
4. 手机连上同一个 Wi-Fi，用手机扫管理页上的二维码。用浏览器打开后，选添加到主屏幕。地址变了就再扫一次管理页上的二维码。

## 家里是怎么用的

服务跑在这台 Windows 电脑上，默认端口 `8765`。账号和权限记在 SQLite 里。照片和视频仍是磁盘上的普通文件。

管理页有两块：概览（账号、手机入口、磁盘）和空间（文件夹、谁可以看）。用户页用来浏览、预览、上传、下载。能上传的地方也可以删除，文件进入电脑回收站。手机用浏览器打开后，选添加到主屏幕。地址变了就再扫一次管理页上的二维码。

## 两种文件夹

| 文件夹 | 位置 | 谁能上传 |
| :--- | :--- | :--- |
| 共享 | 主目录下，和名字相同，如 `D:\PhoneCloud\家庭相册` | 看得到的人都能上传 |
| 个人 | `accounts\<账号>\<名字>` | 只有主人能上传，其他人默认可看 |

新上传时选一个已有文件夹，或新建共享 / 个人文件夹。谁能看，在「空间」里点「谁可以看」：保持所有人，或改成只让勾选的人看。主人始终看得到自己的个人文件夹。

同名文件存成 `IMG_001_1.jpg`，原来的文件留着。网页可以一次选多个文件，或选整个文件夹。`data` 目录即使落在某个空间下面，也不会从网页打开。

## 主目录

```
D:\PhoneCloud\
  phonecloud.exe
  config.json              首次运行生成
  家庭相册\                 共享文件夹里的原文件
  accounts\alice\          个人文件夹
  data\phonecloud.db       账号和权限
  data\thumbs\             缩略图，删了会重新生成
  data\logs\phonecloud.log
```

备份：先双击「停止」，或执行 `phonecloud.exe stop`，再复制整个主目录。

## 怎么开关

双击 `phonecloud.exe`，或双击旁边的「打开 PhoneCloud」。没有黑窗口：没在运行就在后台启动，已经在运行就只打开管理页。启动失败才会弹出提示。

同目录还有两个快捷方式：「重新启动」和「停止」。第一次双击程序时会生成它们。运行状态看管理页里的「本机状态」。

## 命令

| 命令 | 作用 |
| :--- | :--- |
| `start [--port 8765] [--home 目录]` | 后台启动并打开管理页。已经在跑就只打开页面。`--no-browser` 不打开浏览器 |
| `restart` | 先停止，再后台启动，并打开管理页 |
| `stop` | 停止。没有在运行时会说明 |
| `serve` | 在当前命令行窗口前台运行，Ctrl+C 停止 |
| `serve --background` | 后台运行，不打开浏览器。开机自启用这个 |
| `install-autostart` / `uninstall-autostart` | 登录 Windows 后自动启动 / 取消 |
| `reset-admin [--username admin]` | 忘记管理员密码时重置；账号不存在则创建 |

主目录默认是 exe 所在文件夹，也可以用 `--home` 或环境变量 `PHONECLOUD_HOME`。服务在后台跑，没有窗口；从命令行执行时，结果打印在这个窗口里。日志在 `data\logs\phonecloud.log`。移动了 exe 之后，重新执行一次 `install-autostart`。电脑睡眠时手机暂时打不开，页面恢复后会自己重连。

## 配置

改主目录里的 `config.json`，重启后生效。

| 项 | 默认 | 含义 |
| :--- | :--- | :--- |
| `host` | `0.0.0.0` | 监听地址 |
| `port` | `8765` | 端口 |
| `max_upload_mb` | `5120` | 单个文件上限（MB），5120 即 5 GB |
| `min_free_mb` | `1024` | 磁盘至少保留的空闲空间（MB），不够就拒绝上传 |
| `session_days` | `30` | 登录有效天数，使用中会续期 |
| `thumb_size` | `360` | 缩略图最长边（像素） |
| `access_log` | `false` | 是否把每个请求写进日志 |

## 手机打不开

先确认手机和电脑在同一个 Wi-Fi，电脑没有睡眠。管理页上的二维码默认是无线网地址。

用管理员身份打开命令提示符，放行端口（端口按实际修改）：

```
netsh advfirewall firewall add rule name="PhoneCloud" dir=in action=allow protocol=TCP localport=8765 profile=private
```

这条规则只对「专用网络」生效。把当前网络改成专用：

```
powershell -NoProfile -Command "Get-NetConnectionProfile | Set-NetConnectionProfile -NetworkCategory Private"
```

## 使用边界

- 页面走家里的局域网 HTTP。留在可信的家庭网络里，不要把 `8765` 映射到公网。
- 上传中断后重新传，没有断点续传。
- 视频不转码。浏览器播不了的编码，下载后再看。
- 缩略图支持 JPEG、PNG、GIF、WebP、BMP。HEIC 不生成缩略图。
- 账号可以禁用。账号文件夹里没有文件时可以删除；里面还有文件时必须先清空。

## 从源码运行

需要 Python 3.10 或更高版本。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe run.py serve --home .devhome
.\.venv\Scripts\python.exe -m pytest -q
powershell -ExecutionPolicy Bypass -File build.ps1
```

`build.ps1` 先跑测试，再打出 `dist\phonecloud.exe`。加 `-SkipTests` 可以跳过测试。

| 路径 | 内容 |
| :--- | :--- |
| `phonecloud/` | 服务、命令行、内嵌网页 |
| `phonecloud/web/` | 用户页、管理页 |
| `快速使用.md` | 给直接使用的人 |
| `tests/` | 测试 |

运行产生的 `config.json`、`accounts/`、`data/`、`.devhome/`，以及 `build/`、`dist/`、虚拟环境，都在 `.gitignore` 里。

## 许可证

[MIT](LICENSE)。

FastAPI、Pydantic、Pillow、segno 为 MIT；Starlette、uvicorn 为 BSD-3-Clause；python-multipart 为 Apache-2.0。
