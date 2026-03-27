"""UI helper factory functions for Adrenalift.

Centralises repeated widget-creation patterns from main.py into reusable
factory functions, reducing boilerplate across tabs.
"""

from __future__ import annotations

import struct

from PySide6.QtCore import Qt, QSize
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QStyle,
    QTableWidgetItem,
    QToolButton,
    QWidget,
)


def fmt_f32(val: float) -> str:
    """Shortest ``:.Ng`` representation that round-trips through IEEE 754 float32.

    Tries N = 6..9 significant digits and returns the first format whose
    ``float()`` parse re-packs to the same 4-byte pattern.  Falls back to
    ``:.9g`` which is always sufficient per IEEE 754.
    """
    orig = struct.pack("<f", val)
    for n in range(6, 10):
        s = f"{val:.{n}g}"
        if struct.pack("<f", float(s)) == orig:
            return s
    return f"{val:.9g}"


class Float32SpinBox(QDoubleSpinBox):
    """QDoubleSpinBox that displays values using :func:`fmt_f32`.

    Internally keeps ``decimals=9`` for full float32 precision, but
    ``textFromValue`` trims trailing noise so the user sees clean numbers
    (e.g. ``1.45`` instead of ``1.450000048``).
    """

    def textFromValue(self, value: float) -> str:  # noqa: N802
        txt = fmt_f32(value)
        sfx = self.suffix()
        return f"{txt}{sfx}" if sfx else txt

    def valueFromText(self, text: str) -> float:  # noqa: N802
        sfx = self.suffix()
        if sfx and text.endswith(sfx):
            text = text[: -len(sfx)]
        return float(text)


def make_spinbox(
    min_val: int,
    max_val: int,
    value: int,
    suffix: str = "",
    special_value_text: str | None = None,
) -> QSpinBox:
    """Create a pre-configured QSpinBox.

    *suffix* is applied verbatim (include a leading space if desired,
    e.g. ``" MHz"``).
    """
    w = QSpinBox()
    w.setRange(min_val, max_val)
    w.setValue(value)
    if suffix:
        w.setSuffix(suffix)
    if special_value_text is not None:
        w.setSpecialValueText(special_value_text)
    return w


def make_float_spinbox(
    value: float = 0.0,
    suffix: str = "",
    *,
    decimals: int = 9,
    minimum: float = -3.4e38,
    maximum: float = 3.4e38,
    step: float = 0.001,
    use_f32_format: bool = True,
) -> QDoubleSpinBox:
    """Create a QDoubleSpinBox for float or large-integer PP table fields.

    *decimals=9* guarantees lossless IEEE 754 float32 round-trips.
    For unsigned-u32 fields, use ``decimals=0, minimum=0, maximum=4294967295``.

    When *use_f32_format* is True (default for float fields), returns a
    :class:`Float32SpinBox` that formats display with :func:`fmt_f32`.
    """
    w = Float32SpinBox() if use_f32_format else QDoubleSpinBox()
    w.setDecimals(decimals)
    w.setRange(minimum, maximum)
    w.setSingleStep(step)
    w.setValue(value)
    if suffix:
        w.setSuffix(suffix)
    return w


def make_cheatsheet_button(
    parent: QWidget,
    title: str,
    html: str,
    show_fn,
    *,
    tooltip: str | None = None,
    label: str | None = None,
) -> tuple[QToolButton, QHBoxLayout]:
    """Create a ``(?)`` cheatsheet tool-button inside an ``QHBoxLayout``.

    Parameters
    ----------
    parent:
        Any visible QWidget; its ``.style()`` is used to fetch the
        question-mark icon.
    title:
        Dialog window title passed to *show_fn*.
    html:
        HTML body passed to *show_fn*.
    show_fn:
        ``Callable(title, html)`` that opens the cheatsheet dialog.
    tooltip:
        Button tooltip.  Defaults to ``"Open cheatsheet for {title}"``.
    label:
        Bold text placed next to the button.  *None* (default) uses
        *title*; pass ``""`` to suppress the label entirely.

    Returns
    -------
    ``(button, layout)`` -- caller should add *layout* to the parent layout.
    """
    btn = QToolButton()
    btn.setIcon(
        parent.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxQuestion)
    )
    btn.setIconSize(QSize(18, 18))
    btn.setToolTip(tooltip or f"Open cheatsheet for {title}")
    btn.setStyleSheet("QToolButton { border: none; background: transparent; }")
    btn.setCursor(Qt.CursorShape.WhatsThisCursor)
    _t, _h = title, html
    btn.clicked.connect(lambda checked=False, t=_t, h=_h: show_fn(t, h))

    row = QHBoxLayout()
    row.addWidget(btn)
    display_label = label if label is not None else title
    if display_label:
        row.addWidget(QLabel(f"<b>{display_label}</b>"))
    row.addStretch()
    return btn, row


def make_set_button(
    human: str,
    apply_fn,
    run_with_hardware,
    *,
    max_width: int = 50,
) -> QPushButton:
    """Create a *Set* push-button that delegates to *run_with_hardware*.

    Clicking the button calls
    ``run_with_hardware(f"Set {human}", apply_fn, require_scan=False)``.
    """
    btn = QPushButton("Set")
    btn.setMaximumWidth(max_width)
    _fn, _lbl = apply_fn, human
    btn.clicked.connect(
        lambda checked=False, fn=_fn, lbl=_lbl:
            run_with_hardware(f"Set {lbl}", fn, require_scan=False)
    )
    return btn


def make_current_value_label(text: str = "\u2014") -> QLabel:
    """Create a ``QLabel`` used to display the current live value of a parameter."""
    return QLabel(text)


def add_param_row(
    table,
    human: str,
    key: str,
    unit: str,
    widget,
    *,
    cv_col: int = 3,
    widget_col: int = 4,
    set_col: int = 5,
    extra_items: list[tuple[int, object]] | None = None,
    apply_fn=None,
    apply_label: str | None = None,
    run_with_hardware=None,
) -> dict:
    """Insert a parameter row into a ``QTableWidget`` and return metadata.

    Columns 0/1/2 are always *Human name*, *Table key*, *Unit*.
    The current-value label, input widget, and optional Set button are placed
    at *cv_col*, *widget_col*, *set_col* respectively.

    *extra_items* is a list of ``(column, QTableWidgetItem | QWidget)`` pairs
    for additional cells (e.g. OD "Allowed" column).

    Returns ``{"cv_label": QLabel, "unit_str": str, "set_btn": QPushButton | None}``.
    """
    row = table.rowCount()
    table.insertRow(row)
    table.setItem(row, 0, QTableWidgetItem(human))
    table.setItem(row, 1, QTableWidgetItem(key))
    table.setItem(row, 2, QTableWidgetItem(unit))

    if extra_items:
        for col, content in extra_items:
            if isinstance(content, QWidget):
                table.setCellWidget(row, col, content)
            else:
                table.setItem(row, col, content)

    cv_label = make_current_value_label()
    table.setCellWidget(row, cv_col, cv_label)
    table.setCellWidget(row, widget_col, widget)

    result: dict = {
        "cv_label": cv_label,
        "unit_str": f" {unit}" if unit else "",
        "set_btn": None,
    }

    if apply_fn is not None and run_with_hardware is not None:
        btn = make_set_button(apply_label or human, apply_fn, run_with_hardware)
        table.setCellWidget(row, set_col, btn)
        result["set_btn"] = btn

    return result
