# 安卓安装版

此版为现有规划系统的安卓联网客户端：有服务器地址设置、连接失败提示、返回及重试，规划页面与电脑端共用同一后端。不是离线运行包，不包含 Python、模型、数据库或任何真实 API 密钥。

## 安装和使用

1. 将 `dist/travel-planner-0.1.0.apk` 传到安卓手机，打开安装。若系统提示，允许所用文件管理器安装此 APK。系统要求：Android 8.0 或更高版本。
2. 手机和电脑连接同一可信 Wi-Fi。在电脑的 Anaconda Prompt 执行：

   ```bat
   cd /d E:\travel-planner-agent
   conda activate travel-planner
   python -m scripts.start_mobile
   ```

3. 保持终端运行，在手机应用输入终端显示的服务器根地址，如 `http://192.168.1.8:8001`，点击“连接并开始规划”。多网卡时用电脑实际 Wi-Fi 网卡的 IPv4 地址，忽略 VMware 等虚拟网卡。

手机不能填 `127.0.0.1`，因为它指向手机本身。网络变化后需更新地址。电脑端原有8000端口服务可继续使用；手机启动命令使用8001端口，不需要关闭原有服务。

如果连接失败，先在手机浏览器尝试 `http://电脑IP:8001/ui/`。确认电脑服务运行、两端同网且Wi-Fi未启用客户端隔离。Windows弹出防火墙提示时仅允许可信专用网络，不要关闭整个防火墙。公司/校园Wi-Fi可能隔离设备，可改用两端均能访问的私人热点或部署服务器。

## 使用范围

- 电脑休眠、关机或停止后端时，手机不能继续规划。
- HTTP仅用于输入的私有IPv4地址。远程服务使用有效HTTPS证书；客户端不会绕过证书错误。
- 当前后端是单用户系统，同一个服务上的会话和长期偏好由连接它的设备共用。仅适合本人在可信局域网试用；没有自动开放Windows防火墙，也没有修改现有服务的监听地址。
- 客户端加载 `/ui/`。若设置了 `API_ACCESS_TOKEN`，现有后端会关闭网页界面，因此此版不能直接连接那种配置。手机启动脚本会明确提示，不会替你移除访问保护。
- 出门使用需要另行部署有认证的HTTPS后端。当前并未完成公网部署、应用商店上架或多用户账户隔离。

## 重新构建

Windows + JDK17或更高版本，在项目根目录执行：

```bat
conda activate travel-planner
python mobile/android/build.py --setup
```

首次下载Google官方Android SDK平台35和构建工具35.0.0，共约124MB，校验官方仓库提供的SHA-1后解压到 `.tools/android`。此后可直接运行 `python mobile/android/build.py`。没有使用第三方在线打包服务。

构建脚本依次执行Java编译、D8转换、AAPT2资源打包、zipalign对齐、APK签名和签名验证。生成文件：

- `dist/travel-planner-0.1.0.apk`
- `dist/travel-planner-0.1.0.apk.sha256`

此包用本地测试证书签名，可侧载安装。证书保存在 `.tools/android/local-test.keystore`，不入源码；保留该证书以便以后覆盖更新。正式商店发布须使用独立保管的发布密钥，此测试包不是商店发布包。

SDK下载依据：[Google官方SDK仓库](https://dl.google.com/android/repository/repository2-1.xml)；[Android WebView文档](https://developer.android.com/develop/ui/views/layout/webapps/webview)；[APK签名验证](https://developer.android.com/tools/apksigner)。

## 验证边界

2026-09-20已完成：APK编译、zipalign对齐、APK v2/v3签名验证；AAPT2确认启动Activity、包名与Android8+最低版本；地址校验与来源限制的19项JVM断言通过。检查APK只包含Manifest、界面资源与DEX代码，没有.env、数据库或签名私钥。

没有连接安卓真机，因此尚未实测手机安装、界面、键盘、Gradio流式连接及系统返回行为。签名和构建成功不能代替真机验收。
