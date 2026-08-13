"""统计页：各任务当日次数的平滑折线图（纯 QPainter 自绘，无第三方图表库）。

样式参考 qq-farm-copilot 的 steal_chart_panel：Catmull-Rom 样条转三次贝塞尔
（曲线精确穿过数据点）、虚线网格、悬停显示明细、滚轮缩放天数窗口。
数据来源：runs/*_progress.json 的 history 字段（{日期: 次数}），按天补齐 0。
"""
from __future__ import annotations

from datetime import date, timedelta

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from .progress import (
    ADVENTURE_PROGRESS_FILE,
    PK_PROGRESS_FILE,
    SCHOOL_PROGRESS_FILE,
    VISIT_PROGRESS_FILE,
    WORK_PROGRESS_FILE,
    load_progress,
)

# 统计的任务：(名称, 进度文件, 曲线颜色)
TASKS = [
    ('学习', SCHOOL_PROGRESS_FILE, '#E5A50A'),
    ('打工', WORK_PROGRESS_FILE, '#E2703A'),
    ('冒险', ADVENTURE_PROGRESS_FILE, '#06A77D'),
    ('踩踩', VISIT_PROGRESS_FILE, '#118AB2'),
    ('PK', PK_PROGRESS_FILE, '#EF476F'),
]

DEFAULT_DAYS = 15  # 默认显示最近 15 天
MIN_DAYS = 7
MAX_DAYS = 90

# 图表留白：左（Y 轴刻度）/上/右/下（X 轴日期）
_PAD_L, _PAD_T, _PAD_R, _PAD_B = 40, 16, 16, 32
_GRID_ROWS = 5
_TEXT_COLOR = '#1e293b'
_GRID_COLOR = '#e2e8f0'


def load_daily_counts(days: int) -> list[tuple[str, list[int]]]:
    """读各任务进度文件的 history，返回 [(日期, [各任务次数])]，最近 days 天、缺天补 0。"""
    histories = []
    for _, progress_file, _ in TASKS:
        _, _, history = load_progress(progress_file, quiet=True)
        histories.append(history)
    today = date.today()
    out = []
    for i in range(days):
        day = (today - timedelta(days=days - 1 - i)).isoformat()
        out.append((day, [h.get(day, 0) for h in histories]))
    return out


class LineChart(QWidget):
    """多系列平滑折线图：数据点圆点 + Catmull-Rom 平滑曲线，悬停显示明细。"""

    def __init__(self, series: list[tuple[str, str]], on_wheel=None, parent=None):
        super().__init__(parent)
        self._series = series  # [(名称, 颜色)]
        self._data: list[tuple[str, list[int]]] = []
        self._hover_idx = -1
        self._on_wheel = on_wheel  # 滚轮缩放天数窗口的回调（StatsPanel 提供）
        self.setMouseTracking(True)
        self.setFixedHeight(220)  # 固定高度：统计页内容置顶，图表不拉满整页

    def set_data(self, data: list[tuple[str, list[int]]]) -> None:
        self._data = data
        self._hover_idx = -1
        self.update()

    # ---- 绘制 ----

    @staticmethod
    def _build_smooth_path(points: list[QPointF]) -> QPainterPath:
        """Catmull-Rom 样条转三次贝塞尔（张力 1/6，曲线精确穿过数据点）。"""
        path = QPainterPath()
        path.moveTo(points[0])
        n = len(points)
        if n == 2:
            path.lineTo(points[1])
            return path
        for i in range(n - 1):
            p0 = points[max(0, i - 1)]
            p1 = points[i]
            p2 = points[i + 1]
            p3 = points[min(n - 1, i + 2)]
            t = 1.0 / 6.0
            cp1 = QPointF(p1.x() + (p2.x() - p0.x()) * t,
                          p1.y() + (p2.y() - p0.y()) * t)
            cp2 = QPointF(p2.x() - (p3.x() - p1.x()) * t,
                          p2.y() - (p3.y() - p1.y()) * t)
            path.cubicTo(cp1, cp2, p2)
        return path

    def _points(self) -> list[list[QPointF]]:
        """各系列的像素坐标点。"""
        n = len(self._data)
        w = self.width() - _PAD_L - _PAD_R
        h = self.height() - _PAD_T - _PAD_B
        max_val = max((v for _, values in self._data for v in values), default=0) or 1
        points = [[] for _ in self._series]
        for i, (_, values) in enumerate(self._data):
            x = _PAD_L + i * w / n + w / n / 2
            for sidx, value in enumerate(values):
                y = _PAD_T + h - (value / max_val * h)
                points[sidx].append(QPointF(x, y))
        return points

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width() - _PAD_L - _PAD_R
        h = self.height() - _PAD_T - _PAD_B
        if not self._data:
            p.setPen(QColor(_TEXT_COLOR))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, '暂无数据')
            return
        max_val = max((v for _, values in self._data for v in values), default=0) or 1
        n = len(self._data)

        # 虚线网格 + Y 轴刻度
        grid_pen = QPen(QColor(_GRID_COLOR), 1, Qt.PenStyle.DashLine)
        for row in range(_GRID_ROWS + 1):
            y = _PAD_T + h - row * h / _GRID_ROWS
            p.setPen(grid_pen)
            p.drawLine(int(_PAD_L), int(y), int(_PAD_L + w), int(y))
            p.setPen(QColor(_TEXT_COLOR))
            label = str(round(max_val * row / _GRID_ROWS))
            p.drawText(0, int(y) - 8, _PAD_L - 6, 16,
                       Qt.AlignmentFlag.AlignRight, label)

        # X 轴日期（MM-DD，抽样显示）
        step = max(1, n // 6)
        p.setPen(QColor(_TEXT_COLOR))
        for i, (day, _) in enumerate(self._data):
            if i % step and i != n - 1:
                continue
            x = _PAD_L + i * w / n + w / n / 2
            p.drawText(int(x) - 30, _PAD_T + h + 6, 60, 20,
                       Qt.AlignmentFlag.AlignHCenter, day[5:])

        # 平滑折线 + 数据点
        colors = [QColor(c) for _, c in self._series]
        for sidx, points in enumerate(self._points()):
            if len(points) < 2:
                continue
            pen = QPen(colors[sidx], 2.5)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(self._build_smooth_path(points))
            for i, pt in enumerate(points):
                r = 5.5 if i == self._hover_idx else 3.5
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(colors[sidx])
                p.drawEllipse(pt, r, r)

    # ---- 交互 ----

    def _index_at(self, x: float) -> int:
        n = len(self._data)
        if not n:
            return -1
        w = self.width() - _PAD_L - _PAD_R
        idx = round((x - _PAD_L - w / n / 2) / (w / n))
        return idx if 0 <= idx < n else -1

    def mouseMoveEvent(self, event) -> None:
        idx = self._index_at(event.position().x())
        if idx != self._hover_idx:
            self._hover_idx = idx
            self.update()
        if idx >= 0:
            day, values = self._data[idx]
            lines = [day] + [
                f'{name}: {values[sidx]}' for sidx, (name, _) in enumerate(self._series)
            ]
            QToolTip.showText(event.globalPosition().toPoint(), '\n'.join(lines), self)
        else:
            QToolTip.hideText()

    def leaveEvent(self, event) -> None:
        self._hover_idx = -1
        QToolTip.hideText()
        self.update()

    def wheelEvent(self, event) -> None:
        if self._on_wheel:
            self._on_wheel(event.angleDelta().y())


class StatsPanel(QWidget):
    """统计页：标题 + 图例 + 折线图；滚轮缩放天数窗口，显示时自动刷新。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._days = DEFAULT_DAYS
        layout = QVBoxLayout(self)

        title_row = QHBoxLayout()
        self._title = QLabel(f'任务统计（最近 {self._days} 天，滚轮缩放）')
        self._title.setStyleSheet('font-weight: bold; font-size: 14px;')
        title_row.addWidget(self._title)
        title_row.addStretch()
        for name, _, color in TASKS:  # 图例：色点 + 名称
            dot = QLabel('●')
            dot.setStyleSheet(f'color: {color};')
            title_row.addWidget(dot)
            title_row.addWidget(QLabel(name))
            title_row.addSpacing(10)
        layout.addLayout(title_row)

        self._chart = LineChart([(name, color) for name, _, color in TASKS],
                                on_wheel=self._adjust_days)
        layout.addWidget(self._chart)
        layout.addStretch()  # 内容置顶，多余空间留在底部

    def _adjust_days(self, delta_y: int) -> None:
        self._days = max(MIN_DAYS, min(MAX_DAYS,
                                       self._days + (-1 if delta_y > 0 else 1)))
        self.refresh()

    def refresh(self) -> None:
        self._title.setText(f'任务统计（最近 {self._days} 天，滚轮缩放）')
        self._chart.set_data(load_daily_counts(self._days))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh()
