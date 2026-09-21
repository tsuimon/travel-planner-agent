# 部署说明

## Anaconda

推荐Python3.12；本机已创建命名环境travel-planner，路径 `D:\anaconda3\envs\travel-planner`。初次重建执行：

```powershell
conda env create -f environment.yml
conda activate travel-planner
python -m uvicorn src.api.app:app --host 127.0.0.1 --port 8000
```

已有同名环境时不要重复创建，使用 `conda env update -n travel-planner -f environment.yml` 更新。没有在用户现有agent、ml或base环境中安装本项目依赖。早期临时`.venv`不再使用。

PowerShell找不到conda但不希望修改shell配置时，直接执行：

```powershell
& 'D:\anaconda3\Scripts\conda.exe' run -n travel-planner --no-capture-output python -m uvicorn src.api.app:app --host 127.0.0.1 --port 8000
```

默认不需要`.env`。可复制`.env.example`并配置真实供应商或LLM；真实密钥只放环境变量或被gitignore排除的`.env`。修改后重启进程。

## Docker

```bash
docker compose up --build
```

API与UI共用一个容器、一份SQLite和Chroma，named volume持久化data目录；容器以非root运行，HTTP健康检查访问 `/health`。默认只绑定127.0.0.1:8000。

本机没有检测到Docker命令，因此镜像构建/运行没有在本机实测。文件可供具备Docker的环境验证，不应把静态文件存在称为部署成功。

## 维护与排错

- 默认Python若为3.7，说明尚未激活Conda环境。使用 `python --version` 和 `python -c "import sys; print(sys.executable)"` 确认。
- `Address already in use`：已有服务占用8000，可访问现有服务或改用`--port 8001`；不要终止未知进程。
- data目录保存真实会话和偏好，备份时先停止服务，再复制SQLite和Chroma目录。
- knowledge条目更新后运行 `python -m scripts.build_knowledge_base`，同步清理已删除条目并重建向量。
- 单用户桌面原型不支持多worker共享Gradio状态；默认运行一个uvicorn worker。
- 如需通过API token保护数据，设置API_ACCESS_TOKEN后匿名Gradio UI会关闭。多租户、限流、TLS和访问策略需在生产部署前单独实现与验证。

## 可复现依赖

`requirements.txt`与开发依赖文件给出兼容主版本范围；`environment.yml`固定Python3.12并安装它们。实测的本机完整依赖版本见`requirements-win-py312.lock.txt`。该锁文件是Windows验证快照，不应未经解析直接作为Linux依赖锁使用。
