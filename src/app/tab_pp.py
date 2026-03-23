"""
Adrenalift -- PP (PowerPlay) Tab
=================================

Full decoded PP tree view with per-field Set buttons.
"""

from __future__ import annotations

import re

from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.app.constants import DEFAULT_VBIOS_PATH
from src.app.help_texts import PP_HELP_HTML
from src.app.ui_helpers import make_spinbox, make_set_button, make_cheatsheet_button
from src.io.vbios_parser import (
    VbiosValues,
    decode_pp_table_full,
)
from src.io.vbios_storage import read_vbios_decoded
from src.engine.overclock_engine import patch_pp_single_field


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
        _PP_SMU_KEY_MAP = {
            "smc_pptable/SkuTable/DriverReportedClocks/GameClockAc": "gfxclk",
            "smc_pptable/SkuTable/MsgLimits/Power/0/0": "ppt",
            "smc_pptable/SkuTable/MsgLimits/Temperature/0": "temp",
        }

        def _infer_unit(field_name):
            n = field_name.lower()
            if any(p in n for p in ("clock", "freq", "fmin", "fmax", "clk")):
                return "MHz"
            if any(p in n for p in ("power", "ppt", "socketpowerlimit")):
                return "W"
            if any(p in n for p in ("tdc", "edclimit")):
                return "A"
            if any(p in n for p in ("temp", "ctflimit")):
                return "°C"
            if any(p in n for p in ("voltage", "vmax", "vmin")):
                return "mV"
            if "rpm" in n:
                return "RPM"
            return ""

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
            vb_val = int(leaf.get("value", 0))
            raw_offset = int(leaf.get("offset", -1))
            field_type = str(leaf.get("type", "H"))
            unit = _infer_unit(name)
            group = _infer_group(full_path)
            smu_key = _PP_SMU_KEY_MAP.get(full_path)

            if field_type in ("Q", "q"):
                max_val = _QSPIN_MAX
            elif field_type in ("I", "L", "i", "l"):
                max_val = min(2_000_000_000, _QSPIN_MAX)
            elif field_type in ("B", "b"):
                max_val = 255
            elif field_type in ("H", "h"):
                max_val = 65535
            else:
                max_val = _QSPIN_MAX
            vb_val = max(0, min(vb_val, max_val))
            widget = make_spinbox(0, max_val, vb_val, f" {unit}" if unit else "")

            item = QTreeWidgetItem(parent_item, [name, str(vb_val)])
            cv_label = QLabel("---")
            pp_tree.setItemWidget(item, 2, cv_label)
            pp_tree.setItemWidget(item, 3, widget)

            self.param_current_value_widget[full_path] = cv_label
            self.param_unit[full_path] = f" {unit}" if unit else ""
            self.param_widgets[full_path] = widget
            self.pp_patch_keys.add(full_path)
            if smu_key:
                self.param_smu_key[full_path] = smu_key
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
            def _add_fallback_leaf(name, key, unit, vb_val, smu_key=None):
                sb = make_spinbox(0, 65535, int(vb_val) if vb_val and vb_val != "—" else 0,
                                  f" {unit}" if unit else "")
                item = QTreeWidgetItem(pp_tree.invisibleRootItem(),
                                       [name, str(vb_val) if vb_val else "—"])
                cv_label = QLabel("---")
                pp_tree.setItemWidget(item, 2, cv_label)
                pp_tree.setItemWidget(item, 3, sb)
                self.param_current_value_widget[key] = cv_label
                self.param_unit[key] = f" {unit}" if unit else ""
                self.param_widgets[key] = sb
                self.pp_patch_keys.add(key)
                if smu_key:
                    self.param_smu_key[key] = smu_key

            _add_fallback_leaf("Game Clock", "GameClockAc", "MHz", vb.gameclock_ac, "gfxclk")
            _add_fallback_leaf("Boost Clock", "BoostClockAc", "MHz", vb.boostclock_ac)
            _add_fallback_leaf("PPT AC", "PPT0_AC", "W", vb.power_ac, "ppt")
            _add_fallback_leaf("PPT DC", "PPT0_DC", "W", vb.power_dc)
            _add_fallback_leaf("TDC GFX", "TDC_GFX", "A", vb.tdc_gfx)
            _add_fallback_leaf("TDC SOC", "TDC_SOC", "A", vb.tdc_soc)
            _add_fallback_leaf("Temp Edge", "Temp_Edge", "°C", vb.temp_edge or 100, "temp")
            _add_fallback_leaf("Temp Hotspot", "Temp_Hotspot", "°C", vb.temp_hotspot or 110)
            _add_fallback_leaf("Temp Mem", "Temp_Mem", "°C", vb.temp_mem or 100)
            _add_fallback_leaf("Temp VR GFX", "Temp_VR_GFX", "°C", vb.temp_vr_gfx or 115)
            _add_fallback_leaf("Temp VR SOC", "Temp_VR_SOC", "°C", vb.temp_vr_soc or 115)

        pp_layout = QVBoxLayout(pp_grp)
        pp_layout.addWidget(pp_tree)
        pp_btn_row = QHBoxLayout()
        self.pp_refresh_btn = QPushButton("Refresh")
        self.pp_refresh_btn.setToolTip("Read live values from RAM and SMU")
        self.pp_refresh_btn.setEnabled(True)
        pp_btn_row.addWidget(self.pp_refresh_btn)
        self.clocks_apply_btn = QPushButton("Apply PP")
        self.clocks_apply_btn.setToolTip("Patches all PP table fields in driver RAM (no SMU commands)")
        pp_btn_row.addWidget(self.clocks_apply_btn)
        pp_layout.addLayout(pp_btn_row)

        pp_scroll = QScrollArea()
        pp_scroll.setWidgetResizable(True)
        pp_scroll.setWidget(pp_grp)
        pp_tab_layout.addWidget(pp_scroll)

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

    # ------------------------------------------------------------------

    def get_patch_values(self) -> dict[str, int]:
        """Return user values for expanded PP patch fields."""
        values: dict[str, int] = {}
        for key in self.pp_patch_keys:
            widget = self.param_widgets.get(key)
            if widget is None or not hasattr(widget, "value"):
                continue
            values[key] = int(widget.value())
        return values

    @property
    def decoded(self):
        return self._decoded
