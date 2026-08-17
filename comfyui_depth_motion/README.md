# Depth Motion ComfyUI Nodes

## 安装

1. 将 `comfyui_depth_motion` 目录放入 `ComfyUI/custom_nodes/`。
2. 在 ComfyUI 的 Python 环境中执行：`python -m pip install -r ComfyUI/custom_nodes/comfyui_depth_motion/requirements.txt`。
3. 重启 ComfyUI。

## 节点

- `Depth Motion 生成器`：上传本地视频或提交公开视频链接，等待服务生成后下载深度 MP4、Manifest、PNG ZIP 和可选完整素材包。同一远程链接需要重新抓取时递增 `refresh`。
- `Depth Motion 加载深度帧`：从 PNG ZIP 分段加载深度帧并输出标准 `IMAGE` 批次。

默认服务地址为 `https://depth.whaios.com`。本地视频路径必须能由运行 ComfyUI 的机器访问。生成节点的 `output_dir` 也位于运行 ComfyUI 的机器上。

只运行来源可信的 ComfyUI 工作流。工作流中的 `source` 和 `service_url` 会决定节点读取并上传的视频文件及目标服务。
