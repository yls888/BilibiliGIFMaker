"""
B站视频片段截取并导出为GIF工具 —— PyQt6 GUI 版本
================================================================
架构: 模块化设计（工作线程 + 视图组件 + 控制器信号/槽）
  - 工作线程: DownloadWorker / GifWorker / FfmpegFrameReader（不阻塞 UI）
  - 视图:     ROIOverlay / TimelineWidget / MainVideoWidget / VideoPlayerWidget / MainWindow
  - 控制器:   MainWindow / VideoPlayerWidget 内的信号/槽连接

核心技术点:
  1. FfmpegPlayer + MainVideoWidget（QPainter 软件渲染）视频预览
  2. ROIOverlay 鼠标拖拽矩形选区，UI坐标 → 原视频坐标精确映射
  3. TimelineWidget 自定义时间轴，带可拖拽的开始/结束打点标记
  4. 多线程下载 (you-get) 与 GIF 生成 (ffmpeg)，不阻塞 UI
"""

import sys
import os
import ctypes

# ⚠ 必须在 PyQt6 import 之前设置多媒体后端环境变量
# 方案：Windows Media Foundation + 禁用 D3D11，强制纯软件解码
# 解决 AV1 视频因缺少硬件加速支持而解码失败的问题
os.environ['QT_MEDIA_BACKEND'] = 'windows'
os.environ['MF_DISABLE_D3D11'] = '1'

# ── Windows 高精度定时器 ──
# 默认 Windows 定时器分辨率约 15.6ms，导致 threading.Event.wait() 精度不足。
# timeBeginPeriod(1) 将系统定时器提升到 1ms，确保播放帧率节流准确。
try:
    ctypes.windll.winmm.timeBeginPeriod(1)  # type: ignore[attr-defined]
except Exception:
    pass

import subprocess
import threading
import time
import tempfile
import shutil
import re
from pathlib import Path
from typing import Optional

# ==============================================================================
# PyQt6 导入
# ==============================================================================
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QGroupBox, QTextEdit, QPushButton, QListWidget,
    QListWidgetItem, QLabel, QLineEdit, QSpinBox, QDoubleSpinBox,
    QFileDialog, QMessageBox, QStatusBar, QProgressBar,
    QSizePolicy, QAbstractItemView,
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QUrl, QTimer, QRect, QPoint, QSize,
    QEvent, QObject, QRectF,
)
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QFont, QMouseEvent, QPaintEvent,
    QResizeEvent, QPalette, QImage, QPolygon,
)

# ==============================================================================
# 全局常量
# ==============================================================================
BASE_DIR = Path(__file__).parent.resolve()
CACHE_DIR = BASE_DIR / "BilibiliTempVideos"
CACHE_DIR.mkdir(exist_ok=True)

VIDEO_EXTENSIONS = (".mp4", ".flv", ".mkv", ".webm", ".avi", ".mov")

# 暗色主题样式表
DARK_QSS = """
QMainWindow { background-color: #1e1e2e; color: #cdd6f4; }
QGroupBox {
    border: 1px solid #45475a; border-radius: 6px;
    margin-top: 14px; padding-top: 18px;
    font-weight: bold; color: #cdd6f4;
}
QGroupBox::title {
    subcontrol-origin: margin; left: 12px; padding: 0 6px;
    color: #89b4fa;
}
QPushButton {
    background-color: #45475a; color: #cdd6f4;
    border: 1px solid #585b70; border-radius: 4px;
    padding: 6px 14px; font-weight: bold;
}
QPushButton:hover { background-color: #585b70; }
QPushButton:pressed { background-color: #313244; }
QPushButton#btnDownload { background-color: #1e66f5; border-color: #1e66f5; }
QPushButton#btnDownload:hover { background-color: #2e7af5; }
QPushButton#btnGenerate { background-color: #40a02b; border-color: #40a02b; color: #fff; }
QPushButton#btnGenerate:hover { background-color: #50b03b; }
QPushButton:disabled { background-color: #313244; color: #585b70; }
QLabel { color: #cdd6f4; }
QLineEdit, QTextEdit, QListWidget, QSpinBox, QDoubleSpinBox {
    background-color: #313244; color: #cdd6f4;
    border: 1px solid #45475a; border-radius: 4px; padding: 4px;
}
QListWidget::item:selected { background-color: #1e66f5; }
QListWidget::item:hover { background-color: #45475a; }
QProgressBar {
    background-color: #313244; border: 1px solid #45475a;
    border-radius: 4px; text-align: center; color: #cdd6f4;
}
QProgressBar::chunk { background-color: #1e66f5; border-radius: 3px; }
QStatusBar { background-color: #181825; color: #a6adc8; }
QSplitter::handle { background-color: #45475a; width: 2px; }
/* 标准对话框（QMessageBox/QFileDialog 等）暗色主题 */
QMessageBox {
    background-color: #1e1e2e; color: #cdd6f4;
}
QMessageBox QLabel {
    color: #cdd6f4; background-color: transparent;
}
QMessageBox QPushButton {
    background-color: #45475a; color: #cdd6f4;
    border: 1px solid #585b70; border-radius: 4px;
    padding: 6px 18px; min-width: 80px;
}
QMessageBox QPushButton:hover { background-color: #585b70; }
QMessageBox QPushButton:pressed { background-color: #313244; }
"""


# ==============================================================================
# 工具函数（复用原命令行程序逻辑）
# ==============================================================================

def _find_tool(name: str) -> str:
    """优先使用项目目录下的 ffmpeg/ffprobe，否则使用系统 PATH"""
    local = BASE_DIR / f"{name}.exe"
    return str(local) if local.exists() else name


def get_video_resolution(video_path: str) -> tuple:
    """使用 ffprobe 获取视频宽度和高度"""
    cmd = [_find_tool("ffprobe"), "-v", "error",
           "-select_streams", "v:0",
           "-show_entries", "stream=width,height",
           "-of", "csv=s=x:p=0", str(video_path)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"无法获取分辨率: {r.stderr}")
    w, h = r.stdout.strip().split("x")
    return int(w), int(h)


def get_video_fps(video_path: str) -> float:
    """使用 ffprobe 获取视频实际帧率，解析如 '30000/1001' 或 '30/1' 等格式"""
    cmd = [_find_tool("ffprobe"), "-v", "error",
           "-select_streams", "v:0",
           "-show_entries", "stream=r_frame_rate",
           "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
    if r.returncode != 0 or not r.stdout.strip():
        return 30.0  # 默认回退到 30fps
    fps_str = r.stdout.strip()
    if '/' in fps_str:
        num, den = fps_str.split('/')
        if int(den) != 0:
            return float(num) / float(den)
    try:
        return float(fps_str)
    except ValueError:
        return 30.0


def generate_gif(video_path: str, start_time: float, end_time: float,
                 output_gif: str, fps: int = 10, resize: float = 1.0,
                 crop: Optional[tuple] = None) -> str:
    """使用 ffmpeg 生成 GIF，返回日志"""
    ffmpeg = _find_tool("ffmpeg")
    duration = end_time - start_time
    if duration <= 0:
        raise ValueError("结束时间必须大于开始时间")

    orig_w, orig_h = get_video_resolution(video_path)
    filters = [f"fps={fps}"]

    if crop and len(crop) == 4:
        x1, y1, x2, y2 = crop
        x1 = max(0, min(x1, orig_w))
        y1 = max(0, min(y1, orig_h))
        x2 = max(0, min(x2, orig_w))
        y2 = max(0, min(y2, orig_h))
        cw, ch = x2 - x1, y2 - y1
        if cw <= 0 or ch <= 0:
            raise ValueError("裁剪区域无效")
        filters.append(f"crop={cw}:{ch}:{x1}:{y1}")
    else:
        cw, ch = orig_w, orig_h

    if resize and resize != 1.0:
        nw = round(cw * resize / 2) * 2
        nh = round(ch * resize / 2) * 2
        filters.append(f"scale={nw}:{nh}:flags=lanczos")

    filter_str = ",".join(filters)
    palette_file = tempfile.mktemp(suffix=".png")
    logs = []

    try:
        # 步骤1: 生成调色板
        r1 = subprocess.run([
            ffmpeg, "-ss", str(start_time), "-t", str(duration),
            "-i", str(video_path),
            "-vf", f"{filter_str},palettegen=stats_mode=diff",
            "-y", palette_file
        ], check=True, capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
        logs.append(r1.stderr)

        # 步骤2: 生成 GIF
        r2 = subprocess.run([
            ffmpeg, "-ss", str(start_time), "-t", str(duration),
            "-i", str(video_path), "-i", palette_file,
            "-lavfi", f"{filter_str} [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=5",
            "-y", output_gif
        ], check=True, capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
        logs.append(r2.stderr)
        return "\n".join(logs)
    finally:
        if os.path.exists(palette_file):
            os.remove(palette_file)


# ==============================================================================
# 工具函数：从输入行提取 BV 号
# ==============================================================================

_BV_PATTERN = re.compile(r'BV[0-9A-Za-z]+')
_BV_URL_PATTERN = re.compile(r'bilibili\.com/video/(BV[0-9A-Za-z]+)')


def extract_bv_id(raw: str) -> str:
    """
    从一行输入中提取 BV 号。
    支持格式：
      1. 纯 BV 号:  BV1cFMPzEE1t
      2. 视频链接:  https://www.bilibili.com/video/BV1vX1zBJE1x?...
    提取失败时返回原始字符串。
    """
    raw = raw.strip()
    # 优先尝试从 URL 中提取
    m = _BV_URL_PATTERN.search(raw)
    if m:
        return m.group(1)
    # 否则尝试匹配纯 BV 号
    m = _BV_PATTERN.search(raw)
    if m:
        return m.group(0)
    # 兜底：返回原始字符串
    return raw


# ==============================================================================
# Model: 纯软件 FFmpeg 视频播放器（替代 QMediaPlayer）
# ==============================================================================

class FfmpegFrameReader(QThread):
    """
    工作线程：从 ffmpeg subprocess 管道中持续读取解码后的原始帧，
    通过信号发送到主线程。
    """
    frameReady: pyqtSignal = pyqtSignal(QImage)  # 解码完成的一帧
    readError: pyqtSignal = pyqtSignal(str)  # 错误消息

    def __init__(self, video_path: str, w: int, h: int,
                 start_ms: int = 0, fps: float = 30.0, parent=None):
        super().__init__(parent)
        self._video_path = video_path
        self._w = w
        self._h = h
        self._start_ms = start_ms
        self._fps = fps
        self._process: Optional[subprocess.Popen] = None
        self._abort = threading.Event()

    def run(self):
        """线程主循环：启动 ffmpeg 并节流读取帧

        采用锁相环（PLL）计时策略：
          - 追踪每帧的"应显示时刻"（next_frame_time），而非测量每帧耗时
          - Event.wait() 承担大块 sleep，busy-wait 补齐最后 ~2ms
          - 误差不会跨帧累积：上帧多 sleep 的，下帧自动少 sleep
        """
        self._abort.clear()
        frame_duration = 1.0 / self._fps

        # 不指定 -r/-vsync：ffmpeg 按源视频帧率输出原始帧，
        # Python 端通过 sleep 节流来匹配源帧率，避免帧操作导致的时序偏差。
        cmd = [
            _find_tool("ffmpeg"),
            "-hwaccel", "none",
            "-ss", f"{self._start_ms / 1000.0:.3f}",
            "-i", self._video_path,
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "-hide_banner", "-loglevel", "error",
            "-",
        ]
        try:
            self._process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception as e:
            self.readError.emit(f"ffmpeg 启动失败: {e}")
            return

        fsize = self._w * self._h * 3
        # ── 锁相环：第一帧的目标时刻 = 当前时间 ──
        next_frame_time = time.monotonic()

        while not self._abort.is_set():
            try:
                raw = self._process.stdout.read(fsize)
            except Exception:
                break
            if not raw or len(raw) < fsize:
                break

            img = QImage(raw, self._w, self._h,
                         self._w * 3, QImage.Format.Format_RGB888)
            self.frameReady.emit(img.copy())

            if self._abort.is_set():
                break

            # ── 锁相环节流 ──
            next_frame_time += frame_duration
            sleep_time = next_frame_time - time.monotonic()
            if sleep_time > 0.003:
                # Event.wait() 做大块 sleep，留 2ms 余量给 busy-wait 精调
                self._abort.wait(sleep_time - 0.002)
            # busy-wait 补齐剩余时间，实现亚毫秒精度
            while time.monotonic() < next_frame_time:
                if self._abort.is_set():
                    break

        try:
            self._process.wait(timeout=1)
            err = self._process.stderr.read().decode('utf-8', errors='replace').strip()
            if err and not self._abort.is_set():
                self.readError.emit(err)
        except Exception:
            pass
        self._cleanup()

    def stop(self):
        """请求线程停止（线程安全）"""
        self._abort.set()
        proc = self._process
        if proc:
            try:
                proc.kill()
            except Exception:
                pass

    def _cleanup(self):
        """安全清理子进程资源"""
        proc = self._process
        if proc:
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=1)
            except Exception:
                pass
            self._process = None


class FfmpegPlayer(QObject):
    """
    纯软件 FFmpeg 视频播放器（通过 subprocess 管道解码）。
    使用 FfmpegFrameReader 工作线程读取帧，不阻塞 UI 线程。
    """

    # 与 QMediaPlayer.PlaybackState 一致的枚举值
    StoppedState = 0
    PlayingState = 1
    PausedState = 2

    positionChanged: pyqtSignal = pyqtSignal(int)  # 毫秒
    durationChanged: pyqtSignal = pyqtSignal(int)  # 毫秒
    playbackStateChanged: pyqtSignal = pyqtSignal(int)  # PlaybackState 枚举值
    errorOccurred: pyqtSignal = pyqtSignal(object, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = FfmpegPlayer.StoppedState
        self._position_ms = 0.0  # 浮点累积，避免 int() 截断导致 ~1% 速度偏差
        self._duration_ms = 0
        self._video_path = ""
        self._w = 0
        self._h = 0
        self._reader: Optional[FfmpegFrameReader] = None
        self._target_fps = 30.0  # 默认值，setSource 中会根据实际视频帧率更新
        self._frame_interval_ms = 1000.0 / self._target_fps
        self._frame_callbacks = []

        # 防抖定时器：拖动光标时避免同步 subprocess.run 阻塞 UI
        self._seek_timer: QTimer = QTimer(self)
        self._seek_timer.setSingleShot(True)
        self._seek_timer.timeout.connect(self._do_seek_decode)
        self._pending_seek_ms: Optional[int] = None

    # ====== 与 QMediaPlayer 兼容的接口 ======

    def set_audio_output(self, output):
        pass

    def set_playback_rate(self, rate):
        pass

    def set_video_output(self, sink):
        pass

    def set_source(self, url: QUrl):
        """加载视频文件，同时检测实际帧率以匹配播放速度"""
        path = url.toLocalFile()
        self._video_path = path
        try:
            self._w, self._h = get_video_resolution(path)
            self._duration_ms = int(self._get_duration() * 1000)
            # 检测源视频真实帧率，确保播放速度与原始视频一致
            self._target_fps = get_video_fps(path)
            self._frame_interval_ms = 1000.0 / self._target_fps
            self.durationChanged.emit(self._duration_ms)
        except Exception as e:
            self._on_error(str(e))

    def play(self):
        if not self._video_path:
            return
        self._start_reader()
        self._state = FfmpegPlayer.PlayingState
        self.playbackStateChanged.emit(self._state)

    def pause(self):
        self._state = FfmpegPlayer.PausedState
        self._stop_reader()
        self.playbackStateChanged.emit(self._state)

    def stop(self):
        self._state = FfmpegPlayer.StoppedState
        self._stop_reader()
        self._position_ms = 0.0
        self.positionChanged.emit(0)
        self.playbackStateChanged.emit(self._state)

    def playback_state(self) -> int:
        return self._state

    def position(self) -> int:
        return int(self._position_ms)

    def duration(self) -> int:
        return self._duration_ms

    def set_position(self, ms: int):
        """跳转到指定位置（毫秒）并解码一帧显示"""
        self._position_ms = float(max(0.0, min(float(ms), float(self._duration_ms))))
        self.positionChanged.emit(int(self._position_ms))

        if self._state == FfmpegPlayer.PlayingState:
            # 播放中跳转：停止并重启读取器
            self._stop_reader()
            self._start_reader()
        else:
            # 暂停/停止状态：防抖解码，避免拖动光标时 UI 阻塞
            self._pending_seek_ms = int(self._position_ms)
            self._seek_timer.start(50)

    def _do_seek_decode(self):
        """防抖定时器触发：执行实际的单帧解码"""
        if self._pending_seek_ms is not None:
            self._decode_single_frame(self._pending_seek_ms)
            self._pending_seek_ms = None

    def add_frame_callback(self, callback):
        """注册帧回调，接收 QImage"""
        self._frame_callbacks.append(callback)

    # ====== 内部方法 ======

    def _get_duration(self) -> float:
        """用 ffprobe 获取视频时长（秒）"""
        cmd = [
            _find_tool("ffprobe"), "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            self._video_path,
        ]
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding='utf-8', errors='replace',
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return 0.0
        return float(r.stdout.strip())

    def _decode_single_frame(self, pos_ms: int):
        """同步解码一帧（用于 seek 后刷新画面）"""
        if not self._video_path or self._w <= 0 or self._h <= 0:
            return
        try:
            cmd = [
                _find_tool("ffmpeg"),
                "-hwaccel", "none",
                "-ss", f"{pos_ms / 1000.0:.3f}",
                "-i", self._video_path,
                "-f", "rawvideo",
                "-pix_fmt", "rgb24",
                "-vframes", "1",
                "-hide_banner", "-loglevel", "error",
                "-",
            ]
            r = subprocess.run(cmd, capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
            if r.returncode == 0 and len(r.stdout) >= self._w * self._h * 3:
                raw = r.stdout[:self._w * self._h * 3]
                img = QImage(raw, self._w, self._h,
                             self._w * 3, QImage.Format.Format_RGB888)
                decoded = img.copy()
                for cb in self._frame_callbacks:
                    cb(decoded)
        except Exception:
            pass

    def _start_reader(self):
        """启动帧读取工作线程"""
        self._stop_reader()
        self._reader = FfmpegFrameReader(
            self._video_path, self._w, self._h,
            start_ms=int(self._position_ms), fps=self._target_fps,
            parent=self,
        )
        self._reader.frameReady.connect(self._on_frame_ready)
        self._reader.readError.connect(self._on_reader_error)
        self._reader.finished.connect(self._on_reader_finished)
        self._reader.start()

    def _stop_reader(self):
        """停止帧读取工作线程（安全断开信号防止竞态）"""
        reader = self._reader
        self._reader = None
        if reader:
            try:
                reader.frameReady.disconnect()
            except Exception:
                pass
            try:
                reader.readError.disconnect()
            except Exception:
                pass
            try:
                reader.finished.disconnect()
            except Exception:
                pass
            reader.stop()
            reader.wait(500)

    def _on_frame_ready(self, img: QImage):
        """工作线程发来新帧 —— 浮点累积位置，避免 int() 截断导致的 ~1% 速度偏差"""
        self._position_ms += self._frame_interval_ms
        pos_int = int(self._position_ms)
        if pos_int > self._duration_ms:
            self._position_ms = float(self._duration_ms)
            pos_int = self._duration_ms
        self.positionChanged.emit(pos_int)

        for cb in self._frame_callbacks:
            cb(img)

    def _on_reader_error(self, msg: str):
        if self._state == FfmpegPlayer.PlayingState:
            self._state = FfmpegPlayer.StoppedState
            self.playbackStateChanged.emit(self._state)
        self.errorOccurred.emit(None, msg)

    def _on_reader_finished(self):
        if self._state == FfmpegPlayer.PlayingState:
            self._state = FfmpegPlayer.StoppedState
            self.playbackStateChanged.emit(self._state)

    def _on_error(self, msg: str):
        self._state = FfmpegPlayer.StoppedState
        self._stop_reader()
        self.playbackStateChanged.emit(self._state)
        self.errorOccurred.emit(None, msg)


class DownloadWorker(QThread):
    """后台下载视频 (you-get)，通过信号报告进度"""
    log: pyqtSignal = pyqtSignal(str)
    download_finished: pyqtSignal = pyqtSignal(str, str)  # (bv_id, file_path)
    error: pyqtSignal = pyqtSignal(str)

    def __init__(self, bv_id: str, force: bool = False):
        super().__init__()
        self.bv_id = bv_id.strip()
        self.force = force

    def run(self):
        try:
            # 检查缓存
            for fn in os.listdir(CACHE_DIR):
                if fn.startswith(self.bv_id) and fn.lower().endswith(VIDEO_EXTENSIONS):
                    fp = os.path.join(CACHE_DIR, fn)
                    if self.force:
                        os.remove(fp)
                        self.log.emit(f"[缓存] 已清除: {fn}")
                    else:
                        self.log.emit(f"[缓存] 使用已下载视频: {fn}")
                        self.download_finished.emit(self.bv_id, fp)
                        return
                    break

            url = f"https://www.bilibili.com/video/{self.bv_id}"
            self.log.emit(f"[下载] 正在下载 {self.bv_id} ...")

            process = subprocess.Popen(
                [sys.executable, "-m", "you_get", "-o", str(CACHE_DIR),
                 "--no-caption", url],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace',
                cwd=str(BASE_DIR),
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            for line in process.stdout:
                line = line.strip()
                if line:
                    self.log.emit(f"[you-get] {line}")
            process.wait()

            if process.returncode != 0:
                raise RuntimeError(f"you-get 返回码: {process.returncode}")

            # 查找下载文件并重命名
            downloaded = None
            best_mtime = 0
            for fn in os.listdir(CACHE_DIR):
                fp = os.path.join(CACHE_DIR, fn)
                if fn.lower().endswith(VIDEO_EXTENSIONS) and not fn.startswith(self.bv_id):
                    mt = os.path.getmtime(fp)
                    if mt > best_mtime:
                        downloaded, best_mtime = fp, mt

            if not downloaded:
                raise FileNotFoundError("未找到下载完成的视频文件")

            ext = os.path.splitext(downloaded)[1]
            new_path = os.path.join(CACHE_DIR, f"{self.bv_id}{ext}")
            if downloaded != new_path:
                if os.path.exists(new_path):
                    os.remove(new_path)
                shutil.move(downloaded, new_path)

            self.log.emit(f"[完成] {self.bv_id}{ext}")
            self.download_finished.emit(self.bv_id, new_path)

        except Exception as e:
            self.error.emit(f"[错误] 下载 {self.bv_id} 失败: {e}")


# ==============================================================================
# Model: GIF 生成工作线程
# ==============================================================================

class GifWorker(QThread):
    """后台生成 GIF，通过信号报告进度"""
    log: pyqtSignal = pyqtSignal(str)
    gif_finished: pyqtSignal = pyqtSignal(str)  # 输出文件路径
    error: pyqtSignal = pyqtSignal(str)

    def __init__(self, video_path: str, start_t: float, end_t: float,
                 output_path: str, fps: int, resize: float,
                 crop: Optional[tuple]):
        super().__init__()
        self.video_path = video_path
        self.start_t = start_t
        self.end_t = end_t
        self.output_path = output_path
        self.fps = fps
        self.resize = resize
        self.crop = crop

    def run(self):
        try:
            self.log.emit(f"[GIF] 开始生成...")
            if self.crop:
                self.log.emit(f"  ROI: ({self.crop[0]},{self.crop[1]})-({self.crop[2]},{self.crop[3]})")
            self.log.emit(f"  时间段: {self.start_t:.1f}s - {self.end_t:.1f}s")
            self.log.emit(f"  FPS: {self.fps}, 缩放: {self.resize}")

            result = generate_gif(
                self.video_path, self.start_t, self.end_t,
                self.output_path, self.fps, self.resize, self.crop
            )
            # 只显示关键行
            for line in result.splitlines():
                if any(k in line for k in ['Duration', 'frame=', 'size=', 'video:']):
                    self.log.emit(f"[ffmpeg] {line.strip()}")

            self.log.emit(f"[GIF] 生成完成: {os.path.basename(self.output_path)}")
            self.gif_finished.emit(self.output_path)
        except Exception as e:
            self.error.emit(f"[GIF错误] {e}")


# ==============================================================================
# View: ROI 选区覆盖层 —— 核心坐标映射在此实现
# ==============================================================================

class ROIOverlay(QObject):
    """
    ROI 选区逻辑控制器，处理鼠标拖拽绘制 ROI 矩形。
    通过事件过滤器安装在 MainVideoWidget 上，拦截鼠标事件。
    不负责绘制——ROI 可视化由 ROIPreviewWidget 提供。

    ═══════════════════ 坐标映射公式 ═══════════════════
    设:
      - 原视频分辨率:  (orig_w, orig_h)
      - 控件尺寸:      (wg_w,  wg_h)    ← resizeEvent 中实时更新

    视频实际显示区域（居中，保留黑边）:
      scale   = min(wg_w / orig_w, wg_h / orig_h)
      disp_w  = orig_w * scale
      disp_h  = orig_h * scale
      off_x   = (wg_w - disp_w) / 2      ← 水平黑边宽度
      off_y   = (wg_h - disp_h) / 2      ← 垂直黑边宽度

    UI坐标 → 原视频坐标:
      video_x = (ui_x - off_x) / scale
      video_y = (ui_y - off_y) / scale

    原视频坐标 → UI坐标:
      ui_x = video_x * scale + off_x
      ui_y = video_y * scale + off_y
    ════════════════════════════════════════════════════
    """

    roi_changed: pyqtSignal = pyqtSignal(object)  # (x1, y1, x2, y2) 原视频坐标 或 None

    def __init__(self, video_widget):
        """video_widget 应为 MainVideoWidget 实例"""
        super().__init__(video_widget)
        self._video = video_widget

        # 原视频分辨率（外部设置）
        self.orig_w = 1920
        self.orig_h = 1080

        # 坐标映射参数（在 resizeEvent 中计算）
        self.scale = 1.0
        self.off_x = 0
        self.off_y = 0

        # ROI 矩形（原视频坐标系）
        self.roi_rect: Optional[tuple] = None  # (x1, y1, x2, y2)

        # 拖拽/调整 状态
        self._drawing = False
        self._start_ui = QPoint()
        self._current_ui = QPoint()

        # 边缘 resize 状态
        self._resize_edge: Optional[str] = None  # 'tl','tr','bl','br','t','b','l','r'
        self._resize_orig_rect: Optional[tuple] = None  # resize 开始时的原始 roi_rect

    # ---------- 坐标映射 ----------

    def update_mapping(self):
        """根据控件尺寸和原视频分辨率，重新计算映射参数。"""
        if self.orig_w <= 0 or self.orig_h <= 0:
            self.scale, self.off_x, self.off_y = 1.0, 0, 0
            return
        wg_w = self._video.width()
        wg_h = self._video.height()
        if wg_w <= 0 or wg_h <= 0:
            return
        self.scale = min(wg_w / self.orig_w, wg_h / self.orig_h)
        self.off_x = (wg_w - self.orig_w * self.scale) / 2
        self.off_y = (wg_h - self.orig_h * self.scale) / 2

    def ui_to_video(self, ui_x: int, ui_y: int) -> tuple:
        """将 UI 控件坐标映射为原视频坐标"""
        vx = (ui_x - self.off_x) / self.scale
        vy = (ui_y - self.off_y) / self.scale
        return vx, vy

    def video_to_ui(self, vx: float, vy: float) -> tuple:
        """将原视频坐标映射回 UI 控件坐标"""
        ux = vx * self.scale + self.off_x
        uy = vy * self.scale + self.off_y
        return ux, uy

    # ---------- 获取当前交互选区 ----------

    def get_drag_ui_rect(self) -> Optional[tuple]:
        """
        返回当前交互（拖拽或 resize）中的选区在 UI 控件坐标系下的矩形坐标。
        仅在进行中时返回有效值，用于实时绘制覆盖层。
        """
        if self._resize_edge and self.roi_rect:
            # resize 中：将当前 roi_rect 转换为 UI 坐标
            vx1, vy1, vx2, vy2 = self.roi_rect
            ux1, uy1 = self.video_to_ui(vx1, vy1)
            ux2, uy2 = self.video_to_ui(vx2, vy2)
            return (ux1, uy1, ux2, uy2)
        if self._drawing:
            x1 = min(self._start_ui.x(), self._current_ui.x())
            y1 = min(self._start_ui.y(), self._current_ui.y())
            x2 = max(self._start_ui.x(), self._current_ui.x())
            y2 = max(self._start_ui.y(), self._current_ui.y())
            return (x1, y1, x2, y2)
        return None

    # ---------- 设置视频分辨率 ----------

    def set_video_resolution(self, w: int, h: int):
        """设置原视频分辨率并重新计算映射"""
        self.orig_w = w
        self.orig_h = h
        self.roi_rect = None
        self._resize_edge = None
        self._resize_orig_rect = None
        self.update_mapping()
        self._video.update()

    # ---------- 获取 ROI ----------

    def get_roi_video_coords(self) -> Optional[tuple]:
        """返回 (x1, y1, x2, y2) 原视频坐标"""
        return self.roi_rect

    def has_roi(self) -> bool:
        return self.roi_rect is not None

    def clear_roi(self):
        self.roi_rect = None
        self._resize_edge = None
        self._resize_orig_rect = None
        self.roi_changed.emit(None)
        self._video.update()

    # ---------- 事件过滤器（拦截鼠标事件用于 ROI 拖拽 / 调整） ----------

    def _get_video_display_rect(self) -> tuple:
        """返回视频显示区域的 UI 坐标边界 (left, top, right, bottom)"""
        self.update_mapping()
        l = self.off_x
        t = self.off_y
        r = self.off_x + self.orig_w * self.scale
        b = self.off_y + self.orig_h * self.scale
        return l, t, r, b

    def _hit_test_roi_edge(self, ui_pos) -> Optional[str]:
        """
        检测鼠标是否靠近已有 ROI 的边缘。
        返回边缘标识：'tl','tr','bl','br','t','b','l','r' 或 None。
        """
        if not self.roi_rect:
            return None
        vx1, vy1, vx2, vy2 = self.roi_rect
        ux1, uy1 = self.video_to_ui(vx1, vy1)
        ux2, uy2 = self.video_to_ui(vx2, vy2)
        px, py = ui_pos.x(), ui_pos.y()
        threshold = 8
        near_l = abs(px - ux1) <= threshold
        near_r = abs(px - ux2) <= threshold
        near_t = abs(py - uy1) <= threshold
        near_b = abs(py - uy2) <= threshold
        if near_l and near_t: return 'tl'
        if near_r and near_t: return 'tr'
        if near_l and near_b: return 'bl'
        if near_r and near_b: return 'br'
        if near_l: return 'l'
        if near_r: return 'r'
        if near_t: return 't'
        if near_b: return 'b'
        return None

    def eventFilter(self, obj, event):
        """拦截 MainVideoWidget 上的鼠标事件"""
        t = event.type()
        if t == QEvent.Type.MouseButtonPress:
            return self._on_press(event)
        if t == QEvent.Type.MouseMove:
            return self._on_move(event)
        if t == QEvent.Type.MouseButtonRelease:
            return self._on_release(event)
        if t == QEvent.Type.Resize:
            self.update_mapping()
        return False

    def _on_press(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            # ① ROI 已存在 → 检查是否靠近边缘（resize 模式）
            if self.roi_rect:
                edge = self._hit_test_roi_edge(ev.pos())
                if edge:
                    self._resize_edge = edge
                    self._resize_orig_rect = self.roi_rect
                    self._video.update()
                    return True
            # ② 否则开始全新的拖拽选区
            self._drawing = True
            self._start_ui = ev.pos()
            self._current_ui = ev.pos()
            self.roi_rect = None
            self._resize_edge = None
            self._resize_orig_rect = None
            self._video.update()
            return True
        return False

    def _on_move(self, ev):
        # ① resize 模式：实时更新 ROI 矩形 + 设置对应方向光标
        if self._resize_edge:
            self._update_resize_rect(ev.pos())
            self._video.setCursor(self._get_cursor_for_edge(self._resize_edge))
            self._video.update()
            return True
        # ② 拖拽模式：钳位到视频显示区域 + 实时更新
        if self._drawing:
            l, t, r, b = self._get_video_display_rect()
            cx = max(l, min(float(ev.pos().x()), r))
            cy = max(t, min(float(ev.pos().y()), b))
            self._current_ui = QPoint(round(cx), round(cy))
            self._video.update()
            return True
        # ③ 悬停检测：靠近 ROI 边缘时提示可拖拽
        if self.roi_rect:
            edge = self._hit_test_roi_edge(ev.pos())
            if edge:
                self._video.setCursor(self._get_cursor_for_edge(edge))
                return False
        self._video.setCursor(Qt.CursorShape.ArrowCursor)
        return False

    @staticmethod
    def _get_cursor_for_edge(edge: str) -> 'Qt.CursorShape':
        """根据边缘方向返回对应的鼠标指针类型"""
        mapping = {
            'l': Qt.CursorShape.SizeHorCursor,
            'r': Qt.CursorShape.SizeHorCursor,
            't': Qt.CursorShape.SizeVerCursor,
            'b': Qt.CursorShape.SizeVerCursor,
            'tl': Qt.CursorShape.SizeFDiagCursor,
            'br': Qt.CursorShape.SizeFDiagCursor,
            'tr': Qt.CursorShape.SizeBDiagCursor,
            'bl': Qt.CursorShape.SizeBDiagCursor,
        }
        return mapping.get(edge, Qt.CursorShape.ArrowCursor)

    def _update_resize_rect(self, ui_pos):
        """根据鼠标位置和当前调整的边缘，计算新的 ROI 矩形"""
        if not self._resize_edge or not self._resize_orig_rect:
            return
        ox1, oy1, ox2, oy2 = self._resize_orig_rect
        # 鼠标位置 → 原视频坐标（钳位到视频边界）
        vx = max(0.0, min((ui_pos.x() - self.off_x) / self.scale, self.orig_w))
        vy = max(0.0, min((ui_pos.y() - self.off_y) / self.scale, self.orig_h))
        edge = self._resize_edge
        if 'l' in edge:
            ox1 = vx
        if 'r' in edge:
            ox2 = vx
        if 't' in edge:
            oy1 = vy
        if 'b' in edge:
            oy2 = vy
        n_x1 = min(ox1, ox2)
        n_y1 = min(oy1, oy2)
        n_x2 = max(ox1, ox2)
        n_y2 = max(oy1, oy2)
        if n_x2 - n_x1 >= 4 and n_y2 - n_y1 >= 4:
            self.roi_rect = (round(n_x1), round(n_y1), round(n_x2), round(n_y2))
            self.roi_changed.emit(self.roi_rect)
        else:
            self.roi_rect = None
            self.roi_changed.emit(None)

    def _on_release(self, ev):
        # ① 结束 resize
        if self._resize_edge and ev.button() == Qt.MouseButton.LeftButton:
            self._resize_edge = None
            self._resize_orig_rect = None
            self._video.update()
            return True
        # ② 结束拖拽选区
        if ev.button() == Qt.MouseButton.LeftButton and self._drawing:
            self._drawing = False
            # _current_ui 已在 _on_move 中钳位，直接用
            vx1, vy1 = self.ui_to_video(self._start_ui.x(), self._start_ui.y())
            vx2, vy2 = self.ui_to_video(self._current_ui.x(), self._current_ui.y())
            x1 = max(0, min(round(vx1), round(vx2)))
            y1 = max(0, min(round(vy1), round(vy2)))
            x2 = min(self.orig_w, max(round(vx1), round(vx2)))
            y2 = min(self.orig_h, max(round(vy1), round(vy2)))
            if x2 - x1 >= 4 and y2 - y1 >= 4:
                self.roi_rect = (x1, y1, x2, y2)
                self.roi_changed.emit(self.roi_rect)
            else:
                self.roi_rect = None
                self.roi_changed.emit(None)
            self._video.update()
            return True
        return False

    # ====== ROIOverlay 结束 ======


# ==============================================================================
# View: 16:9 比例容器
# ==============================================================================

class AspectRatioContainer(QWidget):
    """
    将子 widget 约束在 16:9 比例内，居中显示。
    多余空间显示为暗色背景（模拟黑边效果）。
    """

    def __init__(self, child: QWidget, parent=None):
        super().__init__(parent)
        self._child = child
        child.setParent(self)
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor(10, 10, 18))
        self.setPalette(pal)

    def resizeEvent(self, ev: QResizeEvent):
        super().resizeEvent(ev)
        cw = self.width()
        ch = self.height()
        # 在可用空间内找到最大的 16:9 矩形
        if cw * 9 > ch * 16:
            # 容器太宽，以高度为基准
            child_h = ch
            child_w = int(ch * 16 / 9)
        else:
            # 容器太高，以宽度为基准
            child_w = cw
            child_h = int(cw * 9 / 16)
        x = (cw - child_w) // 2
        y = (ch - child_h) // 2
        self._child.setGeometry(x, y, child_w, child_h)


# ==============================================================================
# View: 主视频显示区域（FfmpegPlayer 帧回调 → QPainter 渲染）
# ==============================================================================

class MainVideoWidget(QWidget):
    """
    主视频显示区域。
    接收 FfmpegPlayer 帧回调解码后的 QImage，通过 QPainter 渲染到屏幕上。
    鼠标事件由 ROIOverlay 事件过滤器处理，用于框选 ROI 区域。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)

        self.frame_image: Optional[QImage] = None
        self._orig_w = 1920
        self._orig_h = 1080

        # ROIOverlay 弱引用（用于读取拖拽状态和选区坐标）
        self._roi_overlay: Optional['ROIOverlay'] = None

        # 背景颜色
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor(30, 30, 40))
        self.setPalette(pal)

    def set_video_resolution(self, w: int, h: int):
        self._orig_w = w
        self._orig_h = h

    def set_frame(self, image: QImage):
        """接收 FFmpeg 解码后的 QImage 帧"""
        self.frame_image = image
        self.repaint()

    def set_roi_overlay(self, overlay: 'ROIOverlay'):
        """设置 ROIOverlay 引用，用于在绘制时读取选区数据"""
        self._roi_overlay = overlay

    def paintEvent(self, event: QPaintEvent):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        if self.frame_image and not self.frame_image.isNull():
            img = self.frame_image
            # 等比例缩放
            scaled = img.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            p.drawImage(x, y, scaled)

            # ── 绘制 ROI 选区半透明覆盖层 ──
            self._draw_roi_overlay(p)
        else:
            p.fillRect(self.rect(), QColor(30, 30, 40))
            p.setPen(QColor(120, 120, 140))
            p.setFont(QFont("Microsoft YaHei", 12))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "等待载入视频...")
        p.end()

    def _draw_roi_overlay(self, p: QPainter):
        """在视频画面上绘制 ROI 选区半透明覆盖层"""
        overlay = self._roi_overlay
        if not overlay:
            return

        # 确保坐标映射最新
        overlay.update_mapping()

        ux1 = uy1 = ux2 = uy2 = None
        is_dragging = False

        # ① 优先读取拖拽中的选区（实时跟随鼠标）
        drag_rect = overlay.get_drag_ui_rect()
        if drag_rect:
            ux1, uy1, ux2, uy2 = drag_rect
            is_dragging = True
        # ② 否则读取已确定的 ROI（拖拽完成 or SpinBox 编辑）
        elif overlay.roi_rect:
            vx1, vy1, vx2, vy2 = overlay.roi_rect
            ux1, uy1 = overlay.video_to_ui(vx1, vy1)
            ux2, uy2 = overlay.video_to_ui(vx2, vy2)

        if ux1 is None:
            return  # 无选区

        # 构建矩形（钳位到控件范围内）
        rect = QRectF(
            max(0.0, ux1),
            max(0.0, uy1),
            max(4.0, ux2 - ux1),  # 最小宽度 4px
            max(4.0, uy2 - uy1),  # 最小高度 4px
        )

        if is_dragging and overlay._drawing:
            # 新拖拽中：更淡的填充，虚线边框
            fill = QColor(0, 180, 255, 25)
            border = QPen(QColor(0, 180, 255, 180), 2, Qt.PenStyle.DashLine)
        else:
            # 已确定或 resize 中：半透明填充 + 实线边框
            fill = QColor(0, 180, 255, 40)
            border = QPen(QColor(0, 180, 255, 220), 2)

        p.setBrush(QBrush(fill))
        p.setPen(border)
        p.drawRect(rect)


# ==============================================================================
# View: ROI 裁剪预览窗口
# ==============================================================================

class ROIPreviewWidget(QWidget):
    """
    ROI 预览窗口，实时显示当前 ROI 选区内的裁剪画面。
    同步主视频的播放进度和时间轴操作。
    若无 ROI 选区，显示提示文字。
    尺寸自适应：宽度跟随父布局，高度按 16:9 比例自动计算。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(140, 80)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setToolTip("画面截取区域实时预览")

        self._frame_image: Optional[QImage] = None
        self._roi: Optional[tuple] = None  # (x1,y1,x2,y2) 原视频坐标

        # 背景颜色（暗色）
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor(20, 20, 30))
        self.setPalette(pal)

    def sizeHint(self):
        """建议尺寸：宽度 200，高度 16:9"""
        return QSize(200, 113)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w: int) -> int:
        """按 16:9 计算高度，无 ROI 时回退到 4:3"""
        if self._roi:
            x1, y1, x2, y2 = self._roi
            rw, rh = x2 - x1, y2 - y1
            if rw > 0 and rh > 0:
                return int(w * rh / rw)
        return int(w * 9 / 16)

    def set_frame(self, image: QImage):
        """设置当前视频帧图像"""
        self._frame_image = image
        self.repaint()

    def set_roi(self, roi: Optional[tuple]):
        """设置当前 ROI 选区（原视频坐标）"""
        self._roi = roi
        self.update()

    def paintEvent(self, event: QPaintEvent):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        if not self._roi or not self._frame_image or self._frame_image.isNull():
            # 无 ROI 或无帧：显示占位提示
            p.fillRect(self.rect(), QColor(20, 20, 30))
            # 提示文字
            p.setPen(QColor(130, 130, 150))
            p.setFont(QFont("Microsoft YaHei", 9))
            p.drawText(self.rect().adjusted(0, 18, 0, 0),
                       Qt.AlignmentFlag.AlignCenter,
                       "在视频上拖拽选择画面\n截取区域")
        else:
            x1, y1, x2, y2 = self._roi
            fw, fh = self._frame_image.width(), self._frame_image.height()
            # 边界钳位
            x1 = max(0, min(x1, fw))
            y1 = max(0, min(y1, fh))
            x2 = max(0, min(x2, fw))
            y2 = max(0, min(y2, fh))

            if x2 > x1 and y2 > y1:
                cropped = self._frame_image.copy(x1, y1, x2 - x1, y2 - y1)
                # 等比例缩放到预览窗口
                scaled = cropped.scaled(
                    self.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                x = (self.width() - scaled.width()) // 2
                y = (self.height() - scaled.height()) // 2
                p.drawImage(x, y, scaled)

                # 尺寸标签（控件底部偏右，带暗色半透明背景，避免与画面底色混淆）
                label_text = f"{x2 - x1}×{y2 - y1}"
                p.setFont(QFont("Consolas", 8))
                fm = p.fontMetrics()
                lw = fm.horizontalAdvance(label_text) + 10
                lh = fm.height() + 4
                lx = self.width() - lw - 4
                ly = self.height() - lh - 4
                p.setBrush(QColor(20, 20, 30, 200))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawRoundedRect(lx, ly, lw, lh, 3, 3)
                p.setPen(QColor(200, 255, 200, 240))
                p.drawText(lx + 5, ly + 2 + fm.ascent(), label_text)
            else:
                p.setPen(QColor(130, 130, 150))
                p.setFont(QFont("Microsoft YaHei", 9))
                p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "无效 ROI 区域")
        p.end()


# ==============================================================================
# View: 视频播放器控件（FfmpegPlayer + ROIOverlay + 播放控制）
# ==============================================================================

class VideoPlayerWidget(QWidget):
    """
    视频预览组件，组合:
      - MainVideoWidget:   主视频显示（FfmpegPlayer 帧回调 → QPainter 渲染）
      - ROIPreviewWidget:  ROI 裁剪预览窗口
      - ROIOverlay:        ROI 选区逻辑控制器（事件过滤 + 坐标映射）
      - 底部控制栏:         播放/暂停、自定义 TimelineWidget
    """

    position_changed: pyqtSignal = pyqtSignal(float)  # 当前播放位置(秒)
    duration_changed: pyqtSignal = pyqtSignal(float)  # 视频时长(秒)
    roi_changed: pyqtSignal = pyqtSignal(object)  # ROI 选区(原视频坐标) 或 None

    def __init__(self, parent=None):
        super().__init__(parent)

        # --- FFmpeg 软件解码播放器（替代 QMediaPlayer）---
        self.media_player = FfmpegPlayer(self)

        # --- 主视频显示区域（QPainter 渲染）---
        self.main_video = MainVideoWidget()

        # --- ROI 预览窗口 ---
        self.roi_preview = ROIPreviewWidget()

        # ROIOverlay（事件过滤器，安装在 main_video 上用于鼠标拖拽框选）
        self.roi_overlay = ROIOverlay(self.main_video)
        self.roi_overlay.roi_changed.connect(self.roi_changed)
        self.roi_overlay.roi_changed.connect(self._on_roi_for_preview)
        self.roi_overlay.roi_changed.connect(lambda: self.main_video.update())  # ROI 变化时触发重绘
        self.main_video.installEventFilter(self.roi_overlay)

        # 让 MainVideoWidget 持有 ROIOverlay 引用以读取选区数据
        self.main_video.set_roi_overlay(self.roi_overlay)

        # 注册帧回调（接收 FfmpegPlayer 输出的解码帧）
        self.media_player.add_frame_callback(self._on_frame_changed)

        # --- 播放控制按钮 ---
        self.btn_play: QPushButton = QPushButton("▶ 播放")
        self.btn_play.setFixedWidth(90)
        self.btn_play.clicked.connect(self._toggle_play)

        self.btn_stop: QPushButton = QPushButton("■ 停止")
        self.btn_stop.setFixedWidth(90)
        self.btn_stop.clicked.connect(self._stop)

        self.btn_loop: QPushButton = QPushButton("🔁 循环")
        self.btn_loop.setFixedWidth(90)
        self.btn_loop.setCheckable(True)
        self.btn_loop.setToolTip("A-B 循环播放：在开始/结束标记间循环")
        self.btn_loop.toggled.connect(self._on_loop_toggled)

        self.btn_clear_roi: QPushButton = QPushButton("清除截取区域")
        self.btn_clear_roi.setFixedWidth(100)
        self.btn_clear_roi.clicked.connect(self.roi_overlay.clear_roi)

        # 循环状态
        self._loop_suppress_until_enter = False  # 用户手动跳转到区间外后抑制循环

        # --- 自定义时间轴 ---
        self.timeline = TimelineWidget()
        self.timeline.seek_requested.connect(self._seek_to)
        self.timeline.markers_changed.connect(self._on_markers_changed)
        self.timeline.marker_drag_started.connect(self._on_marker_drag_started)

        # --- 布局: 主视频(16:9比例) + 预览窗口 并排 ---
        video_row = QHBoxLayout()
        video_row.setContentsMargins(0, 0, 0, 0)
        video_row.setSpacing(8)

        # 16:9 比例容器包裹主视频
        self._video_container = AspectRatioContainer(self.main_video)
        video_row.addWidget(self._video_container, 4)  # 占 80% 宽度

        # 预览列（占 20% 宽度），内容垂直居中
        preview_wrapper = QVBoxLayout()
        preview_wrapper.setContentsMargins(0, 0, 0, 0)
        preview_wrapper.setSpacing(4)
        preview_wrapper.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview_label = QLabel("画面截取预览")
        preview_label.setStyleSheet("color: #89b4fa; font-weight: bold; font-size: 10px;")
        preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview_wrapper.addWidget(preview_label, 0, Qt.AlignmentFlag.AlignCenter)
        preview_wrapper.addWidget(self.roi_preview, 0, Qt.AlignmentFlag.AlignCenter)
        video_row.addLayout(preview_wrapper, 1)

        control_row = QHBoxLayout()
        control_row.setContentsMargins(4, 2, 4, 2)
        control_row.addWidget(self.btn_play)
        control_row.addWidget(self.btn_stop)
        control_row.addWidget(self.btn_loop)
        control_row.addSpacing(10)
        control_row.addWidget(self.btn_clear_roi)
        control_row.addStretch()
        hint_label = QLabel("提示: 在主视频上左键拖拽选择画面截取区域，预览窗口实时显示裁剪效果")
        hint_label.setStyleSheet("color: #a6adc8;")
        control_row.addWidget(hint_label)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(2)
        main_layout.addLayout(video_row, 1)
        main_layout.addWidget(self.timeline)
        main_layout.addLayout(control_row)

        # --- 播放器信号连接 ---
        self.media_player.positionChanged.connect(self._on_position_changed)
        self.media_player.durationChanged.connect(self._on_duration_changed)
        self.media_player.playbackStateChanged.connect(self._on_state_changed)
        self.media_player.errorOccurred.connect(self._on_error)

        # --- 视频切换时自动更新 ROI 分辨率 ---
        self._pending_video_path: Optional[str] = None

        # 定时器：每 100ms 刷新一次预览（播放时频率足够）
        self._preview_timer: QTimer = QTimer(self)
        self._preview_timer.setInterval(33)
        self._preview_timer.timeout.connect(self._refresh_preview)
        self._preview_timer.start()

    # ---------- 帧回调 ----------

    def _on_frame_changed(self, image: QImage):
        """FfmpegPlayer 新的视频帧到达"""
        self.main_video.set_frame(image)
        self._refresh_preview()

    def _on_roi_for_preview(self, roi: Optional[tuple]):
        """ROI 变化 → 更新预览窗口"""
        self.roi_preview.set_roi(roi)
        self._refresh_preview()

    def _refresh_preview(self):
        """刷新预览窗口的当前帧"""
        if self.main_video.frame_image and not self.main_video.frame_image.isNull():
            self.roi_preview.set_frame(self.main_video.frame_image)

    # ---------- 公共接口 ----------

    def load_video(self, file_path: str):
        """加载视频文件进行预览"""
        self._pending_video_path = file_path
        self.btn_stop.click()
        self.media_player.set_source(QUrl.fromLocalFile(file_path))
        try:
            w, h = get_video_resolution(file_path)
            self.roi_overlay.set_video_resolution(w, h)
            self.main_video.set_video_resolution(w, h)
        except Exception:
            pass
        self.media_player.play()
        self.btn_play.setText("⏸ 暂停")

    def get_current_position(self) -> float:
        return self.media_player.position() / 1000.0

    def get_duration(self) -> float:
        return self.media_player.duration() / 1000.0

    def get_roi(self) -> Optional[tuple]:
        return self.roi_overlay.get_roi_video_coords()

    def get_markers(self) -> tuple:
        return self.timeline.get_markers()

    def pause(self):
        self.media_player.pause()

    @property
    def orig_resolution(self) -> tuple:
        return self.roi_overlay.orig_w, self.roi_overlay.orig_h

    # ---------- 内部槽 ----------

    def _toggle_play(self):
        if self.media_player.playback_state() == FfmpegPlayer.PlayingState:
            self.media_player.pause()
        else:
            self.media_player.play()

    def _stop(self):
        self.media_player.stop()
        self._loop_suppress_until_enter = False

    def _seek_to(self, secs: float):
        # 循环激活时：若手动跳转到区间外，抑制循环直到重新进入区间
        if self.btn_loop.isChecked():
            start, end = self.timeline.get_markers()
            if secs < start or secs > end:
                self._loop_suppress_until_enter = True
            else:
                self._loop_suppress_until_enter = False
        self.media_player.set_position(int(secs * 1000))

    def _on_loop_toggled(self, checked: bool):
        """循环按钮切换"""
        self._loop_suppress_until_enter = False
        self.timeline.set_loop_highlight(checked)
        if checked:
            self.btn_loop.setStyleSheet(
                "background-color: #1e66f5; color: #fff; font-weight: bold;"
                "border: 1px solid #89b4fa; border-radius: 4px; padding: 4px 8px;"
            )
        else:
            self.btn_loop.setStyleSheet("")

    def _on_markers_changed(self):
        if self.media_player.playback_state() == FfmpegPlayer.PlayingState:
            self.media_player.pause()

    def _on_marker_drag_started(self, pos_s: float):
        if self.media_player.playback_state() == FfmpegPlayer.PlayingState:
            self.media_player.pause()
        self.media_player.set_position(int(pos_s * 1000))

    def _on_position_changed(self, pos_ms: int):
        secs = pos_ms / 1000.0
        self.position_changed.emit(secs)
        self.timeline.set_position(secs)

        # --- A-B 循环逻辑 ---
        if self.btn_loop.isChecked() and \
                self.media_player.playback_state() == FfmpegPlayer.PlayingState:
            start_s, end_s = self.timeline.get_markers()
            if self._loop_suppress_until_enter:
                # 用户之前跳到区间外，等待重新进入
                if start_s <= secs <= end_s:
                    self._loop_suppress_until_enter = False
            elif secs >= end_s - 0.05:
                # 到达结束标记，跳回开始标记
                self.media_player.set_position(int(start_s * 1000))

    def _on_duration_changed(self, dur_ms: int):
        secs = dur_ms / 1000.0
        self.duration_changed.emit(secs)
        self.timeline.set_duration(secs)
        self.timeline.set_markers(0, secs)

    def _on_state_changed(self, state: int):
        if state == FfmpegPlayer.PlayingState:
            self.btn_play.setText("⏸ 暂停")
        else:
            self.btn_play.setText("▶ 播放")

    def _on_error(self, error_string):
        print(f"[播放器错误] {error_string}")
        # 取父窗口（MainWindow）显示用户友好的消息框
        parent = self.window()
        if parent and hasattr(parent, '_log'):
            parent._log(f"❌ 播放器错误: {error_string}")  # type: ignore[reportPrivateUsage]


# ==============================================================================
# View: 自定义时间轴控件（带开始/结束打点标记）
# ==============================================================================

class TimelineWidget(QWidget):
    """
    自定义视频时间轴，功能:
      - 显示播放进度（蓝色填充）
      - 显示选区范围（绿色高亮带）
      - 可拖拽的开始标记 (▼ 绿色) 和结束标记 (▼ 红色)
      - 点击跳转播放位置
    """

    seek_requested: pyqtSignal = pyqtSignal(float)  # 用户点击时间轴请求跳转（秒）
    markers_changed: pyqtSignal = pyqtSignal(float, float)  # 开始/结束标记改变 (start_s, end_s)
    marker_drag_started: pyqtSignal = pyqtSignal(float)  # 开始拖拽标记，参数为当前标记位置（秒），应暂停视频

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(48)
        self.setMouseTracking(True)

        self.duration = 1.0  # 视频总时长（秒）
        self.position = 0.0  # 当前播放位置（秒）
        self.marker_start = 0.0  # 开始标记（秒）
        self.marker_end = 1.0  # 结束标记（秒）

        # 拖拽状态: None / 'start' / 'end' / 'seek'
        self._dragging = None

        # 循环高亮
        self._loop_highlight = False

    # ---------- 设置 ----------

    def set_duration(self, secs: float):
        self.duration = max(0.1, secs)
        self.marker_end = min(self.marker_end, self.duration)
        self.update()

    def set_position(self, secs: float):
        self.position = max(0.0, min(secs, self.duration))
        self.update()

    def set_markers(self, start_s: float, end_s: float):
        self.marker_start = max(0.0, min(start_s, self.duration))
        self.marker_end = max(self.marker_start, min(end_s, self.duration))
        self.update()

    def get_markers(self) -> tuple:
        return self.marker_start, self.marker_end

    def set_loop_highlight(self, enabled: bool):
        """启用/禁用 A-B 循环区间高亮"""
        self._loop_highlight = enabled
        self.update()

    # ---------- 坐标换算 ----------

    def _time_to_x(self, t: float) -> int:
        """将时间(秒)映射为控件 X 坐标"""
        margin = 12
        bar_w = self.width() - 2 * margin
        return margin + int(bar_w * (t / self.duration)) if self.duration > 0 else margin

    def _x_to_time(self, x: int) -> float:
        """将控件 X 坐标映射为时间(秒)"""
        margin = 12
        bar_w = self.width() - 2 * margin
        t = (x - margin) / bar_w * self.duration
        return max(0.0, min(t, self.duration))

    # ---------- 鼠标事件 ----------

    def _hit_marker(self, x: int) -> Optional[str]:
        """检测鼠标是否靠近某个标记，返回 'start' / 'end' / None"""
        sx = self._time_to_x(self.marker_start)
        ex = self._time_to_x(self.marker_end)
        if abs(x - sx) < 8 and self.duration > 0:
            return 'start'
        if abs(x - ex) < 8:
            return 'end'
        return None

    def mousePressEvent(self, ev: QMouseEvent):
        if ev.button() == Qt.MouseButton.LeftButton:
            hit = self._hit_marker(ev.pos().x())
            self._dragging = hit if hit else 'seek'
            t = self._x_to_time(ev.pos().x())
            if hit:
                # 拖拽标记：暂停视频并跳转到标记位置
                self.marker_drag_started.emit(
                    self.marker_start if hit == 'start' else self.marker_end)
            self.seek_requested.emit(t)

    def mouseMoveEvent(self, ev: QMouseEvent):
        x = ev.pos().x()
        if self._dragging == 'start':
            t = self._x_to_time(x)
            self.marker_start = min(t, self.marker_end - 0.05)
            self.markers_changed.emit(self.marker_start, self.marker_end)
            self.seek_requested.emit(self.marker_start)
            self.update()
        elif self._dragging == 'end':
            t = self._x_to_time(x)
            self.marker_end = max(t, self.marker_start + 0.05)
            self.markers_changed.emit(self.marker_start, self.marker_end)
            self.seek_requested.emit(self.marker_end)
            self.update()
        elif self._dragging == 'seek':
            t = self._x_to_time(x)
            self.seek_requested.emit(t)
        else:
            # 悬停检测：鼠标在标记上时改光标
            hit = self._hit_marker(x)
            self.setCursor(
                Qt.CursorShape.SizeHorCursor if hit else Qt.CursorShape.PointingHandCursor
            )

    def mouseReleaseEvent(self, ev: QMouseEvent):
        self._dragging = None

    # ---------- 绘制 ----------

    def paintEvent(self, ev: QPaintEvent):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        margin = 12
        bar_y = h // 2 - 3
        bar_h = 6
        bar_w = w - 2 * margin

        # --- 背景轨道 ---
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(60, 60, 75))
        p.drawRoundedRect(margin, bar_y, bar_w, bar_h, 3, 3)

        # --- 选区范围（绿色高亮带） ---
        sx = self._time_to_x(self.marker_start)
        ex = self._time_to_x(self.marker_end)
        p.setBrush(QColor(64, 200, 64, 100))
        p.drawRect(sx, bar_y, ex - sx, bar_h)

        # --- 循环区间高亮（半透明蓝色覆盖） ---
        if self._loop_highlight:
            p.setBrush(QColor(30, 102, 245, 60))
            p.setPen(QPen(QColor(30, 102, 245, 120), 1))
            p.drawRoundedRect(sx, bar_y - 1, ex - sx, bar_h + 2, 3, 3)
            p.setPen(Qt.PenStyle.NoPen)

        # --- 播放进度（蓝色填充） ---
        px = self._time_to_x(self.position)
        p.setBrush(QColor(30, 102, 245))
        p.drawRoundedRect(margin, bar_y, px - margin, bar_h, 3, 3)

        # --- 标记箭头 ---
        # 开始标记 (绿色 ▼)
        self._draw_marker(p, sx, h, QColor(64, 200, 64), "S")
        # 结束标记 (红色 ▼)
        self._draw_marker(p, ex, h, QColor(255, 80, 80), "E")
        # 播放位置 (白色 ●)
        self._draw_playhead(p, px, h)

        # --- 时间标签 ---
        p.setPen(QColor(180, 180, 200))
        p.setFont(QFont("Consolas", 9))
        p.drawText(margin, h - 4, f"{self.marker_start:.1f}s")
        p.drawText(w - margin - 50, h - 4, f"{self.marker_end:.1f}s")
        p.drawText(w // 2 - 30, h - 4, f"{self.position:.1f}s / {self.duration:.1f}s")

        p.end()

    @staticmethod
    def _draw_marker(p: QPainter, x: int, h: int, color: QColor, label: str):
        """绘制三角形标记"""
        p.setBrush(QBrush(color))
        p.setPen(QPen(color.darker(130), 1))
        size = 8
        tri = QPolygon([
            QPoint(x - size, h // 2 - 3 - 12),
            QPoint(x + size, h // 2 - 3 - 12),
            QPoint(x, h // 2 - 3),
        ])
        p.drawPolygon(tri)
        # 标签
        p.setPen(QColor(255, 255, 255))
        p.setFont(QFont("Arial", 8, QFont.Weight.Bold))
        p.drawText(x - 4, h // 2 - 3 - 14, label)

    @staticmethod
    def _draw_playhead(p: QPainter, x: int, h: int):
        """绘制当前播放位置指示器"""
        p.setBrush(QBrush(QColor(255, 255, 255)))
        p.setPen(QPen(QColor(200, 200, 200), 2))
        r = 5
        p.drawEllipse(QPoint(x, h // 2), r, r)


# ==============================================================================
# View: 主窗口
# ==============================================================================

class MainWindow(QMainWindow):
    """
    主窗口布局:

    ┌──────────────────────────────────────────────────┐
    │  左侧面板 (width≈350)  │  右侧面板 (stretch)       │
    │  ┌──────────────────┐  │  ┌─────────────────────┐ │
    │  │ BV 输入           │  │  │                     │ │
    │  │ [QTextEdit]      │  │  │  VideoPlayerWidget  │ │
    │  │ [下载] [强制下载] │  │  │  (预览 + ROI覆盖层)  │ │
    │  ├──────────────────┤  │  │                     │ │
    │  │ 视频库            │  │  ├─────────────────────┤ │
    │  │ [QListWidget]    │  │  │ 时间轴 + 播放控制   │ │
    │  │                  │  │  ├─────────────────────┤ │
    │  │                  │  │  │ GIF 设置面板         │ │
    │  │                  │  │  │ 输出目录/FPS/缩放   │ │
    │  │                  │  │  │ [生成 GIF]           │ │
    │  └──────────────────┘  │  └─────────────────────┘ │
    ├──────────────────────────────────────────────────┤
    │  日志面板 (高度≈150)                               │
    └──────────────────────────────────────────────────┘
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Bilibili视频GIF制作工具")
        self.setMinimumSize(1200, 750)

        # 状态变量
        self.current_bv_id: Optional[str] = None
        self.current_video_path: Optional[str] = None

        self._build_ui()
        self._connect_signals()
        self._refresh_video_list()

    # ───────────────── 构建 UI ─────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(6, 6, 6, 6)
        root_layout.setSpacing(4)

        # 水平分割器
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([330, 850])
        root_layout.addWidget(splitter, 1)

        # 底部日志
        self._build_log_panel(root_layout)

        # 状态栏
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximumWidth(200)
        self.progress_bar.setVisible(False)
        self.status_bar.addPermanentWidget(self.progress_bar)

    def _build_left_panel(self) -> QWidget:
        """左侧：BV 输入 + 视频库"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(8)

        # --- BV 输入 ---
        grp_input = QGroupBox("📥 BV 号输入（一行一个）")
        il = QVBoxLayout(grp_input)

        self.bv_input = QTextEdit()
        self.bv_input.setPlaceholderText("请输入 BV 号或视频链接，每行一个")
        self.bv_input.setMaximumHeight(200)
        il.addWidget(self.bv_input)

        btn_row = QHBoxLayout()
        self.btn_download: QPushButton = QPushButton("⬇ 下载")
        self.btn_download.setObjectName("btnDownload")
        self.btn_force_dl: QPushButton = QPushButton("强制重下")
        self.btn_clear: QPushButton = QPushButton("清空")
        self.btn_clear.clicked.connect(lambda: self.bv_input.clear())
        btn_row.addWidget(self.btn_download)
        btn_row.addWidget(self.btn_force_dl)
        btn_row.addWidget(self.btn_clear)
        il.addLayout(btn_row)

        layout.addWidget(grp_input)

        # --- 视频库（stretch=1，占据剩余空间）---
        grp_lib = QGroupBox("📂 已缓存视频")
        ll = QVBoxLayout(grp_lib)

        self.video_list: QListWidget = QListWidget()
        self.video_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        ll.addWidget(self.video_list)

        self.btn_refresh: QPushButton = QPushButton("🔄 刷新列表")
        ll.addWidget(self.btn_refresh)

        layout.addWidget(grp_lib, 1)

        return panel

    def _build_right_panel(self) -> QWidget:
        """右侧：视频预览 + GIF 设置"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        # --- 视频预览区 ---
        self.video_player = VideoPlayerWidget()
        layout.addWidget(self.video_player, 1)

        # --- GIF 设置面板 ---
        grp_settings = QGroupBox("⚙️ GIF 生成设置")
        sl = QVBoxLayout(grp_settings)

        # 第一行：输出目录
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("输出目录:"))
        self.output_dir_edit: QLineEdit = QLineEdit(str(BASE_DIR / "output"))
        os.makedirs(str(BASE_DIR / "output"), exist_ok=True)
        self.output_dir_edit.setMinimumWidth(250)
        row1.addWidget(self.output_dir_edit)
        self.btn_browse_dir: QPushButton = QPushButton("浏览...")
        self.btn_browse_dir.clicked.connect(self._browse_output_dir)
        row1.addWidget(self.btn_browse_dir)
        sl.addLayout(row1)

        # 第二行：时间/FPS/缩放
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("开始时间(s):"))
        self.spin_start: QDoubleSpinBox = QDoubleSpinBox()
        self.spin_start.setDecimals(1)
        self.spin_start.setSingleStep(0.1)
        self.spin_start.setRange(0, 99999)
        self.spin_start.setValue(0)
        self.spin_start.setFixedWidth(90)
        self.spin_start.setStyleSheet(
            "QDoubleSpinBox { padding-right: 20px; }"
            "QDoubleSpinBox::up-button, QDoubleSpinBox::down-button { width: 20px; }"
        )
        row2.addWidget(self.spin_start)

        self.btn_set_start: QPushButton = QPushButton("SET")
        self.btn_set_start.setFixedWidth(50)
        self.btn_set_start.setToolTip("将当前播放时间设为开始时间")
        self.btn_set_start.clicked.connect(self._on_set_start)
        row2.addWidget(self.btn_set_start)

        row2.addSpacing(8)
        row2.addWidget(QLabel("结束时间(s):"))
        self.spin_end: QDoubleSpinBox = QDoubleSpinBox()
        self.spin_end.setDecimals(1)
        self.spin_end.setSingleStep(0.1)
        self.spin_end.setRange(0.1, 99999)
        self.spin_end.setValue(5.0)
        self.spin_end.setFixedWidth(90)
        self.spin_end.setStyleSheet(
            "QDoubleSpinBox { padding-right: 20px; }"
            "QDoubleSpinBox::up-button, QDoubleSpinBox::down-button { width: 20px; }"
        )
        row2.addWidget(self.spin_end)

        self.btn_set_end: QPushButton = QPushButton("SET")
        self.btn_set_end.setFixedWidth(50)
        self.btn_set_end.setToolTip("将当前播放时间设为结束时间")
        self.btn_set_end.clicked.connect(self._on_set_end)
        row2.addWidget(self.btn_set_end)

        row2.addSpacing(8)
        row2.addWidget(QLabel("FPS:"))
        self.spin_fps: QSpinBox = QSpinBox()
        self.spin_fps.setRange(1, 60)
        self.spin_fps.setValue(10)
        self.spin_fps.setFixedWidth(80)
        self.spin_fps.setStyleSheet(
            "QSpinBox { padding-right: 20px; }"
            "QSpinBox::up-button, QSpinBox::down-button { width: 20px; }"
        )
        row2.addWidget(self.spin_fps)

        row2.addSpacing(10)
        row2.addWidget(QLabel("缩放比例:"))
        self.spin_resize: QDoubleSpinBox = QDoubleSpinBox()
        self.spin_resize.setDecimals(1)
        self.spin_resize.setRange(0.1, 3.0)
        self.spin_resize.setSingleStep(0.1)
        self.spin_resize.setValue(1.0)
        self.spin_resize.setFixedWidth(80)
        self.spin_resize.setStyleSheet(
            "QDoubleSpinBox { padding-right: 20px; }"
            "QDoubleSpinBox::up-button, QDoubleSpinBox::down-button { width: 20px; }"
        )
        row2.addWidget(self.spin_resize)

        row2.addStretch()
        sl.addLayout(row2)

        # 第三行：ROI 精确坐标编辑 + 生成按钮
        row3 = QHBoxLayout()
        row3.addWidget(QLabel("截取区域:"))
        row3.addWidget(QLabel("x1"))
        self.spin_roi_x1: QSpinBox = QSpinBox()
        self.spin_roi_x1.setRange(0, 99999)
        self.spin_roi_x1.setFixedWidth(100)
        self.spin_roi_x1.setToolTip("截取区域左上角 X 坐标（原视频像素）")
        self.spin_roi_x1.setStyleSheet(
            "QSpinBox { padding-right: 20px; }"
            "QSpinBox::up-button, QSpinBox::down-button { width: 20px; }"
        )
        row3.addWidget(self.spin_roi_x1)

        row3.addWidget(QLabel("y1"))
        self.spin_roi_y1: QSpinBox = QSpinBox()
        self.spin_roi_y1.setRange(0, 99999)
        self.spin_roi_y1.setFixedWidth(100)
        self.spin_roi_y1.setToolTip("截取区域左上角 Y 坐标（原视频像素）")
        self.spin_roi_y1.setStyleSheet(
            "QSpinBox { padding-right: 20px; }"
            "QSpinBox::up-button, QSpinBox::down-button { width: 20px; }"
        )
        row3.addWidget(self.spin_roi_y1)

        row3.addWidget(QLabel("x2"))
        self.spin_roi_x2: QSpinBox = QSpinBox()
        self.spin_roi_x2.setRange(0, 99999)
        self.spin_roi_x2.setFixedWidth(100)
        self.spin_roi_x2.setToolTip("截取区域右下角 X 坐标（原视频像素）")
        self.spin_roi_x2.setStyleSheet(
            "QSpinBox { padding-right: 20px; }"
            "QSpinBox::up-button, QSpinBox::down-button { width: 20px; }"
        )
        row3.addWidget(self.spin_roi_x2)

        row3.addWidget(QLabel("y2"))
        self.spin_roi_y2: QSpinBox = QSpinBox()
        self.spin_roi_y2.setRange(0, 99999)
        self.spin_roi_y2.setFixedWidth(100)
        self.spin_roi_y2.setToolTip("截取区域右下角 Y 坐标（原视频像素）")
        self.spin_roi_y2.setStyleSheet(
            "QSpinBox { padding-right: 20px; }"
            "QSpinBox::up-button, QSpinBox::down-button { width: 20px; }"
        )
        row3.addWidget(self.spin_roi_y2)

        self.roi_info_label: QLabel = QLabel("")
        self.roi_info_label.setStyleSheet("color: #40c840; font-family: Consolas; font-weight: bold;")
        row3.addWidget(self.roi_info_label)

        row3.addStretch()
        self.btn_generate: QPushButton = QPushButton("🎬  生成 GIF")
        self.btn_generate.setObjectName("btnGenerate")
        self.btn_generate.setFixedSize(140, 36)
        row3.addWidget(self.btn_generate)
        sl.addLayout(row3)

        layout.addWidget(grp_settings)

        return panel

    def _build_log_panel(self, parent_layout: QVBoxLayout):
        """底部日志面板"""
        grp_log = QGroupBox("📋 日志")
        ll = QVBoxLayout(grp_log)
        self.log_output: QTextEdit = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMaximumHeight(150)
        self.log_output.setFont(QFont("Consolas", 10))
        ll.addWidget(self.log_output)
        parent_layout.addWidget(grp_log)

    # ───────────────── 信号连接 ─────────────────

    def _connect_signals(self):
        # 下载
        self.btn_download.clicked.connect(self._start_download)
        self.btn_force_dl.clicked.connect(lambda: self._start_download(force=True))

        # 视频库
        self.btn_refresh.clicked.connect(self._refresh_video_list)
        self.video_list.itemDoubleClicked.connect(self._on_video_selected)

        # 视频播放器
        self.video_player.roi_changed.connect(self._on_roi_changed)
        self.video_player.position_changed.connect(self._on_position_update)
        self.video_player.duration_changed.connect(self._on_duration_update)

        # ROI SpinBox 精确编辑 → 画面选区双向同步
        for sb in [self.spin_roi_x1, self.spin_roi_y1, self.spin_roi_x2, self.spin_roi_y2]:
            sb.valueChanged.connect(self._on_roi_spin_changed)

        # 时间设置 - SpinBox ↔ 时间轴双向同步
        self.spin_start.valueChanged.connect(self._on_start_spin_changed)
        self.spin_end.valueChanged.connect(self._on_end_spin_changed)
        self.video_player.timeline.markers_changed.connect(self._on_timeline_markers_update)

        # 生成 GIF
        self.btn_generate.clicked.connect(self._start_generate)

    # ───────────────── 槽函数 ─────────────────

    def _log(self, msg: str):
        self.log_output.append(msg)
        # 自动滚动到底部
        scrollbar = self.log_output.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _append_log(self, msg: str):
        """供线程安全地从工作线程追加日志"""
        self._log(msg)

    # --- 下载 ---

    def _start_download(self, force: bool = False):
        text = self.bv_input.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "警告", "请先输入 BV 号")
            return

        bv_list = [extract_bv_id(line) for line in text.splitlines() if line.strip()]
        self._log(f"\n{'=' * 50}")
        self._log(f"开始下载 {len(bv_list)} 个视频...")

        self._download_queue = bv_list
        self._download_force = force
        self._download_index = 0
        self._process_next_download()

    def _process_next_download(self):
        if self._download_index >= len(self._download_queue):
            self._log("全部下载完成！")
            self._refresh_video_list()
            self.bv_input.clear()
            self.btn_download.setEnabled(True)
            self.btn_force_dl.setEnabled(True)
            self.progress_bar.setVisible(False)
            return

        bv_id = self._download_queue[self._download_index]
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)  # 不确定进度

        self._worker = DownloadWorker(bv_id, self._download_force)
        self._worker.log.connect(self._append_log)
        self._worker.download_finished.connect(self._on_download_finished)
        self._worker.error.connect(self._on_download_error)
        self._worker.start()

        self.btn_download.setEnabled(False)
        self.btn_force_dl.setEnabled(False)

    def _on_download_finished(self, bv_id: str):
        self._log(f"✅ {bv_id} 下载完成")
        self._download_index += 1
        self._refresh_video_list()
        self._process_next_download()

    def _on_download_error(self, msg: str):
        self._log(msg)
        self._download_index += 1
        self._process_next_download()

    # --- 视频库 ---

    def _refresh_video_list(self):
        self.video_list.clear()
        if not CACHE_DIR.exists():
            return
        for fn in sorted(os.listdir(CACHE_DIR)):
            if fn.lower().endswith(VIDEO_EXTENSIONS):
                item = QListWidgetItem(fn)
                item.setData(Qt.ItemDataRole.UserRole, os.path.join(CACHE_DIR, fn))
                self.video_list.addItem(item)
        self.status_bar.showMessage(f"已缓存 {self.video_list.count()} 个视频")

    def _on_video_selected(self, item: QListWidgetItem):
        file_path = item.data(Qt.ItemDataRole.UserRole)
        self.current_video_path = file_path
        # 从文件名提取 BV 号
        fn = os.path.basename(file_path)
        bv_match = re.match(r'(BV\w+)', fn)
        self.current_bv_id = bv_match.group(1) if bv_match else fn.rsplit('.', 1)[0]

        self._log(f"加载视频: {fn}")
        self.status_bar.showMessage(f"正在加载: {fn}")
        self.video_player.load_video(file_path)
        # 同步 ROI SpinBox 上限为视频分辨率
        orig_w, orig_h = self.video_player.orig_resolution
        for sb in [self.spin_roi_x1, self.spin_roi_x2]:
            sb.setMaximum(orig_w)
        for sb in [self.spin_roi_y1, self.spin_roi_y2]:
            sb.setMaximum(orig_h)

    # --- ROI ---

    def _on_roi_changed(self, roi: Optional[tuple]):
        """画面 ROI 选区变化 → 同步 SpinBox + 标签"""
        if roi:
            x1, y1, x2, y2 = roi
            w, h = x2 - x1, y2 - y1
            self.roi_info_label.setText(f"{w}×{h}")
            self.roi_info_label.setStyleSheet(
                "color: #40c840; font-family: Consolas; font-weight: bold;")
            # 反向同步 SpinBox（blockSignals 防递归）
            for sb, v in [(self.spin_roi_x1, x1), (self.spin_roi_y1, y1),
                          (self.spin_roi_x2, x2), (self.spin_roi_y2, y2)]:
                sb.blockSignals(True)
                sb.setValue(v)
                sb.blockSignals(False)
        else:
            self.roi_info_label.setText("")
            self.roi_info_label.setStyleSheet("color: #a6adc8; font-family: Consolas;")
            for sb in [self.spin_roi_x1, self.spin_roi_y1, self.spin_roi_x2, self.spin_roi_y2]:
                sb.blockSignals(True)
                sb.setValue(0)
                sb.blockSignals(False)

    def _on_roi_spin_changed(self):
        """用户手动修改 ROI SpinBox → 更新画面选区"""
        x1 = self.spin_roi_x1.value()
        y1 = self.spin_roi_y1.value()
        x2 = self.spin_roi_x2.value()
        y2 = self.spin_roi_y2.value()
        if x2 > x1 and y2 > y1:
            self.video_player.roi_overlay.roi_rect = (x1, y1, x2, y2)
            self.video_player.roi_overlay.roi_changed.emit((x1, y1, x2, y2))
            # 更新标签
            w, h = x2 - x1, y2 - y1
            self.roi_info_label.setText(f"{w}×{h}")
            self.roi_info_label.setStyleSheet(
                "color: #40c840; font-family: Consolas; font-weight: bold;")

    # --- 时间同步 ---

    def _on_position_update(self, secs: float):
        # 空方法：播放位置变化时不自动同步 spin_start/spin_end，
        # 避免覆盖用户手动输入。时间与 SpinBox 的同步仅通过
        # _on_timeline_markers_update（拖拽时间轴标记时反向同步）。
        pass

    def _on_duration_update(self, secs: float):
        self.spin_end.setValue(secs)
        self.spin_end.setMaximum(secs)
        self.spin_start.setMaximum(secs)

    def _on_start_spin_changed(self, val: float):
        self.video_player.timeline.set_markers(val, self.spin_end.value())

    def _on_end_spin_changed(self, val: float):
        self.video_player.timeline.set_markers(self.spin_start.value(), val)

    def _on_set_start(self):
        """SET 按钮：将当前播放时间设为开始时间"""
        pos = self.video_player.get_current_position()
        if pos < self.spin_end.value():
            self.spin_start.setValue(pos)

    def _on_set_end(self):
        """SET 按钮：将当前播放时间设为结束时间"""
        pos = self.video_player.get_current_position()
        if pos > self.spin_start.value():
            self.spin_end.setValue(pos)

    def _on_timeline_markers_update(self, start_s: float, end_s: float):
        """时间轴上拖拽标记时，反向同步 SpinBox（用 blockSignals 防止递归）"""
        self.spin_start.blockSignals(True)
        self.spin_end.blockSignals(True)
        self.spin_start.setValue(start_s)
        self.spin_end.setValue(end_s)
        self.spin_start.blockSignals(False)
        self.spin_end.blockSignals(False)

    # --- 输出目录 ---

    def _browse_output_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择 GIF 输出目录")
        if d:
            self.output_dir_edit.setText(d)

    # --- GIF 生成 ---

    def _start_generate(self):
        if not self.current_video_path:
            QMessageBox.warning(self, "警告", "请先从视频库中选择一个视频")
            return

        start_t = self.spin_start.value()
        end_t = self.spin_end.value()

        if end_t <= start_t:
            QMessageBox.warning(self, "警告", "结束时间必须大于开始时间")
            return

        fps = self.spin_fps.value()
        resize = self.spin_resize.value()
        roi = self.video_player.get_roi()
        output_dir = self.output_dir_edit.text().strip()

        if not output_dir:
            output_dir = str(BASE_DIR / "output")
        os.makedirs(output_dir, exist_ok=True)

        output_path = make_unique_gif_name(
            self.current_bv_id or "output", start_t, end_t, output_dir)

        self._log(f"\n{'=' * 50}")
        self._log(f"开始生成 GIF...")
        self._log(f"  视频: {os.path.basename(self.current_video_path)}")
        self._log(f"  输出: {os.path.basename(output_path)}")

        self.btn_generate.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)

        self._gif_worker = GifWorker(
            self.current_video_path, start_t, end_t,
            output_path, fps, resize, roi
        )
        self._gif_worker.log.connect(self._append_log)
        self._gif_worker.gif_finished.connect(self._on_gif_finished)
        self._gif_worker.error.connect(self._on_gif_error)
        self._gif_worker.start()

    def _on_gif_finished(self, path: str):
        self._log(f"✅ GIF 已保存到: {path}")
        self.btn_generate.setEnabled(True)
        self.progress_bar.setVisible(False)
        QMessageBox.information(
            self, "完成",
            f"GIF 已生成!\n\n{path}\n\n大小: {os.path.getsize(path) / 1024:.1f} KB"
        )

    def _on_gif_error(self, msg: str):
        self._log(f"❌ {msg}")
        self.btn_generate.setEnabled(True)
        self.progress_bar.setVisible(False)
        QMessageBox.critical(self, "生成失败", msg)


# ==============================================================================
# 辅助函数
# ==============================================================================

def make_unique_gif_name(bv_id: str, start_time: float, end_time: float,
                         output_dir: str) -> str:
    """生成不重名的 GIF 文件名"""
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    name = f"{bv_id}_{start_time:.1f}s-{end_time:.1f}s_{timestamp}.gif"
    return os.path.join(output_dir, name)


# ==============================================================================
# 程序入口
# ==============================================================================

def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_QSS)
    app.setApplicationName("Bilibili GIF Maker")

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
