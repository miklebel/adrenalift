"""
HTML help / cheatsheet strings for the overclock GUI.

Extracted from main.py to keep the UI module focused on layout and logic.
"""

from __future__ import annotations

SIMPLE_HOW_IT_WORKS_HTML = """
<h3>How PP Table RAM Patching Works</h3>
<p>This tool uses a <b>multi-layer defense-in-depth</b> approach to keep your
overclock settings alive despite the Windows AMD driver constantly trying to
re-impose stock limits.</p>

<h4>The Problem</h4>
<p>The AMD GPU driver keeps a cached copy of the <b>PowerPlay (PP) table</b>
in system RAM. Whenever the driver re-evaluates power management &mdash;
on workload changes, thermal events, display mode switches, AC/DC transitions
&mdash; it reads from this cached table, derives the "allowed" clock and power
limits, and sends them to the SMU firmware. This <b>overwrites any direct SMU
commands</b> you may have sent.</p>

<h4>Layer 1 &mdash; PP Table RAM Patch (this tool's scan &amp; patch)</h4>
<p>Scans physical memory for the driver's cached PP table copies and overwrites
clock limits (GameClockAc, BoostClockAc) and MsgLimits (PPT, TDC, temps)
directly in RAM. This <b>poisons the driver's own data source</b> so that when
the driver re-derives limits, it sends your higher values instead of stock.</p>
<ul>
  <li>Without the RAM patch, the driver would periodically re-send stock max
      clocks to the SMU, undoing your overclock.</li>
  <li>With it, the driver unknowingly enforces <i>your</i> limits every time
      it re-evaluates.</li>
</ul>

<h4>Layer 2 &mdash; Direct SMU Messages (sent at the same time)</h4>
<p>After patching RAM, the tool sends SMU commands directly
(<code>SetSoftMaxByFreq</code>, <code>SetHardMaxByFreq</code>,
<code>SetPptLimit</code>, etc.). These take effect <b>immediately</b> on the
SMU firmware, setting clocks and power limits right now &mdash; without waiting
for the driver to re-evaluate.</p>
<ul>
  <li><b>Clock gating features</b> (DS_GFXCLK, GFX_ULV, GFXOFF) are disabled
      via <code>DisableSmuFeaturesLow</code>.</li>
  <li><code>DisallowGfxOff</code> prevents the GPU from entering sleep.</li>
  <li>A workload-mask cycle (PowerSave &rarr; 3D Fullscreen) forces the SMU to
      re-evaluate DPM with the new limits.</li>
</ul>

<h4>Layer 3 &mdash; OD Table (percentage-based, via SMU table transfer)</h4>
<p>The OverDrive table sets percentage offsets (PPT%, TDC%, GfxclkFoffset) via
the official <code>TransferTableDram2Smu</code> protocol. The SMU applies these
on top of the base limits from the PP table &mdash; so the RAM patch and OD
table multiply together.</p>

<h4>Layer 4 &mdash; Registry Anti-Clock-Gating (persistent)</h4>
<p>Registry patches disable the driver's own power management policies
(ULPS, GPU power-down, UVD/VCE clock gating, ASPM, clock stretcher).
Unlike RAM patches, these <b>survive reboots</b> and prevent the driver from
re-enabling power-saving features at the driver policy level.</p>

<h4>Layer 5 &mdash; Watchdog (continuous enforcement)</h4>
<p>The watchdog timer periodically re-sends min-frequency floors and feature
disables to catch any driver re-enables that slip through.</p>

<h4>Why All Layers Are Needed</h4>
<table border="1" cellpadding="4" cellspacing="0">
<tr><th>Mechanism</th><th>Purpose</th><th>Persistent?</th></tr>
<tr><td>PP Table RAM Patch</td><td>Prevents driver from re-sending stock limits</td>
    <td>Until reboot</td></tr>
<tr><td>Direct SMU Messages</td><td>Immediately sets clocks/power/features</td>
    <td>Until driver overrides</td></tr>
<tr><td>OD Table Commit</td><td>Percentage offset on top of base limits</td>
    <td>Until driver resets OD</td></tr>
<tr><td>Registry Patches</td><td>Disables driver power management policies</td>
    <td>Across reboots</td></tr>
<tr><td>Watchdog</td><td>Re-enforces floor periodically</td>
    <td>While app is running</td></tr>
</table>

<p><b>Bottom line:</b> SMU messages alone are not enough because the driver will
overwrite them. The PP table RAM patch ensures the driver works <i>for</i> you
rather than <i>against</i> you. Both are needed for a robust overclock.</p>
"""

PP_HELP_HTML = """
<h3>PP &mdash; PowerPlay Table RAM Patching</h3>
<p>The PP tab patches the driver&rsquo;s in-memory copies of the
<b>PowerPlay (PP) table</b> at physical addresses discovered by <b>Scan</b>.
This directly changes the limits the driver reads when configuring the GPU.</p>

<h4>Clocks &amp; Power rows</h4>
<p>For clock and power-limit rows (Game Clock, Boost Clock, PPT, TDC, etc.),
Apply PP does <b>two things</b>:</p>
<ol>
  <li><b>RAM patch</b> &mdash; overwrites the field in every discovered PP table
      copy so the driver sees your value.</li>
  <li><b>SMU commands</b> &mdash; sends <code>SetSoftMinByFreq</code>,
      <code>SetSoftMaxByFreq</code>, <code>SetHardMinByFreq</code>,
      <code>SetHardMaxByFreq</code>, <code>DisallowGfxOff</code>, and
      workload-mask cycling to the SMU so the firmware enforces the new
      limits immediately.</li>
</ol>

<h4>MsgLimits rows</h4>
<p>For power-limit fields (PPT), Apply PP also sends
<code>SetPptLimit</code> to the SMU in addition to the RAM patch.</p>

<h4>Custom PP fields (fan, voltage, freq, board)</h4>
<p>Rows in the lower &ldquo;custom&rdquo; section are <b>RAM-only</b>: they
patch the PP table bytes in driver memory but do <i>not</i> send any SMU
commands. The driver picks up the new values on its next read of the table.</p>

<h4>Prerequisites &amp; Volatility</h4>
<ul>
  <li>A successful <b>Scan</b> is required before Apply PP can locate the PP
      table copies in physical memory.</li>
  <li>All changes are <b>volatile</b> &mdash; lost on reboot, driver reload, or
      GPU reset.</li>
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

TABLES_CHEATSHEET = """
<h3>Tables — Live metrics &amp; raw SMU table dumps</h3>
<p>This tab reads raw data tables from the SMU's DMA buffer. The top section shows
parsed live performance metrics; the bottom section lets you dump raw tables as hex.</p>

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

<h4>UI controls</h4>
<ul>
  <li><b>Refresh Now</b> — One-shot read of the full SmuMetrics_t struct.</li>
  <li><b>Auto-refresh checkbox</b> — Enables a periodic timer.</li>
  <li><b>Interval spinbox (1–30 s)</b> — Sets the auto-refresh period. Adjustable while
      auto-refresh is running.</li>
  <li><b>Status label</b> — Shows last update timestamp and value count, or error text.</li>
  <li><b>Hex view</b> (bottom) — Read-only text area displaying raw hex dumps from the
      "Other Tables" buttons.</li>
</ul>

<h4>Caveats</h4>
<ul>
  <li>Metrics are sampled by the SMU firmware at its own internal rate, not at your refresh
      interval. Polling faster than ~2 s adds MMIO overhead with diminishing returns.</li>
  <li>Some metric values are <i>averaged</i> over the SMU's sampling window, not
      instantaneous snapshots. "Pre/PostDs" frequency differences reveal deep-sleep
      duty cycle, not instantaneous jitter.</li>
  <li>Raw table hex dumps require knowledge of the struct layout to interpret. The PPTable
      format is GPU-generation-specific.</li>
  <li>Reading tables while the GPU is under heavy load may briefly stall the SMU command
      interface.</li>
  <li>D3Hot counters and APU/STAPM fields are typically zero on desktop GPUs.</li>
  <li>Throttler names (Temp_Edge, PPT0, TDC_GFX, etc.) are from the SMU v14.0 firmware
      header (smu14_driver_if_v14_0.h). Values are 0-100% activity percentage.</li>
  <li>PublicSerialNumber fields may be zero if the GPU vendor has not programmed a serial.</li>
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

DIAG_VRAM_DUMP_HTML = """
<h3>VRAM Dump — Full BAR Snapshot</h3>
<p>This tool reads the entire GPU VRAM BAR (Base Address Register) aperture through
physical memory mapping and saves it as a <b>gzip-compressed</b> <code>.bin.gz</code>
file. A small JSON sidecar (<code>.meta.json</code>) is written alongside with
hardware metadata.</p>

<h4>What gets captured</h4>
<ul>
  <li>The driver's cached <b>PPTable</b> copies</li>
  <li><b>SMU metrics</b> data (two fresh transfers are triggered before the dump)</li>
  <li>The <b>DMA buffer</b> used for SMU table transfers</li>
  <li>Any other data visible in the BAR aperture</li>
</ul>

<h4>When to use</h4>
<ul>
  <li><b>ReBAR debugging</b> &mdash; verify the full BAR is accessible and readable</li>
  <li><b>Developer diagnostics</b> &mdash; send the .bin.gz + .meta.json to the developer
      for offline analysis when something isn't working</li>
  <li><b>Before/after comparison</b> &mdash; dump before and after applying patches to
      verify changes in the BAR</li>
</ul>

<h4>Output files</h4>
<ul>
  <li><code>vram_dump.bin.gz</code> &mdash; Gzip-compressed raw BAR content</li>
  <li><code>vram_dump.meta.json</code> &mdash; JSON with BAR size, addresses, SMU version,
      MMHUB register values, timing, and compressed size</li>
</ul>
"""
