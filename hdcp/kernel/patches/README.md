# vc4 HDCP-output patch set (Linux 6.18.y / raspberrypi-linux)

Adds HDCP 1.x **transmitter** support to the vc4 DRM driver for the VideoCore-IV
HDMI block (BCM2835 — Pi Zero/1). Exposes the standard DRM "Content Protection"
connector property and drives the Broadcom HDCP cipher/auth engine.

Apply against `drivers/gpu/drm/vc4/` in a raspberrypi/linux rpi-6.18.y tree:
```
cd linux
git am .../hdcp/kernel/patches/0001-*.patch     # new file vc4_hdcp.c
git apply .../hdcp/kernel/patches/0002-*.patch   # struct fields + prototypes
git apply .../hdcp/kernel/patches/0003-*.patch   # connector_init + atomic_check hooks
git apply .../hdcp/kernel/patches/0004-*.patch   # Makefile (build vc4_hdcp.o)
```
(0001 is a clean add via `git am`; 0002-0004 are context diffs — use `git apply`.)

## Status / provenance
- The **register sequence** implemented here (RDB key load -> ENABLE_KU ->
  I_AUTH_REQUEST -> poll CORE_AUTHENTICATED -> ENC_ONLY_WHEN_AUTH gate + the ~2s
  O_RI watchdog) is **verified on real hardware** by the out-of-tree module in
  `../vc4_hdcp/` (built & loaded on rpiz-3, 6.18.39): it drives the identical
  registers and reaches the expected CORE_AUTHENTICATED stall pending a sink.
- **Not yet built in-tree** (needs a full kernel build) and **not yet run to
  encrypted output** (needs a responding downstream HDCP sink for the DDC
  handshake — see ../FINDINGS.md). Treat as review-ready source pending those two.

## Device keys
HDCP device keys are supplied out-of-band as firmware `vc4_hdcp_keys.bin`
(40 little-endian 7-byte device keys = 280 bytes). Without it, the property is
attached but HDCP output stays disabled. Key material is the integrator's
responsibility (see ../../docs/HDCP-ON-BCM2835-FEASIBILITY.md, key-material
section).

## Remaining wiring (documented, needs sink to validate)
- Read the sink's Bksv over the HDCP DDC (0x74) and pass it to
  vc4_hdcp_atomic_enable() from the encoder-enable path (currently the caller
  must supply sink_bksv). With the NeTV2-as-sink gateware (in progress on
  desktop.buddy.mithis.com) or a real HDCP display, this closes the loop.
