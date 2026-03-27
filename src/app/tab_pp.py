"""
Adrenalift -- PP (PowerPlay) Tab
=================================

Full decoded PP tree view with per-field Set buttons.
"""

from __future__ import annotations

import os
import re

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QScrollArea,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.app.constants import DEFAULT_VBIOS_PATH, PP_DUMPS_DIR
from src.app.help_texts import PP_HELP_HTML
from src.app.ui_helpers import fmt_f32, make_spinbox, make_float_spinbox, make_set_button, make_cheatsheet_button
from src.io.vbios_parser import (
    VbiosValues,
    decode_pp_table_full,
    decode_pp_table_raw,
)
from src.io.vbios_storage import read_vbios_decoded
from src.engine.overclock_engine import patch_pp_single_field, read_raw_at_addr


class PPTab(QWidget):
    """PP tab — PowerPlay Table tree with per-field Set buttons."""

    def __init__(self, vbios_values: VbiosValues, *, log_fn, run_with_hardware_fn,
                 show_cheatsheet_fn, get_scan_result_fn):
        super().__init__()
        self._log = log_fn
        self._run_with_hardware = run_with_hardware_fn
        self._show_cheatsheet = show_cheatsheet_fn
        self._get_scan_result = get_scan_result_fn
        self.vbios_values = vbios_values

        self.param_widgets: dict[str, object] = {}
        self.param_current_value_widget: dict[str, QLabel] = {}
        self.param_smu_key: dict[str, str] = {}
        self.param_unit: dict[str, str] = {}
        self.pp_ram_offset_map: dict[str, dict] = {}
        self.pp_patch_keys: set[str] = set()
        self._item_to_path: dict[QTreeWidgetItem, str] = {}

        self._build_ui()

    def _build_ui(self):
        vb = self.vbios_values
        pp_tab_layout = QVBoxLayout(self)

        _, pp_hint_row = make_cheatsheet_button(
            self, "PP", PP_HELP_HTML, self._show_cheatsheet,
            tooltip="How PP Table RAM patching works",
            label="PP \u2014 PowerPlay Table",
        )
        pp_tab_layout.addLayout(pp_hint_row)

        body = QHBoxLayout()

        # -- Left column: search + table --
        left_col = QVBoxLayout()

        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Search PP fields\u2026")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.textChanged.connect(self._on_search_changed)
        left_col.addWidget(self._search_edit)

        pp_grp = QGroupBox("PP — PowerPlay Table")
        pp_tree = QTreeWidget()
        pp_tree.setColumnCount(5)
        pp_tree.setHeaderLabels(["Field", "VBIOS value", "Current value", "Custom input", ""])
        pp_tree.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        pp_tree.header().setStretchLastSection(True)
        self._pp_tree = pp_tree

        rom_bytes, _ = read_vbios_decoded(DEFAULT_VBIOS_PATH)
        decoded = decode_pp_table_full(rom_bytes, rom_path=DEFAULT_VBIOS_PATH) if rom_bytes else None
        del rom_bytes
        self._decoded = decoded
        decoded_tree = decoded.data if decoded else None

        _bc_pp_off = getattr(self.vbios_values, 'baseclock_pp_offset', 0)

        _SKIP_PAT = re.compile(
            r'^(Padding|Spare|Reserve|MmHubPadding|PADDING_)', re.IGNORECASE,
        )

        def _infer_group(path):
            if "DriverReportedClocks/" in path:
                return "clocks"
            if "MsgLimits/" in path:
                return "msglimits"
            segments = path.split("/")
            for seg in segments:
                if seg.startswith("FreqTable") or seg.startswith("Gfxclk"):
                    return "freq"
            if "CustomSkuTable/" in path:
                low = path.lower()
                if any(k in low for k in ("fan", "acoustic", "pwm", "zerorpm")):
                    return "fan"
                if any(k in low for k in ("temp", "ctf")):
                    return "temps"
                return "power"
            if "BoardTable/" in path:
                return "board"
            if any(k in path.lower() for k in ("voltage", "vmax", "vmin")):
                return "voltage"
            return "other"

        def _mk_pp_field_apply(full_path, spin):
            def _apply(hw):
                meta = self.pp_ram_offset_map.get(full_path)
                if not meta:
                    return {"ok": False, "msg": f"No offset for {full_path}"}
                scan_result = self._get_scan_result()
                return patch_pp_single_field(
                    hw["inpout"], scan_result,
                    meta["offset"], spin.value(), meta.get("type", "H"),
                )
            return _apply

        def _add_pp_leaf(parent_item, name, leaf, full_path):
            _QSPIN_MAX = (1 << 31) - 1
            raw_offset = int(leaf.get("offset", -1))
            field_type = str(leaf.get("type", "H"))
            group = _infer_group(full_path)

            if field_type == "f":
                vb_val = float(leaf.get("value", 0.0))
                widget = make_float_spinbox(vb_val)
                vb_display = fmt_f32(vb_val)
            elif field_type in ("I", "L"):
                vb_val = int(leaf.get("value", 0))
                vb_val = max(0, min(vb_val, 0xFFFFFFFF))
                widget = make_float_spinbox(
                    float(vb_val),
                    decimals=0, minimum=0, maximum=4294967295.0, step=1.0,
                    use_f32_format=False,
                )
                vb_display = str(vb_val)
            else:
                vb_val = int(leaf.get("value", 0))
                if field_type == "h":
                    min_val, max_val = -32768, 32767
                elif field_type == "b":
                    min_val, max_val = -128, 127
                elif field_type in ("i", "l"):
                    min_val, max_val = -_QSPIN_MAX, _QSPIN_MAX
                elif field_type in ("B",):
                    min_val, max_val = 0, 255
                elif field_type in ("H",):
                    min_val, max_val = 0, 65535
                elif field_type in ("Q", "q"):
                    min_val, max_val = 0, _QSPIN_MAX
                else:
                    min_val, max_val = 0, _QSPIN_MAX
                vb_val = max(min_val, min(vb_val, max_val))
                widget = make_spinbox(min_val, max_val, vb_val)
                vb_display = str(vb_val)

            item = QTreeWidgetItem(parent_item, [name, vb_display])
            cv_label = QLabel("---")
            pp_tree.setItemWidget(item, 2, cv_label)
            pp_tree.setItemWidget(item, 3, widget)

            self._item_to_path[item] = full_path
            self.param_current_value_widget[full_path] = cv_label
            self.param_unit[full_path] = ""
            self.param_widgets[full_path] = widget
            self.pp_patch_keys.add(full_path)
            if raw_offset >= 0:
                self.pp_ram_offset_map[full_path] = {
                    "offset": raw_offset - _bc_pp_off,
                    "type": field_type,
                    "group": group,
                }
                apply_fn = _mk_pp_field_apply(full_path, widget)
                btn = make_set_button(name, apply_fn, self._run_with_hardware, max_width=40)
                pp_tree.setItemWidget(item, 4, btn)

        def _populate_pp_tree(parent_item, node, path_prefix):
            if not isinstance(node, dict):
                return
            if "entries" in node and isinstance(node["entries"], list):
                for idx, child in enumerate(node["entries"]):
                    child_path = f"{path_prefix}/{idx}" if path_prefix else str(idx)
                    if isinstance(child, dict) and "value" in child and "offset" in child:
                        _add_pp_leaf(parent_item, str(idx), child, child_path)
                    else:
                        item = QTreeWidgetItem(parent_item, [str(idx)])
                        _populate_pp_tree(item, child, child_path)
                return
            for key, child in node.items():
                if _SKIP_PAT.match(str(key)):
                    continue
                child_path = f"{path_prefix}/{key}" if path_prefix else str(key)
                if isinstance(child, dict):
                    if "value" in child and "offset" in child:
                        _add_pp_leaf(parent_item, str(key), child, child_path)
                    else:
                        item = QTreeWidgetItem(parent_item, [str(key)])
                        _populate_pp_tree(item, child, child_path)
                elif isinstance(child, list):
                    item = QTreeWidgetItem(parent_item, [str(key)])
                    for idx, elem in enumerate(child):
                        elem_path = f"{child_path}/{idx}"
                        if isinstance(elem, dict) and "value" in elem and "offset" in elem:
                            _add_pp_leaf(item, str(idx), elem, elem_path)
                        elif isinstance(elem, dict):
                            sub = QTreeWidgetItem(item, [str(idx)])
                            _populate_pp_tree(sub, elem, elem_path)

        if decoded_tree is not None:
            _populate_pp_tree(pp_tree.invisibleRootItem(), decoded_tree, "")
            root = pp_tree.invisibleRootItem()
            for i in range(root.childCount()):
                top = root.child(i)
                top.setExpanded(True)
                for j in range(top.childCount()):
                    top.child(j).setExpanded(True)
        else:
            def _add_fallback_leaf(name, key, vb_val):
                sb = make_spinbox(0, 65535, int(vb_val) if vb_val and vb_val != "—" else 0, "")
                item = QTreeWidgetItem(pp_tree.invisibleRootItem(),
                                       [name, str(vb_val) if vb_val else "—"])
                cv_label = QLabel("---")
                pp_tree.setItemWidget(item, 2, cv_label)
                pp_tree.setItemWidget(item, 3, sb)
                self.param_current_value_widget[key] = cv_label
                self.param_unit[key] = ""
                self.param_widgets[key] = sb
                self.pp_patch_keys.add(key)

            _add_fallback_leaf("Game Clock", "GameClockAc", vb.gameclock_ac)
            _add_fallback_leaf("Boost Clock", "BoostClockAc", vb.boostclock_ac)
            _add_fallback_leaf("PPT AC", "PPT0_AC", vb.power_ac)
            _add_fallback_leaf("PPT DC", "PPT0_DC", vb.power_dc)
            _add_fallback_leaf("TDC GFX", "TDC_GFX", vb.tdc_gfx)
            _add_fallback_leaf("TDC SOC", "TDC_SOC", vb.tdc_soc)
            _add_fallback_leaf("Temp Edge", "Temp_Edge", vb.temp_edge or 100)
            _add_fallback_leaf("Temp Hotspot", "Temp_Hotspot", vb.temp_hotspot or 110)
            _add_fallback_leaf("Temp Mem", "Temp_Mem", vb.temp_mem or 100)
            _add_fallback_leaf("Temp VR GFX", "Temp_VR_GFX", vb.temp_vr_gfx or 115)
            _add_fallback_leaf("Temp VR SOC", "Temp_VR_SOC", vb.temp_vr_soc or 115)

        pp_layout = QVBoxLayout(pp_grp)
        pp_layout.addWidget(pp_tree)

        pp_scroll = QScrollArea()
        pp_scroll.setWidgetResizable(True)
        pp_scroll.setWidget(pp_grp)
        left_col.addWidget(pp_scroll)

        body.addLayout(left_col, stretch=1)

        # -- Right column: dump list + buttons --
        right_col = QVBoxLayout()

        self._dump_list = QListWidget()
        self._dump_list.setMaximumWidth(200)
        right_col.addWidget(self._dump_list, stretch=1)

        self._load_dump_btn = QPushButton("Load Dump")
        self._load_dump_btn.setToolTip("Load a saved PP dump into spinboxes (does not apply)")
        self._load_dump_btn.clicked.connect(self._on_load_dump)
        right_col.addWidget(self._load_dump_btn)

        self.pp_refresh_btn = QPushButton("Refresh")
        self.pp_refresh_btn.setToolTip("Read live values from RAM and SMU")
        self.pp_refresh_btn.setEnabled(True)
        right_col.addWidget(self.pp_refresh_btn)

        self.clocks_apply_btn = QPushButton("Apply PP")
        self.clocks_apply_btn.setToolTip("Patches all PP table fields in driver RAM (no SMU commands)")
        right_col.addWidget(self.clocks_apply_btn)

        self.pp_save_dump_btn = QPushButton("Save Dump")
        self.pp_save_dump_btn.setToolTip("Save live PP table from RAM to a .bin file")
        self.pp_save_dump_btn.setEnabled(False)
        self.pp_save_dump_btn.clicked.connect(self._on_save_dump)
        right_col.addWidget(self.pp_save_dump_btn)

        body.addLayout(right_col)

        pp_tab_layout.addLayout(body)

        # Zero the inner fingerprint region in pp_bytes so physical memory
        # scans don't match our own heap copy of the decoded PP table.
        if decoded is not None:
            _bc_off = getattr(self.vbios_values, 'baseclock_pp_offset', 0)
            _fp_to_clk = getattr(self.vbios_values, 'inner_fp_to_clocks', 0)
            if _bc_off > 0 and _fp_to_clk > 0:
                _fp_start = _bc_off - _fp_to_clk
                _fp_end = _fp_start + 16
                if 0 <= _fp_start < _fp_end <= len(decoded.pp_bytes):
                    decoded.pp_bytes[_fp_start:_fp_end] = b'\x00' * 16

        self._refresh_dump_list()

    # ------------------------------------------------------------------

    def _on_search_changed(self, text: str):
        text = text.strip().lower()

        def _set_visibility(item):
            child_count = item.childCount()
            if child_count == 0:
                if not text:
                    item.setHidden(False)
                    return True
                path = self._item_to_path.get(item, "")
                vbios_val = item.text(1)
                visible = text in path.lower() or text in vbios_val.lower()
                item.setHidden(not visible)
                return visible
            any_visible = False
            for i in range(child_count):
                if _set_visibility(item.child(i)):
                    any_visible = True
            if not text:
                item.setHidden(False)
                return True
            item.setHidden(not any_visible)
            return any_visible

        root = self._pp_tree.invisibleRootItem()
        for i in range(root.childCount()):
            _set_visibility(root.child(i))

    # ------------------------------------------------------------------

    def _refresh_dump_list(self):
        self._dump_list.clear()
        if not os.path.isdir(PP_DUMPS_DIR):
            return
        bins = sorted(f for f in os.listdir(PP_DUMPS_DIR) if f.endswith(".bin"))
        for name in bins:
            self._dump_list.addItem(name)

    # ------------------------------------------------------------------

    def _on_save_dump(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Save PP Dump")
        dlg_layout = QVBoxLayout(dlg)
        dlg_layout.addWidget(QLabel("Dump name:"))
        name_edit = QLineEdit()
        name_edit.setPlaceholderText("e.g. stock_1800mhz")
        dlg_layout.addWidget(name_edit)
        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
        )
        btn_box.accepted.connect(dlg.accept)
        btn_box.rejected.connect(dlg.reject)
        dlg_layout.addWidget(btn_box)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        name = name_edit.text().strip()
        if not name:
            self._log("Save Dump: empty name, cancelled.")
            return

        _INVALID = set('\\/:*?"<>|')
        if any(c in _INVALID for c in name):
            self._log(f"Save Dump: invalid characters in name '{name}'")
            return

        if self._decoded is None or not self._decoded.pp_bytes:
            self._log("Save Dump: no decoded PP table available.")
            return

        pp_size = len(self._decoded.pp_bytes)
        dest_path = os.path.join(PP_DUMPS_DIR, f"{name}.bin")

        def do_save(hw):
            scan_result = self._get_scan_result()
            if not scan_result or not scan_result.valid_addrs:
                return (False, "No valid scan addresses — run Scan first")
            bc_off = getattr(self.vbios_values, 'baseclock_pp_offset', 0)
            pp_start = scan_result.valid_addrs[0] - bc_off
            raw = read_raw_at_addr(hw["inpout"], pp_start, pp_size)
            if raw is None:
                return (False, "Failed to read PP table from RAM")
            os.makedirs(PP_DUMPS_DIR, exist_ok=True)
            with open(dest_path, "wb") as f:
                f.write(raw)
            self._log(f"Save Dump: wrote {len(raw)} bytes to {dest_path}")

        self._run_with_hardware("Save Dump", do_save)

    # ------------------------------------------------------------------

    def _on_load_dump(self):
        item = self._dump_list.currentItem()
        if item is None:
            self._log("Load Dump: no dump selected.")
            return
        filename = item.text()
        path = os.path.join(PP_DUMPS_DIR, filename)
        try:
            with open(path, "rb") as f:
                dump_bytes = f.read()
        except OSError as e:
            self._log(f"Load Dump: failed to read {path}: {e}")
            return

        decoded = decode_pp_table_raw(dump_bytes)
        if decoded is None:
            self._log("Load Dump: failed to decode dump file.")
            return

        updated = 0

        def _safe_set(widget, val):
            if isinstance(widget, QDoubleSpinBox):
                clamped = max(widget.minimum(), min(float(val), widget.maximum()))
            else:
                clamped = max(widget.minimum(), min(int(val), widget.maximum()))
            widget.setValue(clamped)

        def _walk_and_update(node, path_prefix):
            nonlocal updated
            if not isinstance(node, dict):
                return
            if "entries" in node and isinstance(node["entries"], list):
                for idx, child in enumerate(node["entries"]):
                    child_path = f"{path_prefix}/{idx}" if path_prefix else str(idx)
                    if isinstance(child, dict) and "value" in child and "offset" in child:
                        widget = self.param_widgets.get(child_path)
                        if widget and hasattr(widget, "setValue"):
                            _safe_set(widget, child["value"])
                            updated += 1
                    else:
                        _walk_and_update(child, child_path)
                return
            for key, child in node.items():
                child_path = f"{path_prefix}/{key}" if path_prefix else str(key)
                if isinstance(child, dict):
                    if "value" in child and "offset" in child:
                        widget = self.param_widgets.get(child_path)
                        if widget and hasattr(widget, "setValue"):
                            _safe_set(widget, child["value"])
                            updated += 1
                    else:
                        _walk_and_update(child, child_path)
                elif isinstance(child, list):
                    for idx, elem in enumerate(child):
                        elem_path = f"{child_path}/{idx}"
                        if isinstance(elem, dict) and "value" in elem and "offset" in elem:
                            widget = self.param_widgets.get(elem_path)
                            if widget and hasattr(widget, "setValue"):
                                _safe_set(widget, elem["value"])
                                updated += 1
                        elif isinstance(elem, dict):
                            _walk_and_update(elem, elem_path)

        dump_leaf_paths = []

        def _collect_paths(node, path_prefix):
            if not isinstance(node, dict):
                return
            if "entries" in node and isinstance(node["entries"], list):
                for idx, child in enumerate(node["entries"]):
                    child_path = f"{path_prefix}/{idx}" if path_prefix else str(idx)
                    if isinstance(child, dict) and "value" in child and "offset" in child:
                        dump_leaf_paths.append(child_path)
                    else:
                        _collect_paths(child, child_path)
                return
            for key, child in node.items():
                child_path = f"{path_prefix}/{key}" if path_prefix else str(key)
                if isinstance(child, dict):
                    if "value" in child and "offset" in child:
                        dump_leaf_paths.append(child_path)
                    else:
                        _collect_paths(child, child_path)
                elif isinstance(child, list):
                    for idx, elem in enumerate(child):
                        elem_path = f"{child_path}/{idx}"
                        if isinstance(elem, dict) and "value" in elem and "offset" in elem:
                            dump_leaf_paths.append(elem_path)
                        elif isinstance(elem, dict):
                            _collect_paths(elem, elem_path)

        _collect_paths(decoded.data, "")
        _walk_and_update(decoded.data, "")

        if updated == 0 and dump_leaf_paths:
            widget_keys = sorted(self.param_widgets.keys())[:3]
            dump_sample = dump_leaf_paths[:3]
            self._log(f"Load Dump: 0 matches! dump paths sample: {dump_sample}")
            self._log(f"  widget keys sample: {widget_keys}")
        self._log(f"Load Dump: updated {updated}/{len(dump_leaf_paths)} spinbox values "
                  f"from {filename} ({len(self.param_widgets)} widgets registered)")

    # ------------------------------------------------------------------

    def get_patch_values(self) -> dict:
        """Return user values for expanded PP patch fields (int or float)."""
        values: dict = {}
        for key in self.pp_patch_keys:
            widget = self.param_widgets.get(key)
            if widget is None or not hasattr(widget, "value"):
                continue
            values[key] = widget.value()
        return values

    def sync_spinboxes_from_ram(self, ram_data: dict) -> int:
        """Update Custom input spinboxes to match current RAM values."""
        if not isinstance(ram_data, dict):
            self._log("PP sync: ram_data is not a dict, skipping")
            return 0
        matched = 0
        synced = 0
        for key in self.pp_patch_keys:
            if key not in ram_data:
                continue
            matched += 1
            widget = self.param_widgets.get(key)
            if widget and hasattr(widget, "setValue"):
                val = ram_data[key]
                if val is not None:
                    if isinstance(widget, QDoubleSpinBox):
                        clamped = max(widget.minimum(), min(float(val), widget.maximum()))
                    else:
                        clamped = max(widget.minimum(), min(int(val), widget.maximum()))
                    widget.setValue(clamped)
                    synced += 1
        self._log(f"PP sync: {len(self.pp_patch_keys)} patch keys, "
                  f"{len(ram_data)} ram keys, {matched} matched, {synced} spinboxes set")
        return synced

    @property
    def decoded(self):
        return self._decoded
