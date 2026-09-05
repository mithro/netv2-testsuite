# HDCP-output bring-up on rpiz-3 (BCM2835) — findings & blocker

## Proven on real hardware
1. Both HDCP register blocks respond (not power/clock/OTP gated): key-loader
   `0x7e809000` and cipher/auth engine `0x7e902000` (HDCP_READY set; HSM clk 163MHz;
   OTP HDCP key rows blank).
2. Full register/field map recovered & cross-checked (3 sources). See REGISTERS.md.
3. Key loading works (both the 0x809000 START/DONE loader and the RDB path
   CP_CONFIG.I_ENABLE_RDB_KEY_LOAD + HDCP_KEY_1/2).
4. Writing a balanced fake BKSV asserts O_BKSV_VALID.
5. AUTH_REQUEST generates a session An (AN0/1, AN_READY asserts).

## The exact intended encryption sequence (from Broadcom STB refsw semantics)
Two distinct auth bits: CORE_AUTHENTICATED[3] (hardware cipher computed Km->Ks->R0)
vs RDB_AUTHENTICATED[2] (pure register override); AUTHENTICATED_OK[4] = OR of them.
Forcing RDB only sets the flag; it never runs the cipher (O_RI stays static) -> no
real encryption. The real path:
```
SCHEDULER_CONTROL.MODE_ACTIVE==1                 # HDMI up (vc4 already does this)
SCHEDULER_CONTROL |= ENC_ONLY_WHEN_AUTH          # gate: encrypt while authenticated
CP_CONFIG: I_KEY_BASE_ADDRESS=base; |= I_ENABLE_RDB_KEY_LOAD
for n in 0..39: HDCP_KEY_2=key_hi32; HDCP_KEY_1=(key_lo24<<8)|n
CP_CONFIG &= ~I_ENABLE_RDB_KEY_LOAD; |= I_ENABLE_KU_COMPUTATION
HDCP_CTL = I_AUTH_REQUEST                         # runs block cipher
poll CP_STATUS: O_AN_READY -> O_BKSV_VALID -> CORE_AUTHENTICATED & AUTHENTICATED_OK
verify CP_INTEGRITY.O_RI[31:16] advancing         # cipher really encrypting
```

## THE BLOCKER (architectural, needs a decision)
On real hardware the sequence **stalls at AN_READY**: CORE_AUTHENTICATED never
asserts and O_RI never advances, across every variant tried (both key paths, base 0,
force-key-valid, external An, 2s poll). Reason: the BCM HDCP auth state machine
completes only after its **DDC handshake with a responding HDCP sink** (write
An+Aksv, read the sink's Bksv/R0' at DDC 0x74). **The rig has no HDCP sink:**
- MS2109 capture card: not HDCP.
- NeTV2: a *snooper/decryptor* — `i2c_snoop` is passive; `hdcp_mod` decrypts; it has
  SDA-override pins but presents **no** HDCP-receiver (Bksv/R0') slave and no EDID ROM.
The NeTV2's designed topology is source -> NeTV2(snoop) -> **real HDCP sink**.

## Options to unblock (need user input / hardware or gateware change)
A. Attach a real HDCP HDMI sink (a TV/monitor that does HDCP) downstream of the NeTV2
   output; the Pi authenticates with it and the NeTV2 snoops An to decrypt. (Physical
   recabling — I can't do this remotely.)
B. Make the NeTV2 emulate an HDCP sink on input0 (respond to 0x74 with a chosen Bksv
   and the matching R0' via its SDA-override + CPU). Substantial gateware/firmware work
   + bitstream rebuild/reflash.
C. Investigate a Pi-side "no-DDC / local authentication" path further (not found so far;
   the engine appears to require the DDC exchange).

The Linux-kernel-patch deliverable (implement the sequence above in vc4) is ready to
write, but it is only *verifiable* once a sink (A or B) exists.
