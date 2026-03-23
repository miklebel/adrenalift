"""
HTML help / cheatsheet strings for Adrenalift.

Extracted from main.py to keep the UI module focused on layout and logic.
"""

from __future__ import annotations

SIMPLE_HOW_IT_WORKS_HTML = """
<h3>How Simple Apply Works</h3>

<h4>The Problem</h4>
<p>The AMD GPU driver keeps its own copy of the <b>PowerPlay (PP) table</b>
in system RAM. This table contains the clock limits that the driver considers
"allowed". Every time the driver re-evaluates power management &mdash;
on workload changes, thermal events, display switches &mdash; it reads these
limits and sends them to the GPU firmware (SMU). If you set clocks directly
via SMU commands, the driver will eventually overwrite them with whatever is
in its cached table.</p>

<h4>What Apply Does</h4>
<p>Apply performs three steps in order:</p>

<ol>
  <li><b>Patch the PP table in RAM.</b> During the earlier Scan step, this tool
      found the physical memory addresses where the driver keeps its cached PP
      table copies. Apply now overwrites the <b>GameClockAc</b> and
      <b>BoostClockAc</b> fields at those addresses with your chosen clock value.
      From this point on, when the driver reads its own table, it sees your value
      instead of the stock one.</li>

  <li><b>DisallowGfxOff.</b> Sends a message to the SMU telling it not to put
      the GPU into the GfxOff sleep state. This keeps the GPU awake so the new
      clock values can take effect immediately.</li>

  <li><b>Workload mask cycle.</b> Switches the SMU workload profile briefly
      (compute &rarr; 3D fullscreen), then back. This forces the driver to
      re-read the PP table and re-derive its clock limits. Because we already
      patched the table, the driver now sends <i>your</i> clock values to the
      SMU as if they were the original ones.</li>
</ol>

<h4>Why This Works</h4>
<p>The driver trusts its own cached PP table unconditionally. By changing the
values in that table before the driver reads them, we make the driver enforce
our overclock on our behalf. The workload mask cycle is the trigger that makes
the driver re-read the table and push the new limits to the SMU right away,
rather than waiting for a natural re-evaluation event.</p>

<p><b>No direct frequency commands</b> (SetSoftMax, SetHardMax, etc.) are sent
in this mode &mdash; the driver itself handles all SMU frequency programming
using the patched table values.</p>
"""

PP_HELP_HTML = """
<h3>PP &mdash; PowerPlay Table (Full Field Editor)</h3>
<p>The PP tab exposes <b>every field</b> in the GPU's PowerPlay (PP) table as an
editable tree. The PP table is a binary structure baked into the VBIOS ROM that
contains all of the driver's clock limits, power limits, temperature limits,
voltage curves, fan curves, and feature flags. At driver load time, the AMD
driver copies this table from the VBIOS into <b>system RAM</b> and consults it
whenever it needs to re-evaluate power management. This tab lets you view and
overwrite any field in that RAM copy.</p>

<h4>How the tree is built</h4>
<p>On startup the tool reads the VBIOS ROM and decodes the PP table using the
UPP library. Every decoded field is shown in a hierarchical tree matching the
struct layout: <code>smc_pptable &rarr; SkuTable</code>,
<code>CustomSkuTable</code>, <code>BoardTable</code>, etc. If UPP is
unavailable or the ROM cannot be decoded, a <b>fallback flat list</b> with the
most important fields (GameClock, BoostClock, PPT, TDC, temperatures) is shown
instead.</p>

<h4>Tree columns</h4>
<ul>
  <li><b>Field</b> &mdash; Struct field name from the PP table layout
      (e.g. <code>GameClockAc</code>, <code>SocketPowerLimitAc</code>).</li>
  <li><b>VBIOS value</b> &mdash; The stock value read from the VBIOS ROM.
      This never changes and serves as your reference for the factory setting.</li>
  <li><b>Current value</b> &mdash; The value currently in the driver's RAM copy.
      Updated when you click <b>Refresh</b>. Starts as &ldquo;---&rdquo; until
      the first refresh completes. For a few key fields (GameClockAc, PPT,
      Temperature) this column can also show live SMU telemetry.</li>
  <li><b>Custom input</b> &mdash; Editable spinbox where you enter your desired
      value. Initialised to the VBIOS value. The spinbox range is derived from
      the field's data type (uint8 &rarr; 0&ndash;255, uint16 &rarr;
      0&ndash;65535, uint32 &rarr; 0&ndash;2 billion). Units (MHz, W, A,
      &deg;C, mV, RPM) are inferred from the field name.</li>
  <li><b>Set</b> &mdash; Per-field apply button. Patches <i>only this one
      field</i> across all valid PP table addresses in RAM. Uses the correct
      write width (1, 2, or 4 bytes) based on the field's struct type.</li>
</ul>

<h4>Key field groups</h4>
<ul>
  <li><b>DriverReportedClocks</b> &mdash; <code>GameClockAc</code> and
      <code>BoostClockAc</code>. These are the primary clock limits the driver
      reports to the SMU. Patching them is the core of the Simple Apply
      overclock.</li>
  <li><b>MsgLimits &rarr; Power</b> &mdash; <code>SocketPowerLimitAc/Dc</code>
      (PPT limits in watts). Controls how much total board power the driver
      allows before asking the SMU to throttle.</li>
  <li><b>MsgLimits &rarr; Temperature</b> &mdash; Per-sensor thermal limits
      (Edge, Hotspot, Memory, VR GFX, VR SOC). Lowering these makes the driver
      throttle sooner; raising them gives more thermal headroom.</li>
  <li><b>MsgLimits &rarr; Current</b> &mdash; TDC (Thermal Design Current)
      limits in amps for GFX and SOC rails.</li>
  <li><b>FreqTableGfxclk / FreqTableUclk / FreqTableFclk</b> &mdash; DPM
      frequency tables. Each entry is a clock speed for one DPM level.</li>
  <li><b>CustomSkuTable</b> &mdash; Fan curves, acoustic limits, zero-RPM
      settings, advanced voltage/clock overrides.</li>
  <li><b>BoardTable</b> &mdash; Board-level power delivery parameters,
      VRM current limits, voltage regulator thermal limits.</li>
</ul>

<h4>Apply PP (bulk write)</h4>
<p>Clicking <b>Apply PP</b> reads every spinbox value in the tree and writes all
of them to the driver's RAM copy at every scanned PP table address. This is a
<b>RAM-only operation</b> &mdash; no SMU messages are sent. The write uses each
field's decoded byte offset and data type, so 8-bit, 16-bit, and 32-bit fields
are all patched correctly. Each write is verified by reading back the patched
value.</p>

<h4>Per-field Set (single write)</h4>
<p>Each row's <b>Set</b> button patches just that one field. Useful for testing
a single change without touching anything else. The button only appears for
fields that have a known RAM offset.</p>

<h4>Refresh</h4>
<p>Clicking <b>Refresh</b> triggers a background worker that:</p>
<ol>
  <li>Reads the PP table bytes from the first valid physical address and
      extracts every field value using the offset map (updates the
      <b>Current value</b> column).</li>
  <li>Reads the OD table from the SMU (updates OD-linked fields).</li>
  <li>Reads SMU metrics (updates live clock, PPT, temperature for key
      fields).</li>
  <li>Queries full SMU state (DPM frequencies, voltage, features).</li>
</ol>

<h4>Prerequisite: Scan</h4>
<p>Before any PP patching can work, the main Scan must have found at least one
valid PP table address in physical memory. If no addresses were found, Apply PP
will report &ldquo;no valid addresses to patch&rdquo;. Per-field Set buttons
still appear but will return an error. The scan searches system RAM for byte
patterns matching the VBIOS PP table fingerprint and validates candidates
against known structure offsets.</p>

<h4>PP vs. OD vs. Simple</h4>
<table border="1" cellpadding="4" cellspacing="0">
  <tr><th></th><th>PP Tab</th><th>OD Tab</th><th>Simple Tab</th></tr>
  <tr><td>Mechanism</td><td>Direct RAM overwrite</td>
      <td>SMU table transfer</td><td>RAM overwrite + SMU workload cycle</td></tr>
  <tr><td>Scope</td><td>Any PP table field</td>
      <td>OD parameters only</td><td>Clock + PPT + temps</td></tr>
  <tr><td>SMU involved?</td><td>No</td><td>Yes</td><td>Yes (workload mask)</td></tr>
  <tr><td>Admin required?</td><td>Yes (physical memory)</td>
      <td>Yes (MMIO)</td><td>Yes</td></tr>
  <tr><td>Takes effect</td><td>Next driver re-evaluation</td>
      <td>Immediately</td><td>Immediately (forced re-eval)</td></tr>
</table>

<h4>Why change fields beyond clocks?</h4>
<ul>
  <li><b>Raise PPT</b> to give the GPU more power budget before throttling.</li>
  <li><b>Raise temperature limits</b> to prevent premature thermal throttling
      (monitor temps!).</li>
  <li><b>Raise TDC</b> to allow higher sustained current draw.</li>
  <li><b>Edit fan curves</b> so the driver's built-in fan controller uses more
      aggressive cooling.</li>
  <li><b>Modify DPM frequency tables</b> to change the clock steps the driver
      programs into the SMU.</li>
  <li><b>Flip feature bits</b> in the PP header to unlock OD features the VBIOS
      disables.</li>
</ul>

<h4>Caveats</h4>
<ul>
  <li>All changes are <b>volatile</b> &mdash; lost on reboot, driver reload, or
      GPU reset.</li>
  <li>PP patching modifies the driver's <i>cached copy</i> in RAM, not the
      VBIOS ROM. The original VBIOS is never touched.</li>
  <li>Changes do not take effect instantly. The driver must <b>re-read</b> the
      patched table (via a workload transition, display mode switch, or thermal
      event) before the new values reach the SMU. Use the Simple tab's workload
      cycle or OD apply to force an immediate re-evaluation.</li>
  <li>Padding, Spare, and Reserve fields are automatically hidden from the tree
      to reduce clutter.</li>
  <li>If the VBIOS cannot be decoded (no UPP library, unsupported GPU
      generation), the fallback flat list only covers clocks, PPT, TDC, and
      temperatures. Full-tree mode requires the UPP library and a supported
      RDNA3/4 PP table layout.</li>
  <li>Per-field Set buttons only appear for fields with a known byte offset.
      Computed or virtual fields (like smu_key-mapped fields) cannot be patched
      individually.</li>
  <li>Setting values outside the hardware's safe operating range can cause
      instability, crashes, or thermal damage. Always verify changes with the
      Metrics tab.</li>
  <li>Multiple RAM copies of the PP table may exist (one per driver context).
      All copies found by Scan are patched simultaneously.</li>
</ul>
"""

OD_HELP_HTML = """
<h3>OD &mdash; OverDrive Table</h3>
<p>The OD tab sends the <b>OverDrive settings table</b> to the GPU's System
Management Unit (SMU) via the SMU table-transfer mailbox. Unlike PP patching
(which writes to the driver's RAM copy of the PowerPlay table), OD talks
directly to the SMU firmware.</p>

<h4>What you can change</h4>
<ul>
  <li><b>Gfxclk Offset</b> &mdash; Positive MHz offset added to the graphics
      clock. The SMU adds this on top of whatever DPM level it selects.</li>
  <li><b>PPT %</b> / <b>TDC %</b> &mdash; Package Power Tracking and Thermal
      Design Current percentage offsets. +10 means &ldquo;allow 10&percnt; more
      than stock&rdquo;.</li>
  <li><b>UCLK / FCLK min&thinsp;/&thinsp;max</b> &mdash; Memory controller and
      data-fabric clock boundaries.</li>
  <li><b>V/F Zone offsets</b> &mdash; Per-zone voltage offsets on the GFX
      voltage-frequency curve (6 points).</li>
  <li><b>VddGfx / VddSoc Vmax</b> &mdash; Maximum voltage caps for the GFX
      core and SoC rails.</li>
  <li><b>Fan controls</b> &mdash; Target temperature, min PWM, fan-curve
      points, acoustic limits, zero-RPM enable.</li>
  <li><b>EDC / PCC</b> &mdash; Electrical Design Current and PCC limit
      controls.</li>
  <li><b>Full Ctrl fields</b> &mdash; Advanced voltage, clock, and power-saving
      overrides (requires AdvancedOdModeEnabled&thinsp;=&thinsp;1).</li>
</ul>

<h4>How &ldquo;Apply OD&rdquo; works</h4>
<p>Clicking <b>Apply OD</b> calls <code>apply_od_table_only</code>, which
reads the current OD table from the SMU, merges your edits into it, and writes
the modified table back. Each per-row <b>Set</b> button calls
<code>apply_od_single_field</code> to change one field at a time.</p>

<h4>Volatility</h4>
<p>All OD changes are <b>volatile</b> &mdash; they are lost on reboot, driver
reload, or GPU reset. To persist settings across reboots, re-apply them after
each startup (or use the Simple Settings tab with the watchdog).</p>

<h4>Allowed column</h4>
<p>The <i>Allowed</i> column shows whether the VBIOS OD limits permit changing
that feature. &ldquo;No&rdquo; means the SMU will likely reject the change
unless you first patch the PP table to unlock the corresponding OD feature
bit.</p>
"""

STATUS_CHEATSHEET = """
<h3>Status — Read-only SMU dashboard</h3>
<p>This tab shows the <b>live state</b> reported by the GPU's System Management Unit (SMU).
Nothing here is writable — it's a monitoring-only view. Hit <b>Refresh</b> to re-read
all values from hardware.</p>

<h4>What each row means</h4>
<ul>
  <li><b>SMU Firmware Version</b> — Version string of the firmware running on the SMU
      micro-controller inside the GPU die.</li>
  <li><b>Driver IF Version</b> — The interface contract version the SMU firmware expects
      from the host-side driver. Mismatched versions may cause commands to be rejected.</li>
  <li><b>GFX Voltage (SVI3)</b> — Real-time GPU core voltage (millivolts) as reported by
      the SVI3 serial voltage-identification bus between the SMU and the voltage
      regulator.</li>
  <li><b>Running Features (hex)</b> — Raw 64-bit bitmask of all currently enabled SMU
      features. Each bit corresponds to a feature listed in the <i>Features</i> sub-tab.</li>
  <li><b>Current PPT Limit</b> — Package Power Tracking limit in watts. This is the maximum
      total board power the SMU will allow before it starts throttling.</li>
  <li><b>Per-clock Min / Max</b> — The current DPM frequency boundaries for each of the 11
      clock domains (GFXCLK, SOCCLK, UCLK, etc.). "Min" is the floor, "Max" is the
      ceiling the SMU is enforcing right now.</li>
  <li><b>DC Max</b> — Maximum allowed frequency for each clock when the system is running
      on battery (DC) power rather than wall power (AC).</li>
</ul>

<h4>All clock domains shown</h4>
<p>Three rows appear for each of the 11 PPCLK domains (Min / Max / DC Max):</p>
<ul>
  <li><b>GFXCLK</b> — Graphics/shader engine core clock.</li>
  <li><b>SOCCLK</b> — System-on-chip infrastructure clock.</li>
  <li><b>UCLK</b> — Unified memory controller clock (VRAM bandwidth).</li>
  <li><b>FCLK</b> — Data-fabric clock (GFX &harr; memory &harr; IO interconnect).</li>
  <li><b>DCLK0</b> — Video decoder engine clock.</li>
  <li><b>VCLK0</b> — Video encoder engine clock.</li>
  <li><b>DISPCLK</b> — Display pipe clock.</li>
  <li><b>DPPCLK</b> — Display pixel-processing clock.</li>
  <li><b>DPREFCLK</b> — Display reference clock (DisplayPort symbol rate base).</li>
  <li><b>DCFCLK</b> — Display controller fabric clock.</li>
  <li><b>DTBCLK</b> — Display timing-base clock (pixel timing generator).</li>
</ul>

<h4>UI controls on this tab</h4>
<ul>
  <li><b>Refresh</b> button — Sends a query to the SMU and updates every row. This is the
      only way to update values on this tab; nothing is streamed automatically.</li>
  <li>All cells are <b>read-only</b>. You can select and copy text from the Value column.</li>
</ul>

<h4>Caveats</h4>
<ul>
  <li>Values only update when you click <b>Refresh</b> — they are not live-streamed.</li>
  <li>GFX Voltage may read 0 mV when the GPU is deeply idle (GfxOff).</li>
  <li>Min/Max frequencies show the SMU's <i>enforced</i> boundaries, which may differ from
      what you requested if the SMU clamped or rejected the value.</li>
  <li>DC Max values are only meaningful on laptops with battery. On desktop GPUs they
      typically mirror the AC max or report 0.</li>
  <li>The Running Features hex value may be hard to read. Cross-reference individual bits
      in the <i>Features</i> sub-tab for a human-readable breakdown.</li>
</ul>
"""

CLOCK_CHEATSHEET = """
<h3>Clock Limits — DPM frequency boundaries</h3>
<p>This tab lets you override the Dynamic Power Management (DPM) frequency range for
each of the GPU's 11 clock domains. You can also set the Package Power Tracking (PPT)
limit.</p>

<h4>Limit types</h4>
<ul>
  <li><b>SoftMin / SoftMax</b> — <i>Advisory</i> limits. The SMU will try to keep the clock
      within this range but may briefly step outside it during transient workloads or
      thermal events. Think of these as "preferred operating range".</li>
  <li><b>HardMin / HardMax</b> — <i>Absolute</i> floor and ceiling. The SMU will not clock
      below HardMin or above HardMax under any circumstances (short of emergency
      thermal shutdown). Use these when you need a hard guarantee.</li>
</ul>

<h4>Clock domains</h4>
<ul>
  <li><b>GFXCLK</b> — Graphics/shader engine core clock.</li>
  <li><b>SOCCLK</b> — System-on-chip infrastructure clock (command processor, display
      controller fabric, etc.).</li>
  <li><b>UCLK</b> — Unified memory controller clock (directly determines VRAM bandwidth).</li>
  <li><b>FCLK</b> — Data fabric clock (interconnect between GFX, memory, and IO).</li>
  <li><b>DCLK0 / VCLK0</b> — Video decode and encode engine clocks.</li>
  <li><b>DISPCLK / DPPCLK</b> — Display pipe and pixel-processing clocks.</li>
  <li><b>DPREFCLK / DCFCLK / DTBCLK</b> — Display reference, display controller fabric, and
      display timing base clocks.</li>
</ul>

<h4>PPT Limit</h4>
<p>Total board power cap in watts. When total power draw reaches this limit the SMU
throttles clocks to stay within budget. Shown at the bottom of the tab in its own
group box with its own <b>Current</b> label, spinbox (0–600 W), and <b>Set</b> button.</p>

<h4>UI elements in each cell</h4>
<ul>
  <li><b>Current value label</b> (top of each cell) — The frequency the SMU currently reports
      for that limit direction (min or max). Updated on Refresh.</li>
  <li><b>QSpinBox</b> (bottom-left) — Enter the desired frequency in MHz. The value 0
      (displayed as "—") means "skip, don't send this command".</li>
  <li><b>Set button</b> (bottom-right of each cell) — Sends the
      <code>Set{Soft,Hard}{Min,Max}ByFreq</code> SMU message for that specific clock and
      limit type only. Other cells are not affected.</li>
  <li><b>Refresh button</b> (bottom of tab) — Re-reads all SMU state and updates every
      current value label.</li>
</ul>

<h4>Caveats</h4>
<ul>
  <li>Setting SoftMin &gt; SoftMax or HardMin &gt; HardMax may be silently rejected or cause
      instability.</li>
  <li>The "current value" for SoftMin and HardMin both show the same reported minimum
      frequency — the SMU reports only one min and one max per clock regardless of which
      limit type originally set it. Same applies to SoftMax/HardMax.</li>
  <li>Setting a very high HardMax does not guarantee the GPU will reach it. Thermal and
      power limits still apply.</li>
  <li>GFXCLK changes take effect immediately. Some display clocks may only change on the
      next mode switch.</li>
  <li>Enter 0 (shown as "—") to skip a row without sending anything.</li>
  <li>Display-related clocks (DISPCLK, DPPCLK, DPREFCLK, DCFCLK, DTBCLK) affect
      monitor output. Setting them incorrectly can cause a black screen until the next
      mode switch or driver reload.</li>
  <li>UCLK and FCLK are tightly coupled on most RDNA4 GPUs. Changing one without the
      other may put the memory subsystem into a sub-optimal ratio.</li>
</ul>
"""

CONTROLS_CHEATSHEET = """
<h3>Controls — Individual SMU commands</h3>
<p>Each row sends one specific command to the SMU when you click its <b>Set</b> button.
Nothing is sent until you explicitly click — there is no "Apply All".</p>

<h4>What each control does</h4>
<ul>
  <li><b>GFX Off</b> — When checked, sends <code>DisallowGfxOff</code> which prevents the GPU
      from entering deep idle power-gating (GfxOff). Useful during benchmarking to avoid
      wake-up latency spikes. Unchecked sends <code>AllowGfxOff</code> (default behavior).</li>
  <li><b>GFX DCS</b> — Dynamic Clock Spreading. When checked, allows the SMU to
      slightly spread clock transitions to reduce electromagnetic interference (EMI).
      Unchecked disables it.</li>
  <li><b>Workload Profile</b> — Tells the SMU which DPM heuristic to use:
    <ul>
      <li><i>Default</i> — balanced general-purpose behavior.</li>
      <li><i>3D Fullscreen</i> — prefers higher clocks, lower latency.</li>
      <li><i>PowerSave</i> — aggressively drops clocks when idle.</li>
      <li><i>Video</i> — optimized for media decode power efficiency.</li>
      <li><i>VR</i> — low-latency, high sustained clocks.</li>
      <li><i>Compute</i> — holds clocks high, disables some display power saving.</li>
      <li><i>Custom / Window3D</i> — vendor-specific variants.</li>
    </ul></li>
  <li><b>Throttler Mask</b> — A hex bitmask controlling which throttling sources are active.
      Each bit corresponds to a different throttler (thermal, power, current, etc.).
      Setting this to <code>0x0000</code> disables all throttlers.</li>
  <li><b>Temp Input Select</b> — Selects which temperature sensor (0–15) the SMU uses as
      its primary input for thermal throttling decisions (0 = Edge, 1 = Hotspot,
      higher values = VRAM, VR SoC, etc.).</li>
  <li><b>FW D-states Mask</b> — Controls which firmware power states (D-states) are allowed.
      Each bit enables a different low-power idle sub-state.</li>
  <li><b>DCS Architecture</b> — Selects the Dynamic Clock Spreading implementation:
      0 = Disabled, 1 = Async, 2 = Sync.</li>
</ul>

<h4>Table columns</h4>
<ul>
  <li><b>Human name</b> — Friendly label for the control.</li>
  <li><b>Table key</b> — Internal identifier used by the engine (e.g.
      <code>SMU_DisallowGfxOff</code>). Useful for cross-referencing logs.</li>
  <li><b>Unit</b> — Measurement unit if applicable (usually blank for toggles).</li>
  <li><b>Current value</b> — Last-known value read from hardware (where readable).
      Shows "—" for write-only commands.</li>
  <li><b>Custom input</b> — The widget you interact with (checkbox, combo box, or
      hex spinbox depending on the row).</li>
  <li><b>Set</b> — Per-row apply button. Sends only this one command to the SMU.</li>
</ul>

<h4>SMU messages sent (reference)</h4>
<ul>
  <li>GFX Off &rarr; <code>AllowGfxOff</code> / <code>DisallowGfxOff</code></li>
  <li>GFX DCS &rarr; <code>AllowGfxDcs</code> / <code>DisallowGfxDcs</code></li>
  <li>Workload Profile &rarr; <code>SetWorkloadMask</code></li>
  <li>Throttler Mask &rarr; <code>SetThrottlerMask</code></li>
  <li>Temp Input Select &rarr; <code>SetTemperatureInputSelect</code></li>
  <li>FW D-states Mask &rarr; <code>SetFwDstatesMask</code></li>
  <li>DCS Architecture &rarr; <code>SetDcsArch</code></li>
</ul>

<h4>Caveats</h4>
<ul>
  <li><span style="color: #c00;"><b>Setting Throttler Mask to 0x0000 disables ALL thermal and
      power protection.</b></span> The GPU can physically damage itself under load without
      these safeguards. Use with extreme caution.</li>
  <li>Workload profile changes are advisory — the SMU may still throttle if thermal limits
      are hit regardless of profile.</li>
  <li>GfxOff disallow does not persist across driver reloads or system sleep.</li>
  <li>Setting "no change" (-1) in a spinbox skips that command entirely.</li>
  <li>DCS Architecture and GFX DCS are related but separate: DCS toggles the feature on/off,
      while DCS Architecture selects which spreading algorithm is used when DCS is on.</li>
  <li>Temp Input Select values beyond the number of physical sensors on your GPU are
      invalid and will be silently ignored.</li>
</ul>
"""

FEATURES_CHEATSHEET = """
<h3>Features — Per-bit SMU feature toggles</h3>
<p>The SMU firmware maintains a 64-bit register where each bit enables or disables one
hardware feature. This tab gives you granular control over every individual bit.</p>

<h4>How it works</h4>
<ul>
  <li><b>Current state</b> — Shows whether the SMU currently has this feature ON or OFF
      (read from hardware on Refresh).</li>
  <li><b>Toggle checkbox</b> — Set the desired state. Nothing is sent until you click the
      row's <b>Set</b> button.</li>
  <li>Each <b>Set</b> button only toggles that one bit. There is no "Apply All" to prevent
      accidentally flipping features you didn't intend to change.</li>
</ul>

<h4>Complete feature reference</h4>
<p><b>Low word (bits 0–31):</b></p>
<ul>
  <li><b>0 — FW_DATA_READ</b> — Allows SMU firmware to read data tables from DRAM. Required
      for metrics, PPTable reads, and most telemetry. Do not disable.</li>
  <li><b>1 — DPM_GFXCLK</b> — Dynamic Power Management for the graphics core clock. When ON
      the GPU scales GFXCLK up/down with load. OFF locks it at its current level.</li>
  <li><b>2 — DPM_GFX_POWER_OPTIMIZER</b> — Additional power-aware optimizer that fine-tunes
      GFX voltage/frequency operating points beyond basic DPM stepping.</li>
  <li><b>3 — DPM_UCLK</b> — DPM for memory controller clock. OFF locks VRAM speed.</li>
  <li><b>4 — DPM_FCLK</b> — DPM for data-fabric clock. OFF locks the interconnect speed.</li>
  <li><b>5 — DPM_SOCCLK</b> — DPM for SoC infrastructure clock.</li>
  <li><b>6 — DPM_LINK</b> — DPM for PCIe link speed/width. OFF prevents link-speed downshift
      to save power at idle.</li>
  <li><b>7 — DPM_DCN</b> — DPM for the display controller (DCN). OFF locks display clocks.</li>
  <li><b>8 — VMEMP_SCALING</b> — Memory power-supply voltage scaling. Allows the SMU to
      reduce VRAM voltage at low clock speeds.</li>
  <li><b>9 — VDDIO_MEM_SCALING</b> — Memory I/O voltage scaling. Same concept for the
      VDDIO rail feeding the memory PHY.</li>
  <li><b>10 — DS_GFXCLK</b> — Deep-sleep for GFXCLK. Allows the clock to gate entirely
      when idle (lowest power). OFF keeps it ticking.</li>
  <li><b>11 — DS_SOCCLK</b> — Deep-sleep for SOCCLK.</li>
  <li><b>12 — DS_FCLK</b> — Deep-sleep for FCLK.</li>
  <li><b>13 — DS_LCLK</b> — Deep-sleep for LCLK (PCIe reference clock domain).</li>
  <li><b>14 — DS_DCFCLK</b> — Deep-sleep for display controller fabric clock.</li>
  <li><b>15 — DS_UCLK</b> — Deep-sleep for memory controller clock.</li>
  <li><b>16 — GFX_ULV</b> — Ultra-Low Voltage mode for GFX. Allows the GPU to drop to a
      very low voltage/frequency point at near-zero load.</li>
  <li><b>17 — FW_DSTATE</b> — Firmware D-state management. Allows the SMU to enter
      low-power firmware states between workloads.</li>
  <li><b>18 — GFXOFF</b> — GfxOff power gating. Allows the entire graphics engine to power
      down when idle. Saves significant power but adds wake-up latency.</li>
  <li><b>19 — BACO</b> — Bus Active, Chip Off. A deep idle state where most of the GPU
      is powered down but PCIe stays alive.</li>
  <li><b>20 — MM_DPM</b> — DPM for multimedia engines (VCN/JPEG). OFF locks video
      encode/decode clocks.</li>
  <li><b>21 — SOC_MPCLK_DS</b> — Deep-sleep for the SoC MP (management processor) clock.</li>
  <li><b>22 — BACO_MPCLK_DS</b> — Deep-sleep for the MP clock specifically during BACO.</li>
  <li><b>23 — THROTTLERS</b> — Master switch for all throttling logic. OFF disables every
      throttler (thermal, power, current). <span style="color:#c00;">Extremely
      dangerous.</span></li>
  <li><b>24 — SMARTSHIFT</b> — AMD SmartShift (laptop dynamic power sharing between CPU
      and GPU). Irrelevant on desktops.</li>
  <li><b>25 — GTHR</b> — GPU Thermal Headroom reporting. Exposes how close the GPU is
      to its thermal limit.</li>
  <li><b>26 — ACDC</b> — AC/DC power-source detection. Allows the SMU to switch profiles
      between wall power and battery.</li>
  <li><b>27 — VR0HOT</b> — Voltage-regulator over-temperature protection. When the VRM
      reports overheating, the SMU throttles to reduce current draw.</li>
  <li><b>28 — FW_CTF</b> — <span style="color: #c00;"><b>Critical Thermal Fault handler.
      Permanently locked ON.</b></span> This is the GPU's last-resort emergency thermal
      shutdown. Cannot be disabled from this UI.</li>
  <li><b>29 — FAN_CONTROL</b> — SMU-managed fan curve. OFF hands fan control to the host
      driver or leaves it at the last-set duty cycle.</li>
  <li><b>30 — GFX_DCS</b> — Dynamic Clock Spreading for GFX. Reduces EMI by slightly
      modulating the clock edge timing.</li>
  <li><b>31 — GFX_READ_MARGIN</b> — Read-margin adjustment for GFX SRAM. Internal
      reliability feature, leave ON.</li>
</ul>

<p><b>High word (bits 32–56):</b></p>
<ul>
  <li><b>32 — LED_DISPLAY</b> — Controls the GPU's onboard LED/RGB lighting via SMU.</li>
  <li><b>33 — GFXCLK_SPREAD_SPECTRUM</b> — Spread-spectrum clocking for GFXCLK to reduce
      EMI. Turning OFF gives a slightly cleaner clock but may violate EMI compliance.</li>
  <li><b>34 — OUT_OF_BAND_MONITOR</b> — Out-of-band telemetry monitoring (BMC/IPMI
      sideband reporting on server GPUs).</li>
  <li><b>35 — OPTIMIZED_VMIN</b> — Per-part optimized minimum-voltage calibration. Allows
      the SMU to run lower voltage than the generic table for parts that pass testing.</li>
  <li><b>36 — GFX_IMU</b> — GFX Integrated Management Unit. Internal co-processor that
      handles fast voltage/frequency transitions.</li>
  <li><b>37 — BOOT_TIME_CAL</b> — Boot-time silicon calibration. Runs once at power-on to
      characterize the specific die. Disabling skips calibration (not recommended).</li>
  <li><b>38 — GFX_PCC_DFLL</b> — GFX Precision Clock Controller with Digital Frequency-Locked
      Loop. Enables fine-grained clock regulation.</li>
  <li><b>39 — SOC_CG</b> — SoC clock gating. Allows unused SoC blocks to gate their clocks
      for power savings.</li>
  <li><b>40 — DF_CSTATE</b> — Data Fabric C-state. Allows the interconnect fabric to enter
      low-power idle states.</li>
  <li><b>41 — GFX_EDC</b> — Electrical Design Current protection for GFX. Throttles when
      instantaneous current spikes approach the VRM design limit.</li>
  <li><b>42 — BOOT_POWER_OPT</b> — Boot-time power optimization. Reduces power during
      driver initialization.</li>
  <li><b>43 — CLOCK_POWER_DOWN_BYPASS</b> — Bypasses clock power-down sequencing. Internal
      debug feature.</li>
  <li><b>44 — DS_VCN</b> — Deep-sleep for the Video Core Next (VCN) encode/decode engine.</li>
  <li><b>45 — BACO_CG</b> — Clock gating specifically during BACO state.</li>
  <li><b>46 — MEM_TEMP_READ</b> — Enables the SMU to read VRAM temperature sensors.</li>
  <li><b>47 — ATHUB_MMHUB_PG</b> — Power gating for the Address Translation Hub and
      Multimedia Hub. Saves idle power but adds latency on first access after gate.</li>
  <li><b>48 — SOC_PCC</b> — SoC Precision Clock Controller. Fine clock regulation for
      SoC domain.</li>
  <li><b>49 — EDC_PWRBRK</b> — EDC Power-Break. An emergency current-limiting mechanism that
      aggressively throttles when instantaneous power spikes are detected.</li>
  <li><b>50 — SOC_EDC_XVMIN</b> — SoC EDC with cross-voltage-minimum awareness.</li>
  <li><b>51 — GFX_PSM_DIDT</b> — GFX Power State Machine di/dt (current-slew-rate)
      protection. Limits how fast current can ramp to protect the power delivery.</li>
  <li><b>52 — APT_ALL_ENABLE</b> — Adaptive Power Tuning master enable.</li>
  <li><b>53 — APT_SQ_THROTTLE</b> — APT Shader Queue throttle — dynamically reduces shader
      workload to stay within power/thermal budgets.</li>
  <li><b>54 — APT_PF_DCS</b> — APT Power-Filtered Dynamic Clock Spreading.</li>
  <li><b>55 — GFX_EDC_XVMIN</b> — GFX EDC with cross-voltage-minimum awareness.</li>
  <li><b>56 — GFX_DIDT_XVMIN</b> — GFX di/dt with cross-voltage-minimum awareness.</li>
</ul>
<p>Bits 57–63 are SPARE (reserved/unused) and are not shown in the table.</p>

<h4>Table columns</h4>
<ul>
  <li><b>Bit</b> — Bit position in the 64-bit feature register (0–63).</li>
  <li><b>Name</b> — Human-readable feature name from the SMU firmware header.</li>
  <li><b>Current state</b> — ON or OFF as last read from hardware.</li>
  <li><b>Toggle</b> — Checkbox to set your desired state. Greyed out for FW_CTF.</li>
  <li><b>Set</b> — Sends <code>EnableSmuFeaturesLow/High</code> or
      <code>DisableSmuFeaturesLow/High</code> for this one bit only.</li>
</ul>

<h4>Caveats</h4>
<ul>
  <li>Disabling DPM features locks clocks at their current level. If you disable DPM_GFXCLK
      while the GPU is idle, you may be stuck at a low clock until you re-enable it.</li>
  <li>Disabling thermal/protection features (THROTTLERS, VR0HOT, GFX_EDC, EDC_PWRBRK)
      removes hardware protection. The GPU can overheat or overdraw current.</li>
  <li>Some features have hidden dependencies — disabling a parent feature may silently
      force-disable its children.</li>
  <li>Changes take effect immediately but <b>do not persist</b> across driver reloads or
      system reboots.</li>
  <li>Enabling a feature the SMU firmware doesn't support on your specific GPU may be
      silently ignored or return an error code.</li>
  <li>The ACDC and SMARTSHIFT features are laptop-oriented and have no effect on desktop
      GPUs.</li>
  <li>Disabling FW_DATA_READ (bit 0) will break metrics, table reads, and most telemetry.
      Re-enabling it may require a driver reload.</li>
</ul>
"""

METRICS_CHEATSHEET = """
<h3>Metrics — Live SMU performance telemetry</h3>
<p>This tab reads the <code>SmuMetrics_t</code> struct from the SMU's DMA buffer and
displays every field as a live, refreshable table.</p>

<h4>All metrics sections</h4>
<p>The metrics table is organized into sections. Every field from the
<code>SmuMetrics_t</code> struct is displayed:</p>

<p><b>Current Clocks (MHz)</b></p>
<ul>
  <li><b>CurrClock_GFXCLK / SOCCLK / UCLK / FCLK / DCLK0 / VCLK0 / DISPCLK / DPPCLK /
      DPREFCLK / DCFCLK / DTBCLK</b> — Real-time frequency of each clock domain. This is
      what the hardware is actually running at right now, not the requested target.</li>
</ul>

<p><b>Power</b></p>
<ul>
  <li><b>AverageSocketPower</b> — Average power draw from the GPU die only (watts).</li>
  <li><b>AverageTotalBoardPower</b> — Average total board power including VRMs, memory, and
      aux circuits (watts).</li>
  <li><b>dGPU_W_MAX</b> — Maximum instantaneous power draw seen during the sampling window.</li>
  <li><b>EnergyAccumulator</b> — Cumulative energy counter (increments over time). Useful for
      computing average power over custom intervals.</li>
</ul>

<p><b>Voltage (mV)</b></p>
<ul>
  <li><b>AvgVoltage_VDD_GFX</b> — Graphics core voltage rail.</li>
  <li><b>AvgVoltage_VDD_SOC</b> — SoC logic voltage rail.</li>
  <li><b>AvgVoltage_VDDCI_MEM</b> — Memory controller I/O voltage.</li>
  <li><b>AvgVoltage_VDDIO_MEM</b> — Memory PHY I/O voltage.</li>
</ul>

<p><b>Current (mA)</b></p>
<ul>
  <li><b>AvgCurrent_VDD_GFX / VDD_SOC / VDDCI_MEM / VDDIO_MEM</b> — Averaged current draw
      for each voltage rail. Multiply with voltage to get per-rail power.</li>
</ul>

<p><b>Activity (%)</b></p>
<ul>
  <li><b>AverageGfxActivity</b> — GPU shader/compute utilization (0–100%).</li>
  <li><b>AverageUclkActivity</b> — Memory controller utilization.</li>
  <li><b>AverageVcn0ActivityPercentage</b> — VCN0 (video encode/decode engine 0) utilization.</li>
  <li><b>Vcn1ActivityPercentage</b> — VCN1 (second video engine, if present) utilization.</li>
</ul>

<p><b>Fan</b></p>
<ul>
  <li><b>AvgFanPwm</b> — Fan duty cycle as a percentage (0–100).</li>
  <li><b>AvgFanRpm</b> — Fan speed in RPM. 0 when fan-stop is active.</li>
</ul>

<p><b>Temperature</b></p>
<ul>
  <li><b>AvgTemperature_Edge</b> — Die-edge temperature sensor (&deg;C).</li>
  <li><b>AvgTemperature_Hotspot / Hotspot_GFX / Hotspot_SOC</b> — Peak junction temps.
      Hotspot is the overall max; GFX and SOC break it down by domain.</li>
  <li><b>AvgTemperature_Mem</b> — VRAM temperature.</li>
  <li><b>AvgTemperature_VR_GFX / VR_SOC / VR_Mem0 / VR_Mem1</b> — Voltage regulator
      temperatures for each power rail.</li>
  <li><b>AvgTemperature_Liquid0 / Liquid1</b> — Liquid cooling loop sensors (if equipped).</li>
  <li><b>AvgTemperature_PLX</b> — PLX/PCIe switch temperature (multi-GPU boards).</li>
  <li><b>AvgTemperatureFanIntake</b> — Ambient air temperature at the fan intake.</li>
</ul>

<p><b>PCIe</b></p>
<ul>
  <li><b>PcieRate</b> — Current PCIe generation (1 = Gen1, 2 = Gen2, … 5 = Gen5).</li>
  <li><b>PcieWidth</b> — Current PCIe lane width (1, 2, 4, 8, 16).</li>
</ul>

<p><b>Throttling (%)</b></p>
<ul>
  <li><b>Throttle_Temp_Edge / Hotspot / Hotspot_GFX / Hotspot_SOC</b> — Thermal throttling
      from edge, hotspot (junction), GFX-specific hotspot, or SOC hotspot sensors.</li>
  <li><b>Throttle_Temp_Mem</b> — Memory temperature throttling.</li>
  <li><b>Throttle_Temp_VR_GFX / VR_SOC / VR_Mem0 / VR_Mem1</b> — Voltage regulator
      thermal throttling for GFX, SOC, and memory VRMs.</li>
  <li><b>Throttle_Temp_Liquid0 / Liquid1 / PLX</b> — Liquid cooling and PLX sensor throttling.</li>
  <li><b>Throttle_TDC_GFX / TDC_SOC</b> — Thermal Design Current limit. Fires when the
      current draw of the GFX or SOC rail approaches the VRM current limit.</li>
  <li><b>Throttle_PPT0 / PPT1 / PPT2 / PPT3</b> — Package Power Tracking limits. PPT0 is
      the primary board power limit. Non-zero means the SMU is actively reducing clocks
      to stay within the power budget.</li>
  <li><b>Throttle_FIT</b> — Failure In Time / reliability throttling.</li>
  <li><b>Throttle_GFX_APCC_Plus</b> — Adaptive Power Control Circuit throttling.</li>
  <li><b>Throttle_GFX_DVO</b> — Digital Voltage Optimizer throttling.</li>
  <li><b>VmaxThrottlingPercentage</b> — How much the maximum-voltage limiter is restricting
      clocks.</li>
</ul>

<p><b>Average Frequencies (MHz)</b></p>
<ul>
  <li><b>AverageGfxclkFrequencyTarget</b> — The clock the SMU is targeting for GFX.</li>
  <li><b>AverageGfxclkFrequencyPreDs / PostDs</b> — GFX clock before and after deep-sleep
      gating is applied. PreDs &ge; PostDs; the difference shows how much time is spent
      in deep-sleep.</li>
  <li><b>AverageFclkFrequencyPreDs / PostDs</b> — Same for FCLK.</li>
  <li><b>AverageMemclkFrequencyPreDs / PostDs</b> — Same for memory clock.</li>
  <li><b>AverageVclk0Frequency / AverageDclk0Frequency</b> — Average video encode/decode
      clock 0.</li>
  <li><b>AverageVclk1Frequency / AverageDclk1Frequency</b> — Same for engine 1.</li>
  <li><b>AveragePCIeBusy</b> — Average PCIe bus utilization.</li>
</ul>

<p><b>Moving Averages</b></p>
<ul>
  <li>Same metrics as Average Frequencies plus activity and power, but using a longer
      exponential-moving-average window. Useful for smoothing out transient spikes.</li>
  <li>Includes: <b>MovingAverageGfxclkFrequencyTarget</b>, <b>PreDs/PostDs</b> for GFXCLK,
      FCLK, MEMCLK; <b>MovingAverageVclk0/Dclk0Frequency</b>;
      <b>MovingAverageGfxActivity / UclkActivity / Vcn0Activity / PCIeBusy</b>;
      <b>MovingAverageUclkActivity_MAX</b>; <b>MovingAverageSocketPower</b>.</li>
</ul>

<p><b>D3Hot Counters</b></p>
<ul>
  <li>Entry/exit counters for each D3Hot sequence. Tracks how many times the GPU has entered
      and exited each low-power mode:</li>
  <li><b>BACO</b> — Bus Active Chip Off.</li>
  <li><b>MSR</b> — Modern Standby Resume.</li>
  <li><b>BAMACO</b> — Bus Active Memory Active Chip Off.</li>
  <li><b>ULPS</b> — Ultra Low Power State (display-related).</li>
  <li><b>ArmMsgReceived_*</b> — Counts of "arm" messages received per mode (internal
      handshake between host driver and SMU for D3 transitions).</li>
</ul>

<p><b>Misc</b></p>
<ul>
  <li><b>MetricsCounter</b> — How many times the SMU firmware has updated the metrics struct.
      Incrementing confirms the SMU is alive and sampling.</li>
  <li><b>ApuSTAPMSmartShiftLimit / ApuSTAPMLimit</b> — SmartShift/STAPM power limits
      (laptop-only, irrelevant on desktop).</li>
  <li><b>AvgApuSocketPower</b> — APU socket power (laptop-only).</li>
  <li><b>AverageUclkActivity_MAX</b> — Peak memory utilization seen in the window.</li>
  <li><b>PublicSerialNumberLower / Upper</b> — GPU die serial number (two 32-bit halves).</li>
</ul>

<h4>UI controls</h4>
<ul>
  <li><b>Refresh Now</b> — One-shot read of the full SmuMetrics_t struct.</li>
  <li><b>Auto-refresh checkbox</b> — Enables a periodic timer.</li>
  <li><b>Interval spinbox (1–30 s)</b> — Sets the auto-refresh period. Adjustable while
      auto-refresh is running.</li>
  <li><b>Status label</b> — Shows last update timestamp and value count, or error text.</li>
</ul>

<h4>Caveats</h4>
<ul>
  <li>Metrics are sampled by the SMU firmware at its own internal rate, not at your refresh
      interval. Polling faster than ~2 s adds MMIO overhead with diminishing returns.</li>
  <li>Some metric values are <i>averaged</i> over the SMU's sampling window, not
      instantaneous snapshots. "Pre/PostDs" frequency differences reveal deep-sleep
      duty cycle, not instantaneous jitter.</li>
  <li>D3Hot counters and APU/STAPM fields are typically zero on desktop GPUs.</li>
  <li>Throttler names (Temp_Edge, PPT0, TDC_GFX, etc.) are from the SMU v14.0 firmware
      header (smu14_driver_if_v14_0.h). Values are 0-100% activity percentage.</li>
  <li>PublicSerialNumber fields may be zero if the GPU vendor has not programmed a serial.</li>
</ul>
"""

TABLES_CHEATSHEET = """
<h3>Tables — Raw SMU table dumps &amp; PFE settings</h3>
<p>This tab reads raw data tables from the SMU's DMA buffer as hex dumps, and provides
controls for reading and patching PFE (PPTable header) settings.</p>

<h4>Other Tables (on demand)</h4>
<ul>
  <li><b>Read PPTable</b> (table id 0) — Raw hex dump of the power-play table stored in SMU
      SRAM. Contains DPM frequency/voltage curves, thermal limits, fan curves, and feature
      enable masks. The layout is GPU-generation-specific.</li>
  <li><b>Read Driver Info</b> (table id 10) — DPM frequency tables and driver state. Shows
      all DPM levels the SMU has configured for each clock domain.</li>
  <li><b>Read ECC Info</b> (table id 11) — Error-correction counters. Shows
      correctable/uncorrectable error counts per memory partition.</li>
</ul>

<h4>PFE Settings (PPTable Header)</h4>
<p>The PFE (PowerPlay Feature Enable) section reads and patches fields in the PPTable
header stored in SMU SRAM: <code>FeaturesToRun</code>, <code>FwDStateMask</code>, and
<code>DebugOverrides</code>.</p>
<ul>
  <li><b>Read PFE Settings</b> — Reads the current PFE_Settings_t from TABLE_PPTABLE and
      displays FeaturesToRun, FwDStateMask, and DebugOverrides.</li>
  <li><b>Patch FeaturesToRun</b> — Adds GFX_EDC (41), CLOCK_POWER_DOWN_BYPASS (43),
      EDC_PWRBRK (49) to FeaturesToRun and writes back via TransferTableDram2Smu.</li>
  <li><b>Patch DebugOverrides</b> — Sets DISABLE_FMAX_VMAX (0x40) and
      ENABLE_PROFILING_MODE (0x1000) in DebugOverrides and writes back.</li>
  <li><b>Check OD Memory Caps</b> — Checks ODCAP bits 4 (AUTO_OC_MEMORY),
      5 (MEMORY_TIMING_TUNE), 6 (MANUAL_AC_TIMING) and UCLK OD support.</li>
</ul>

<h4>Tools DRAM Path (msg 0x53)</h4>
<p>The "Tools Path" buttons perform the same patches as their counterparts above but
write via <code>TransferTableDram2SmuWithAddr</code> (msg 0x53) instead of the standard
msg 0x13. This bypasses driver-path rejection for table writes. Falls back to
TABLE_CUSTOM_SKUTABLE (id=12) if TABLE_PPTABLE fails.</p>

<h4>UI controls</h4>
<ul>
  <li><b>Hex view</b> — Read-only text area displaying raw hex dumps from the table-read
      buttons.</li>
  <li><b>PFE result view</b> — Read-only text area showing PFE read/patch results.</li>
</ul>

<h4>Caveats</h4>
<ul>
  <li>Raw table hex dumps require knowledge of the struct layout to interpret. The PPTable
      format is GPU-generation-specific.</li>
  <li>Reading tables while the GPU is under heavy load may briefly stall the SMU command
      interface.</li>
  <li>PFE patches modify the SMU's in-SRAM copy of the PPTable header. Changes are
      <b>volatile</b> — lost on reboot, driver reload, or GPU reset.</li>
  <li>The Tools DRAM Path (msg 0x53) is an undocumented SMU message. It may not be
      available on all firmware versions.</li>
</ul>
"""

REG_CHEATSHEET_HTML = """
<h3>Registry Patch &mdash; AMD GPU Driver Settings</h3>
<p>This tab reads and writes DWORD values in the AMD GPU driver's registry key
under <code>HKLM\\SYSTEM\\CurrentControlSet\\Control\\Class\\{display-GUID}\\XXXX</code>.
Changes are <b>persistent across reboots</b> (unlike SMU commands or RAM patches).</p>

<h4>How it works</h4>
<ul>
  <li><b>Current</b> column &mdash; The value currently stored in the registry (read-only).</li>
  <li><b>Custom</b> column &mdash; The value you want to apply. Checkboxes toggle 0/1;
      spinboxes accept numeric values.</li>
  <li><b>Select recommended</b> &mdash; Fills the Custom column with the recommended
      anti-gating values (disables power-saving features). Settings that have no specific
      recommendation are left at their current registry value.</li>
  <li><b>Apply</b> &mdash; Writes all Custom column values to the registry. A backup of
      original values is saved on first apply.</li>
  <li><b>Return to stock</b> &mdash; Restores the original values from the backup file.</li>
</ul>

<h4>Anti-Clock-Gating Patches</h4>
<p>These disable various power-saving features that cause clock gating, power gating,
and idle downclocking. Disabling them keeps clocks high and reduces latency spikes
at the cost of higher idle power consumption.</p>
<ul>
  <li><b>EnableUlps</b> (&rarr;0) &mdash; Ultra Low Power State. Aggressively power-gates the
      GPU at idle. Causes wake-up latency, CrossFire/multi-monitor instability.</li>
  <li><b>PP_GPUPowerDownEnabled</b> (&rarr;0) &mdash; GPU power-down gating. Allows the driver
      to fully gate the GPU when idle.</li>
  <li><b>EnableUvdClockGating</b> (&rarr;0) &mdash; UVD (Unified Video Decoder) clock gating.
      Legacy; replaced by VCN on RDNA. Harmless to set.</li>
  <li><b>EnableVceSwClockGating</b> (&rarr;0) &mdash; VCE (Video Coding Engine) software clock
      gating. Legacy; replaced by VCN on RDNA.</li>
  <li><b>KMD_EnableContextBasedPowerManagement</b> (&rarr;0) &mdash; Context-based power
      management. Driver switches power states based on application context.</li>
  <li><b>EnableAspmL0s / EnableAspmL1</b> (&rarr;0) &mdash; PCIe Active State Power Management.
      L0s and L1 add link-level power saving with latency penalties.</li>
  <li><b>PP_ULPSDelayIntervalInMilliSeconds</b> (&rarr;0) &mdash; Delay before entering ULPS.
      Setting to 0 effectively disables the ULPS timer.</li>
  <li><b>DisableVCEPowerGating</b> (&rarr;1) &mdash; VCE power gating (1=disabled). Legacy.</li>
  <li><b>PP_DisablePowerContainment</b> (&rarr;1) &mdash; Power containment / boost capping.
      When disabled, the GPU does not cap boost clocks based on power draw. Useful for
      benchmarking.</li>
  <li><b>PP_DisableClockStretcher</b> (&rarr;1) &mdash; Clock stretcher. When disabled, prevents
      mid-load clock drops used to manage transient power spikes.</li>
  <li><b>PP_MCLKDeepSleepDisable</b> (&rarr;1) &mdash; Memory clock deep sleep. When disabled,
      VRAM stays at operating frequency instead of dropping to deep-sleep.</li>
  <li><b>KMD_EnableGFXLowPowerState</b> (&rarr;0) &mdash; GFX low-power state. When disabled,
      the graphics engine does not enter its low-power idle mode.</li>
  <li><b>DisableDrmdmaPowerGating</b> (&rarr;1) &mdash; DRM/DMA power gating (1=disabled).</li>
</ul>

<h4>Verification Values</h4>
<p>These are typically already set correctly by modern drivers. We verify them
but they rarely need changing:</p>
<ul>
  <li><b>PP_SclkDeepSleepDisable</b> (=1) &mdash; SCLK deep sleep disabled. Prevents HDMI
      audio dropouts.</li>
  <li><b>PP_DisableVoltageIsland</b> (=1) &mdash; Voltage island power gating disabled.</li>
  <li><b>DisableSAMUPowerGating</b> (=1) &mdash; SAMU power gating disabled (pre-RDNA).</li>
  <li><b>GCOOPTION_DisableGPIOPowerSaveMode</b> (=1) &mdash; GPIO power-save mode disabled.</li>
</ul>

<h4>Performance Tuning</h4>
<p>These control DPM behavior, stutter mode, and other tunable parameters. They are
<b>not</b> included in &ldquo;Select recommended&rdquo; &mdash; the Custom column defaults to the
current registry value so you can tweak them manually.</p>
<ul>
  <li><b>PP_ThermalAutoThrottlingEnable</b> &mdash; Master thermal throttle switch in
      PowerPlay. Default 1 (enabled). Setting to 0 disables thermal throttling &mdash;
      <span style="color:#c00;">dangerous, GPU can overheat.</span></li>
  <li><b>StutterMode</b> &mdash; Memory clock stutter mode. 0=OFF, 1=on, 2=Vega default.
      Setting to 0 eliminates MCLK stuttering (keeps VRAM clock constant).</li>
  <li><b>PP_MCLKStutterModeThreshold</b> &mdash; Threshold before the driver engages
      stutter mode. Default ~81920 (0x14000). Lower values or 0 reduce/disable stutter.</li>
  <li><b>PP_ActivityTarget</b> &mdash; DPM activity threshold (%). Default 30. Lower values
      make the GPU ramp up to higher clocks sooner under light load.</li>
  <li><b>PP_AllGraphicLevel_UpHyst</b> &mdash; Delay in ms before clocking up. Default ~50ms.
      Setting to 0 gives immediate clock ramp-up.</li>
  <li><b>PP_AllGraphicLevel_DownHyst</b> &mdash; Delay in ms before clocking down. Default
      ~20ms. Higher values keep clocks elevated longer after load drops.</li>
  <li><b>KMD_FRTEnabled</b> &mdash; Frame Rate Target Control. 0=off, 1=on. When on, the
      driver caps FPS via KMD_MaxFrameRateRequested.</li>
  <li><b>DisableFBCSupport</b> &mdash; Frame Buffer Compression. 1=disabled. Disabling can
      fix rendering artifacts on some setups.</li>
  <li><b>DMMEnableDDCPolling</b> &mdash; DDC (Display Data Channel) polling. 0=disabled.
      Disabling reduces periodic overhead from monitor detection.</li>
</ul>

<h4>Caveats</h4>
<ul>
  <li>All changes require a <b>reboot</b> to take full effect (the driver reads these
      values at load time).</li>
  <li>UVD, VCE, and SAMU settings are GCN/Vega-era legacy. On RDNA GPUs they are
      harmless but functionally no-ops (VCN replaced UVD+VCE).</li>
  <li>Disabling thermal throttling or power containment removes hardware protection.
      Monitor temperatures carefully.</li>
  <li>A backup of original values is saved on first Apply. Use &ldquo;Return to stock&rdquo; to
      restore.</li>
  <li>Administrator privileges are required for both reading and writing.</li>
</ul>
"""

THROTTLERS_CHEATSHEET = """
<h3>Throttlers — Per-Bit Throttler Diagnostics &amp; Mask Control</h3>
<p>The SMU firmware enforces <b>21 independent throttlers</b>, each tied to a
specific hardware limit (temperature sensor, current rail, power budget, or
reliability model). When a throttler fires it forces clocks down until the
monitored quantity drops below its threshold. The <b>Live %</b> column shows
how aggressively each throttler is currently reducing clocks (0&nbsp;=&nbsp;not
firing, 100&nbsp;=&nbsp;maximum throttle).</p>

<h4>Bit Map</h4>
<table border="1" cellpadding="4" cellspacing="0">
  <tr><th>Bit</th><th>Category</th><th>Name</th></tr>
  <tr><td>0</td><td>Thermal</td><td>Temp_Edge</td></tr>
  <tr><td>1</td><td>Thermal</td><td>Temp_Hotspot</td></tr>
  <tr><td>2</td><td>Thermal</td><td>Temp_Hotspot_GFX</td></tr>
  <tr><td>3</td><td>Thermal</td><td>Temp_Hotspot_SOC</td></tr>
  <tr><td>4</td><td style="color:#d80;">Thermal (MEM)</td><td>Temp_Mem</td></tr>
  <tr><td>5</td><td>VR Thermal</td><td>Temp_VR_GFX</td></tr>
  <tr><td>6</td><td>VR Thermal</td><td>Temp_VR_SOC</td></tr>
  <tr><td>7</td><td style="color:#d80;">VR Thermal (MEM)</td><td>Temp_VR_Mem0</td></tr>
  <tr><td>8</td><td style="color:#d80;">VR Thermal (MEM)</td><td>Temp_VR_Mem1</td></tr>
  <tr><td>9</td><td>Thermal</td><td>Temp_Liquid0</td></tr>
  <tr><td>10</td><td>Thermal</td><td>Temp_Liquid1</td></tr>
  <tr><td>11</td><td>Thermal</td><td>Temp_PLX</td></tr>
  <tr><td>12</td><td>Current</td><td>TDC_GFX</td></tr>
  <tr><td>13</td><td>Current</td><td>TDC_SOC</td></tr>
  <tr><td>14</td><td>Power</td><td>PPT0</td></tr>
  <tr><td>15</td><td>Power</td><td>PPT1</td></tr>
  <tr><td>16</td><td>Power</td><td>PPT2</td></tr>
  <tr><td>17</td><td>Power</td><td>PPT3</td></tr>
  <tr><td>18</td><td style="color:#c00;">Reliability</td><td>FIT</td></tr>
  <tr><td>19</td><td>Other</td><td>GFX_APCC_Plus</td></tr>
  <tr><td>20</td><td>Other</td><td>GFX_DVO</td></tr>
</table>

<h4>Per-Throttler Descriptions</h4>

<p><b>Thermal throttlers (bits 0–4, 9–11):</b></p>
<ul>
  <li><b>0 — Temp_Edge</b> — Die-edge temperature sensor. Fires when the edge
      temperature approaches the VBIOS thermal limit (~100&nbsp;&deg;C typical).
      Edge temp is usually the lowest reading; if this fires, the GPU is very
      hot.</li>
  <li><b>1 — Temp_Hotspot</b> — Peak junction (hotspot) temperature. The single
      hottest point on the die. This is the primary thermal limiter on most GPUs
      and the first throttler to fire under heavy load.</li>
  <li><b>2 — Temp_Hotspot_GFX</b> — Hotspot temperature specific to the GFX
      (shader/compute) domain. Fires when the graphics engine area exceeds its
      thermal limit.</li>
  <li><b>3 — Temp_Hotspot_SOC</b> — Hotspot temperature for the SoC domain
      (memory controllers, display engine, media engines). Usually cooler than
      GFX.</li>
  <li><b>4 — Temp_Mem</b> — <span style="color:#d80;"><b>VRAM temperature
      (memory-related).</b></span> Fires when GDDR6/GDDR6X memory modules exceed
      their thermal limit. Common on cards with poor memory cooling or high
      ambient temperatures. Disabling is relatively safe if you monitor VRAM
      temps externally.</li>
  <li><b>9 — Temp_Liquid0</b> — Liquid cooling loop temperature sensor 0.
      Only active on cards with liquid cooling connectors or AIO coolers.</li>
  <li><b>10 — Temp_Liquid1</b> — Liquid cooling loop temperature sensor 1
      (secondary loop, if present).</li>
  <li><b>11 — Temp_PLX</b> — PLX/PCIe bridge chip temperature. Only relevant on
      multi-GPU boards with a PLX switch. Reports 0 on most single-GPU cards.</li>
</ul>

<p><b>VR thermal throttlers (bits 5–8):</b></p>
<ul>
  <li><b>5 — Temp_VR_GFX</b> — Voltage regulator temperature for the GFX core
      power rail. Fires when the VRM MOSFETs powering the GPU core overheat.
      Indicates inadequate VRM cooling or excessive power draw.</li>
  <li><b>6 — Temp_VR_SOC</b> — Voltage regulator temperature for the SoC rail.
      Typically runs cooler than the GFX VRM.</li>
  <li><b>7 — Temp_VR_Mem0</b> — <span style="color:#d80;"><b>Memory VRM
      temperature, channel 0 (memory-related).</b></span> Fires when the voltage
      regulator feeding VRAM channel 0 overheats.</li>
  <li><b>8 — Temp_VR_Mem1</b> — <span style="color:#d80;"><b>Memory VRM
      temperature, channel 1 (memory-related).</b></span> Same as above for the
      second memory channel.</li>
</ul>

<p><b>Current throttlers (bits 12–13):</b></p>
<ul>
  <li><b>12 — TDC_GFX</b> — Thermal Design Current for the GFX rail. Fires when
      the sustained current draw approaches the VRM's rated continuous current
      capacity. Distinct from EDC (which is instantaneous spikes).</li>
  <li><b>13 — TDC_SOC</b> — Thermal Design Current for the SoC rail. Usually has
      generous headroom on desktop GPUs.</li>
</ul>

<p><b>Power throttlers (bits 14–17):</b></p>
<ul>
  <li><b>14 — PPT0</b> — Package Power Tracking limit 0 (primary). This is the
      main total-board-power cap. When total power draw reaches the PPT0 limit
      the SMU throttles all clocks to stay within budget. The most common
      throttler under sustained heavy load.</li>
  <li><b>15 — PPT1</b> — PPT limit 1 (secondary). A second power budget tier,
      sometimes used for a tighter short-duration power cap.</li>
  <li><b>16 — PPT2</b> — PPT limit 2 (tertiary). Rarely active on consumer
      GPUs.</li>
  <li><b>17 — PPT3</b> — PPT limit 3 (quaternary). Typically unused on desktop
      RDNA cards.</li>
</ul>

<p><b>Reliability throttler (bit 18):</b></p>
<ul>
  <li><b>18 — FIT</b> — <span style="color:#c00;"><b>Failure-In-Time /
      reliability throttling. Key suspect for unexplained VRAM clock
      drops.</b></span> The SMU maintains an internal electromigration and
      reliability model. When the firmware estimates that sustained
      voltage&times;frequency operation is degrading the chip's projected
      lifespan, FIT throttling reduces clocks proactively — even when
      temperatures, power, and current are all within normal limits. This is the
      most common cause of &ldquo;phantom&rdquo; throttling where clocks drop
      for no visible reason. <b>Disabling FIT is the single most useful
      diagnostic step</b> when investigating unexplained clock drops.</li>
</ul>

<p><b>Other throttlers (bits 19–20):</b></p>
<ul>
  <li><b>19 — GFX_APCC_Plus</b> — Adaptive Power Control Circuit Plus. A
      firmware-driven power optimization that dynamically adjusts voltage and
      frequency based on real-time power telemetry. Can cause minor clock
      fluctuations under variable workloads.</li>
  <li><b>20 — GFX_DVO</b> — Digital Voltage Optimizer. Adjusts voltage in
      real-time based on silicon-specific characterization data. Firing
      indicates the DVO is actively pulling voltage/frequency down for
      efficiency or reliability.</li>
</ul>

<p><b>Additional read-only metric:</b></p>
<ul>
  <li><b>VmaxThrottlingPercentage</b> — How much the maximum-voltage limiter is
      restricting clocks (0–100%). Shown as a read-only row at the bottom.
      Non-zero indicates the GPU is hitting its voltage ceiling and cannot clock
      higher even if other limits allow it.</li>
</ul>

<h4>SetThrottlerMask (SMU message 0x3A)</h4>
<p>The <code>SetThrottlerMask</code> SMU message takes a 21-bit bitmask where
each <b>set</b> bit means that throttler is <b>enabled</b> (allowed to fire).
Clearing a bit disables the corresponding throttler at the firmware level.</p>
<ul>
  <li>Full mask: <code>0x1FFFFF</code> (all 21 throttlers enabled — stock
      behavior).</li>
  <li>Zero mask: <code>0x000000</code> (all throttlers disabled — no protection
      whatsoever).</li>
</ul>
<p>This is a <b>runtime override only</b> — it resets on driver reload, GPU
reset, or system reboot. It does not persist.</p>

<h4>Memory-Related Throttlers (Bits 4, 7, 8)</h4>
<p>Three throttlers are directly tied to VRAM thermal limits:</p>
<ul>
  <li><b>Bit 4 (Temp_Mem)</b> — GDDR temperature sensor on the memory modules
      themselves.</li>
  <li><b>Bit 7 (Temp_VR_Mem0)</b> — VRM temperature for memory power channel 0.</li>
  <li><b>Bit 8 (Temp_VR_Mem1)</b> — VRM temperature for memory power channel 1.</li>
</ul>
<p>These are highlighted in the table. Disabling them is useful for diagnosing
whether VRAM thermal limits are causing UCLK drops, but you should
<b>monitor VRAM temperatures externally</b> (via the Tables/metrics tab)
while they are disabled.</p>

<h4>Table Columns</h4>
<ul>
  <li><b>Bit</b> — Bit position in the 21-bit throttler mask (0–20).</li>
  <li><b>Name</b> — Human-readable throttler name from the SMU firmware
      header.</li>
  <li><b>Category</b> — Grouping: Thermal, VR Thermal, Current, Power,
      Reliability, or Other. Color-coded for quick scanning.</li>
  <li><b>Live %</b> — Current throttling percentage (0–100) from the latest
      metrics read. <b style="color:#ff4444;">Bold red</b> when non-zero
      (throttler is actively firing).</li>
  <li><b>Enabled</b> — Whether this throttler is currently enabled in the
      mask (ON/OFF).</li>
  <li><b>Toggle</b> — Checkbox to include/exclude this throttler when applying
      a new mask.</li>
  <li><b>Set</b> — Per-row apply button (or use the "Apply Mask" button at the
      bottom to send all toggles at once).</li>
</ul>

<h4>Quick Actions</h4>
<ul>
  <li><b>Apply Mask</b> — Reads all 21 checkboxes, computes the bitmask, and
      sends <code>SetThrottlerMask</code>.</li>
  <li><b>Disable Mem Throttlers</b> — Clears bits 4, 7, 8 and applies. Safe
      first step for diagnosing VRAM clock drops.</li>
  <li><b>Disable FIT</b> — Clears bit 18 and applies. The single most useful
      button for investigating phantom throttling.</li>
  <li><b>Disable All</b> — Sends mask&nbsp;=&nbsp;0x0. Removes all
      protection — use only for short diagnostic sessions with temperature
      monitoring.</li>
  <li><b>Enable All</b> — Sends mask&nbsp;=&nbsp;0x1FFFFF. Restores stock
      behavior.</li>
  <li><b>Refresh</b> — Triggers a metrics read to update the Live % column.</li>
</ul>

<h4>Safety Warnings</h4>
<ul>
  <li><span style="color:#c00;"><b>Disabling thermal throttlers (bits 0–11)
      removes hardware thermal protection.</b></span> The GPU, VRMs, or VRAM can
      physically overheat and sustain permanent damage under sustained load
      without these safeguards. Always monitor temperatures via the Tables tab
      while thermal throttlers are disabled.</li>
  <li><span style="color:#c00;"><b>Disabling all throttlers (mask&nbsp;=&nbsp;0x0)
      is extremely aggressive.</b></span> Use only for short diagnostic sessions.
      Never leave it applied during unattended workloads.</li>
  <li><b>FIT (bit 18) and memory throttlers (bits 4, 7, 8) are the safest to
      disable</b> for testing purposes. FIT protects long-term reliability (not
      immediate safety), and memory thermal limits have generous margins on most
      desktop cards with adequate cooling.</li>
  <li>Power throttlers (PPT0–PPT3) protect the VRM from exceeding its power
      delivery rating. Disabling them may cause VRM overheating or instability
      under extreme power draw.</li>
  <li>Current throttlers (TDC_GFX, TDC_SOC) protect against sustained
      overcurrent. Disabling them risks VRM damage under prolonged heavy load.</li>
  <li>All changes are <b>volatile</b> — they reset on driver reload, GPU reset,
      or system reboot. No permanent damage to firmware settings is possible
      from <code>SetThrottlerMask</code> alone.</li>
</ul>
"""

ESCAPE_OD_HELP_HTML = """
<h3>Escape OD &mdash; D3DKMTEscape OD8 Write</h3>
<p>This tab sends <b>OD8 settings</b> to the GPU driver via the
<code>D3DKMTEscape</code> WDDM interface &mdash; the same mechanism AMD
Adrenalin uses at runtime.  <b>No admin privileges</b> are required.</p>

<h4>How it differs from the OD (SMU) tab</h4>
<table border="1" cellpadding="4" cellspacing="0">
  <tr><th></th><th>OD (SMU) Tab</th><th>Escape OD Tab</th></tr>
  <tr><td>Transport</td><td>DMA + SMU mailbox</td><td>D3DKMTEscape IOCTL</td></tr>
  <tr><td>Admin required?</td><td>Yes</td><td><b>No</b></td></tr>
  <tr><td>Scope</td><td>Full OD table + SMU cmds</td><td>OD8 indices only</td></tr>
  <tr><td>Protocol</td><td>TABLE_OVERDRIVE write</td><td>0x00C000A1 v2 escape</td></tr>
</table>

<h4>How targeting works</h4>
<p>The OD8 escape uses a <b>73-entry indexed array</b> (indices 0&ndash;72).
Each entry carries a <code>(value, is_set)</code> pair.  Setting
<code>is_set=1</code> tells the driver to apply that index;
<code>is_set=0</code> means skip.  This lets you target <b>any individual
index or combination</b> in a single escape call.</p>

<p>Click <b>Read</b> to fetch all 73 current values from the driver.
Use a per-row <b>Set</b> button to write one index, or
<b>Apply All Modified</b> to write every row whose input differs from the
current driver value.</p>

<h4>Confidence tags</h4>
<ul>
  <li><b>[F]</b> Frida-confirmed &mdash; captured from live Adrenalin traffic,
      ground truth.</li>
  <li><b>[G]</b> Ghidra-confirmed &mdash; found in decompiled driver handler
      code.</li>
  <li><b>[I]</b> Inferred &mdash; structural analysis + Linux kernel
      cross-reference; less certain.</li>
</ul>

<h4>Volatility</h4>
<p>All OD8 changes are <b>volatile</b> &mdash; lost on reboot or driver
reload, just like the OD (SMU) tab.</p>

<h4>Reset to Defaults</h4>
<p>The <b>Reset</b> button sends index 71 (ResetFlag) with value&nbsp;1.
The driver treats this as a signal to revert all OD settings to their
power-on defaults.</p>
"""
