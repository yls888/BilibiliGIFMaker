# BilibiliGIFMaker

从 Bilibili 下载视频片段并一键导出为高质量 GIF 的桌面工具。基于 PyQt6 构建，采用纯软件 FFmpeg 解码渲染，无需显卡加速即可流畅播放和预览视频。

#### 注意：本程序的代码基本全由AI生成（包括此README文件也是），如有BUG请自行修复解决，作者不提供任何帮助。

---

## 核心功能

### 🎯 输入 BV 号批量下载

- 支持纯 BV 号（如 `BV1cFMPzEE1t`）和完整 Bilibili 视频链接
- 支持多行输入，批量下载多个视频
- 视频自动缓存到本地 `BilibiliTempVideos` 目录，重复下载自动复用缓存
- 提供"强制重下"按钮，用于刷新已缓存的视频

### 🎬 视频本地预览播放

- 基于 FFmpeg 纯软件解码（`FFmpegPlayer`），无需安装额外解码器
- 支持播放 / 暂停 / 停止操作
- **A-B 循环播放**：在时间轴上设置起始/结束标记，视频在标记区间内循环播放，方便精确定位 GIF 片段
- 播放进度实时反馈在自定义时间轴上

### 🖱️ 鼠标拖拽绘制截取区域（ROI）

- 在主视频画面上直接**鼠标拖拽**框选需要截取的区域
- 选区坐标**精确映射**到原始视频分辨率，确保裁剪精度
- 右侧实时预览窗口同步显示裁剪效果
- 支持通过微调 SpinBox 精确设置 ROI 的四个边界坐标（x1, y1, x2, y2）

### ⚙️ 自定义 GIF 参数

- **起止时间**：通过时间轴标记拖拽或手动输入，精确控制 GIF 片段范围（精确到 0.1 秒）
- **FPS**：可选 1-60 帧/秒，控制 GIF 播放流畅度
- **缩放比例**：0.1x – 3.0x 缩放输出尺寸
- 支持"SET"快捷键，将当前的播放时间点快速设置为开始/结束时间

### 🚀 一键导出 GIF

- 点击"生成 GIF"按钮即可自动调用 ffmpeg 完成调色板生成和 GIF 编码
- 生成过程在后台线程执行，不阻塞 UI 界面
- 生成完成后弹出提示框，包含文件路径和大小信息
- 支持自定义输出目录（默认 `output/` 文件夹）

### 🌙 暗色主题界面

- 全局深色主题，护眼且美观
- 日志面板实时显示下载、播放、生成过程中的详细信息

---

## 使用说明

### 方式一：直接运行源码

**1. 安装 Python 依赖**

```bash
pip install PyQt6 you-get
```

> 建议在虚拟环境中安装。项目根目录已包含 `.venv` 虚拟环境。

**2. 准备 ffmpeg**

从 [ffmpeg.org](https://ffmpeg.org/download.html) 下载 Windows 版本，将 `ffmpeg.exe` 和 `ffprobe.exe` 放在项目根目录（与 `BilibiliGIFMaker_GUI.py` 同级）。程序会优先使用同目录下的这两个文件，否则自动搜索系统 PATH。

**3. 运行程序**

```bash
python BilibiliGIFMaker_GUI.py
```

**4. 使用流程**

1. 在左侧输入 BV 号或视频链接（每行一个），点击"下载"
2. 下载完成后，在"已缓存视频"列表中双击视频加载
3. 视频自动开始播放，在画面上**鼠标拖拽**选择截取区域（或不框选则使用全画面）
4. 拖拽时间轴上的绿色（开始）和红色（结束）标记，设定 GIF 起止时间
5. 调整 FPS、缩放比例等参数
6. 点击"生成 GIF"，等待完成

### 方式二：使用打包后的 EXE

参见下方"打包说明"，生成可直接运行的 `BilibiliGIFMaker.exe`，双击即可启动，无需安装 Python 环境。

---

## 打包说明

项目内置了 `build_exe.py` 一键打包脚本，基于 PyInstaller 生成**无控制台窗口**的 Windows 可执行文件。

### 打包命令

在项目根目录运行：

```bash
python build_exe.py
```

脚本会自动完成以下步骤：

1. 检查 PyInstaller 是否安装，未安装则自动安装
2. 验证 `ffmpeg.exe` / `ffprobe.exe` 是否存在
3. 执行 PyInstaller 打包：
   - `--noconsole`：生成 GUI 子系统程序，运行时无控制台窗口
   - `--onedir`：非单文件模式，输出目录结构清晰
   - `--add-data`：将 ffmpeg/ffprobe 自动复制到输出目录
   - `--hidden-import`：包含 `you_get` 等必要依赖
4. 自动清理临时构建文件（`build/` 目录和 `.spec` 文件）

### 打包产物

输出目录为 `dist/BilibiliGIFMaker/`，包含：

```
dist/BilibiliGIFMaker/
├── BilibiliGIFMaker.exe    ← 主程序（双击运行）
├── ffmpeg.exe              ← 自动包含
├── ffprobe.exe             ← 自动包含
├── _internal/              ← Python 运行时和依赖库
└── ...
```

将整个 `BilibiliGIFMaker` 目录分发到其他 Windows 电脑即可直接运行，无需安装 Python 或任何依赖。

> **提示**：移动 EXE 位置时，请确保 `ffmpeg.exe` 和 `ffprobe.exe` 与其保持在同一目录下。

---

## 文件结构

```
BilibiliGIFMaker/
├── BilibiliGIFMaker_GUI.py   ← 主程序入口（PyQt6 GUI）
├── build_exe.py              ← PyInstaller 一键打包脚本
├── ffmpeg.exe                ← FFmpeg 编解码工具（需自行下载）
├── ffprobe.exe               ← FFprobe 媒体分析工具（需自行下载）
├── BilibiliTempVideos/       ← 视频下载缓存目录（自动创建）
│   └── BV*.mp4               ← 下载的原始视频文件
├── output/                   ← GIF 输出目录（自动创建）
│   └── *.gif                 ← 生成的 GIF 文件
├── dist/                     ← 打包输出目录（运行 build_exe.py 后生成）
│   └── BilibiliGIFMaker/
│       └── BilibiliGIFMaker.exe
├── .venv/                    ← Python 虚拟环境（可选）
└── README.md                 ← 本文件
```

---

## 依赖与环境

### Python 包

| 包名 | 用途 |
|------|------|
| [PyQt6](https://pypi.org/project/PyQt6/) | GUI 界面框架 |
| [you-get](https://pypi.org/project/you-get/) | Bilibili 视频下载引擎 |

安装命令：

```bash
pip install PyQt6 you-get
```

### 外部工具

| 工具 | 用途 | 获取方式 |
|------|------|----------|
| [ffmpeg](https://ffmpeg.org/) | 视频解码、GIF 编码 | 下载 Windows 版本的 `ffmpeg.exe` + `ffprobe.exe`，放入项目根目录 |
| [PyInstaller](https://pyinstaller.org/) | 打包为 EXE（可选） | `pip install pyinstaller`（`build_exe.py` 会自动安装） |

### 运行环境

- **操作系统**：Windows 10 / 11（推荐）
- **Python 版本**：3.9+
- **显示**：建议分辨率 1280×720 以上，以获得最佳界面体验
