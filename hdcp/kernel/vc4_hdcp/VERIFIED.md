# vc4_hdcp module — built & run on rpiz-3 (6.18.39+rpt-rpi-v6)

Built clean against the running kernel headers; loaded; drove the real HDCP
registers via ioremap/readl/writel. Output:

```
vc4_hdcp: loaded; CORE_REV=0x00000600
# echo 1 > /sys/kernel/debug/vc4_hdcp/start
CP_STATUS 0x80000000 -> 0x80000003 [AN_READY BKSV_VALID HDCP_READY]
CP_CONFIG 0x00130080 -> 0x001b0000 (ENABLE_KU set)
SCHED_CTL 0x000cb02b -> 0x000cb06b (ENC_ONLY_WHEN_AUTH set)
AN = 0x0b977ac5dc306f21   Km(computed) = 0xdddddddddddc38
core_auth@-1 -> stalled before CORE_AUTHENTICATED (needs responding HDCP sink)
```

This reproduces the python reference exactly and confirms the kernel-space
transmitter sequence is correct up to the sink-dependent DDC handshake.
Ignore the .gitignore'd build artifacts (*.ko, *.o, *.mod*, .*.cmd, Module.symvers).
