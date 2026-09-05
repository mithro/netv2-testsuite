# Task brief: add an HDCP-1.x RECEIVER to the NeTV2 gateware

**For:** the Claude agent on `desktop.buddy.mithis.com` (NeTV2 gateware work).
**Repo:** `netv2-fpga` (on rpi3-netv2 at `/home/pi/code/netv2-fpga`; also the
AlphamaxMedia netv2-fpga upstream). Coordinating repo for the RPi side +
key tooling: `mithro/netv2` (this repo).

## Why
The RPi-Zero HDCP **transmitter** work is done and verified (see this repo:
`hdcp/` + `docs/HDCP-ON-BCM2835-FEASIBILITY.md`). The Pi's HDCP engine will only
key its cipher after completing a **DDC authentication handshake with a
responding HDCP sink** (it reads the sink's Bksv, writes An+Aksv, verifies R0').
The current NeTV2 gateware is a *passive snooper/decryptor* — it has no receiver
to answer the Pi, so auth stalls at O_AN_READY and no pixels are encrypted.

**Goal:** make the NeTV2 present an HDCP-1.x receiver on its HDMI **input0** DDC
so the Pi authenticates against it, then decode the resulting encrypted video
(the decrypt path already exists — `overlay/hdcp_mod.v`).

## What to build (input0, HDCP DDC I2C address 0x74 / 7-bit 0x3a)
An I2C **slave** responder on the hdmi_in0 DDC (SCL=T18, SDA=V18; the gateware
already has SDA override drivers `hdmi_sda_over_up`=G20 / `hdmi_sda_over_dn`=F20,
and a passive `i2c_snoop.v`). It must implement the HDCP receiver register map:
- `0x00` BKSV (5B, LE): the sink KSV (KSV_sink from keygen; 20 ones/20 zeros).
- `0x08` Ri' (2B): current link-integrity value from the cipher.
- `0x10` Aksv (5B): written by the source.
- `0x18` An (8B): written by the source (the snooper already extracts An; reuse).
- `0x15` Ainfo (1B); `0x40` Bcaps (1B, REPEATER=0, FAST=0); `0x41` Bstatus (2B).
- On the last Aksv byte write -> trigger auth: compute
  `Km = sum(sink_keys[i] for i in setbits(Aksv))` (mod 2^56), init the block
  cipher (reuse `overlay/hdcp_cipher.v`) with (Km, An) -> Ks, R0; expose R0 at
  `0x08` within 100 ms. Re-expose Ri every 128 frames for link checks.
- Feed the same (Km, An) to the existing `hdcp_mod` decryptor so the composited
  output is decrypted (today Km is a CSR; wire it from the receiver's computed Km,
  or keep the CSR path and have the CPU program the computed Km).

## Keys (no leaked master key needed — closed loop we own)
Use `hdcp/keygen.py` in the mithro/netv2 repo: it makes a random SYMMETRIC 40x40
56-bit master matrix and derives interoperable device key sets. Because M is
symmetric, source and sink agree on Km (verified: `Km` matches). It emits:
- `source_keys.bin` (40x7B LE) + KSV_source  -> the Pi (`vc4_hdcp_keys.bin`).
- `sink_keys.bin`   (40x7B LE) + KSV_sink    -> the NeTV2 receiver.
The NeTV2 receiver returns KSV_sink as Bksv and uses sink_keys for Km. The Pi
uses KSV_source as Aksv and source_keys. Run keygen once; share the manifest.json
(KSVs) between both sides. **Do not commit key .bin files.**

## Reuse / integration points (netv2mvp.py + overlay/)
- `overlay/hdcp_cipher.v`, `hdcp_lfsr.v`, `hdcp_block.v` — the HDCP cipher (already
  present; used by the decryptor). The receiver's R0/Ri computation is the same
  cipher init — reuse it.
- `overlay/i2c_snoop.v` — already recovers An + the Aksv-complete strobe from the
  DDC; extend or parallel it with an actual I2C slave that ACKs 0x74 and drives
  responses via the SDA override.
- `netv2mvp.py`: the `HDCP` and `I2Csnoop` submodules and the hdmi_in0 wiring
  (~lines 619-720, 1100-1126). Add the receiver submodule + CSRs (sink KSV, sink
  keys load, R0/Ri readback, auth status).
- Add CSRs so the CPU can load `sink_keys` and read auth state (mirror the
  existing `Km`/`Km_valid`/`Aksv_mode` CSR style).

## Definition of done
1. Pi (with the vc4 HDCP patch + source keys) runs AUTH_REQUEST and reaches
   CORE_AUTHENTICATED (its O_RI advances) — i.e. the NeTV2 answered the handshake.
2. The Pi emits HDCP-encrypted video (a non-decoding capture shows noise).
3. The NeTV2 decrypts it (capture shows the clean source image again).
Verification tooling (capture entropy clean~4 vs noise~8 bits) is in this repo:
`hdcp/netv2_capture_stats.py`. Coordinate KSVs/keys via keygen manifest.

## Caution
Rebuilding + reflashing the NeTV2 bitstream over JTAG carries brick risk; keep a
known-good bitstream and the recovery/JTAG procedure (`~/alphamax-rpi.cfg` on
rpi3-netv2) ready before flashing.
