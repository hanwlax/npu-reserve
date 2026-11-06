# NPU 机时预约

适用于内部机器资源预约。后端使用 Python 和 SQLite，浏览器直接加载前端页面。支持 Windows 和 Linux，需要 Python 3.10 或以上版本和 `cryptography`。

## 本地启动

在项目目录执行。使用虚拟环境时，请先激活虚拟环境。

```bash
python3 -m pip install -r requirements.txt
python3 server.py serve
```

打开 [http://127.0.0.1:8080](http://127.0.0.1:8080)，用内置管理员账户登录，先在「机器资源池」添加机器，再创建用户并分配机器或机器组。同网段的设备用 `http://<服务器局域网 IP>:8080` 访问。用户向管理员私聊申请账户，管理员单独告知登录信息。服务默认使用 `data/reservations.sqlite3`，前台按 Ctrl+C 停止。

数据库初始化会添加两个管理员 `zhaoming` 和 `leixuewei`，初始密码分别与登录账户相同。升级时，已有的同名普通账户会提升为管理员，保留现有密码，并在重新登录后使用管理员权限。首次使用请更换初始密码，再开放站点访问。

登录后可在右上角修改自己的密码。管理员可在「账户与授权」重置普通用户或其他管理员的密码，建号和重置都可自动生成密码。手动设置密码至少 8 个字符，自动生成 8 位字母数字密码。管理员可随时在普通用户旁点击「查看密码」。旧账户的密码在用户重新登录、主动改密或管理员重置后可查看。改密无需旧密码，当前改密者的会话会保留，目标账户的其他会话会失效。如需额外的管理员，可运行 `python3 server.py create-admin --username admin` 交互式设置初始密码。对于 systemd 部署，请以 `npu-reserve` 系统账户执行，并用 `--db /var/lib/npu-reserve/reservations.sqlite3` 指向服务数据库。

默认监听 `0.0.0.0:8080`。可用 `serve --host`、`--port` 或环境变量 `NPU_RESERVE_HOST`、`NPU_RESERVE_PORT` 调整。若在同一台主机上使用 HTTPS 反向代理，将监听地址设为 `127.0.0.1`，并设置 `NPU_RESERVE_SECURE_COOKIE=1`。站点部署在域名根路径。

普通用户的可查看密码用 Fernet 加密存储，密钥默认保存在数据库旁的 `reservations.sqlite3.key`。部署备份时请单独保护密钥，恢复数据库时需恢复对应密钥。

Windows 原生 Python 可直接用 `python3 .\server.py serve` 启动。Windows 用当前系统账户的 DPAPI 保护密钥，重启时请使用同一 Windows 账户。已有加密密码的数据请用创建密钥的环境启动。

Linux 密钥文件需要支持 `0600` 权限。在 WSL 的 Windows 挂载目录运行时，请把密钥放到 Linux 文件系统。以下命令继续使用项目现有的数据库路径。

```bash
NPU_RESERVE_PASSWORD_KEY_FILE="$HOME/.local/state/npu-reserve/password.key" python3 server.py serve
```

## systemd 部署

在目标 Linux 主机上为 systemd 所用的 `/usr/bin/python3` 安装 `cryptography`，例如 Debian 或 Ubuntu 上运行 `sudo apt install python3-cryptography`。然后从项目目录执行以下命令。已有 `npu-reserve` 系统账户时可跳过第一条。更新程序时重新安装程序文件和 unit，然后重启服务即可，数据库位于独立的状态目录。

```bash
sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin npu-reserve
sudo install -d -m 0755 /opt/npu-reserve/static
sudo install -m 0644 server.py reservation.py /opt/npu-reserve/
sudo install -m 0644 static/index.html static/app.css static/app.js /opt/npu-reserve/static/
sudo install -d -m 0750 -o npu-reserve -g npu-reserve /var/lib/npu-reserve

sudo install -m 0644 deploy/npu-reserve.service /etc/systemd/system/npu-reserve.service
sudo systemctl daemon-reload
sudo systemctl enable --now npu-reserve.service
```

`enable --now` 同时启动服务并启用开机自启。服务异常退出后由 systemd 等待 3 秒重启。

服务默认使用以下监听配置。可用 `sudoedit /etc/npu-reserve.env` 新建或编辑配置文件。

```ini
NPU_RESERVE_HOST=0.0.0.0
NPU_RESERVE_PORT=8080
```

如需改用端口 `18080`，将对应配置改为 `NPU_RESERVE_PORT=18080`，保存后重启服务。

```bash
sudo systemctl restart npu-reserve.service
curl -fsS -o /dev/null http://127.0.0.1:18080/
```

查看服务状态和日志。

```bash
systemctl status npu-reserve
journalctl -u npu-reserve -f
```

## 预约规则

- 时间轴使用北京时间，展示当天 08:00 至次日 08:00。预约粒度为 30 分钟，进行中预约可从下一个可预约的半小时边界释放。午夜后仍在进行的预约可在「我的预约」中释放。
- 用户只能查看已授权的机器。单独授权和组授权取并集。失去访问权后，受影响的剩余机时自动释放。
- 悬停预约或锁定色块可查看时段和操作人显示名。旧锁定缺少操作人记录时显示「未记录」。
- 用户可整段或部分释放自己的预约。管理员可以用选区切分预约或解除部分锁定，选区外的时段保留。删除机器会收回相关权限并清理剩余预约和锁定。
- 并发预约以先成功提交数据库事务的一单为准。重叠提交返回冲突，页面局部更新时间轴和预约。管理员确认时如果数据已变化，页面会刷新数据供重新核对。

## 验证

```bash
python3 -m unittest discover -s tests -v
node --check static/app.js
systemd-analyze verify deploy/npu-reserve.service
```
