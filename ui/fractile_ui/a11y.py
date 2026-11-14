"""Accessibility helpers (UX-06).

A screen reader announces a control by its accessible name. Qt derives one
from a button's text or a form label's buddy, but not for a control inside a
composite form field (a number with a unit box), a list, a table, a text
box or a custom widget. Those get a name here.
"""
from __future__ import annotations

from PyQt6.QtWidgets import (QAbstractButton, QAbstractItemView, QAbstractSlider, QAbstractSpinBox, QComboBox,
                             QFormLayout, QLabel, QLineEdit, QPlainTextEdit, QTabWidget, QTextEdit, QWidget)

CONTROLS = (QAbstractButton, QLineEdit, QComboBox, QAbstractSpinBox, QAbstractSlider, QAbstractItemView,
            QPlainTextEdit, QTextEdit)


def name(widget: QWidget, text: str, description: str | None = None) -> QWidget:
    widget.setAccessibleName(text)
    if description:
        widget.setAccessibleDescription(description)
    return widget


def _label_text(label: QWidget | None) -> str:
    if not isinstance(label, QLabel):
        return ""
    return label.text().replace("&&", "\0").replace("&", "").replace("\0", "&").strip()


def name_form_fields(root: QWidget) -> None:
    """Name the controls inside composite form fields after the row's label:
    the first control gets the label, a unit box after it "<label> unit"."""
    for form in root.findChildren(QFormLayout):
        for row in range(form.rowCount()):
            li = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            fi = form.itemAt(row, QFormLayout.ItemRole.FieldRole)
            text = _label_text(li.widget() if li else None)
            field = fi.widget() if fi else None
            if not text or field is None:
                continue
            if isinstance(field, CONTROLS):
                if not field.accessibleName():
                    field.setAccessibleName(text)
                continue
            inner = [w for w in field.findChildren(QWidget) if isinstance(w, CONTROLS) and not isinstance(w.parent(), (QComboBox, QAbstractSpinBox))]
            for i, w in enumerate(inner):
                if w.accessibleName():
                    continue
                if i == 0:
                    w.setAccessibleName(text)
                elif isinstance(w, QComboBox):
                    w.setAccessibleName(f"{text} unit")
                elif not (isinstance(w, QAbstractButton) and w.text()):
                    w.setAccessibleName(f"{text} {i + 1}")


def name_tab_widgets(root: QWidget, what: str) -> None:
    """A tab widget is a container a screen reader can land on; name it."""
    for t in root.findChildren(QTabWidget):
        if not t.accessibleName():
            t.setAccessibleName(f"{what} sections")
