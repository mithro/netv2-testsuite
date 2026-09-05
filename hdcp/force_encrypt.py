#!/usr/bin/env python3
"""Attempt to force the BCM2835 HDCP engine into encrypting state, no real sink.

Uses the CP_TST test path: external An (TST_AN), force-key-valid, and
HDCP_CTL.I_SET_RDB_AUTHENTICATED. Loads 40 deterministic test keys and a balanced
fake BKSV, computes the resulting Km = sum(keys[i] for BKSV bit i set) mod 2^56
(to be loaded into the NeTV2 decryptor's Km CSR).

Modes: probe | encrypt | revert
Registers per hdcp/REGISTERS.md. Writes only HDCP registers vc4 never touches.
"""
import mmap
import os
import struct
import sys
import time

PERI = 0x20000000
PAGE = 4096
KEYLOADER_BUS = 0x7E809000
CORE_BUS = 0x7E902000

# core offsets
BKSV0, BKSV1 = 0x10, 0x14
AN0, AN1 = 0x18, 0x1C
TST_AN0, TST_AN1 = 0x28, 0x2C
HDCP_KEY_1, HDCP_KEY_2 = 0x3C, 0x40
HDCP_CTL, CP_STATUS, CP_INTEGRITY, CP_CONFIG, CP_TST = 0x44, 0x48, 0x4C, 0x54, 0x58
# key loader offsets
K_CTL, K_ADR, K_KY0, K_KY1 = 0x00, 0x04, 0x08, 0x0C
# fields
CTL_AUTH_REQ, CTL_CLR_RDB, CTL_SET_RDB, CTL_FORCE_UNAUTH, CTL_RESET_KU = 1, 2, 4, 8, 1 << 16
ST_AN_READY, ST_BKSV_VALID, ST_RDB_AUTH, ST_CORE_AUTH, ST_AUTH_OK, ST_HDCP_READY = 1, 2, 4, 8, 0x10, 1 << 31
CFG_ENABLE_RDB_KEY_LOAD, CFG_ENABLE_KU = 1 << 10, 1 << 19
TST_MODE_AN, TST_EXT_AN, TST_FORCE_KEY_VALID = 1 << 6, 1 << 7, 1 << 8

KEYS = [((0x123456789ABCDE * (i + 1)) & 0x00FFFFFFFFFFFFFF) for i in range(40)]
BKSV = 0xAAAAAAAAAA  # 40 bits, exactly 20 ones (even? 0xA=1010 -> bits 1,3 per nibble => 20 ones)


def km_for(bksv, keys):
    s = 0
    for i in range(40):
        if (bksv >> i) & 1:
            s = (s + keys[i]) & 0x00FFFFFFFFFFFFFF
    return s


class Blk:
    def __init__(self, fd, bus):
        self.base = ((bus & 0xFFFFFF) | PERI) & ~(PAGE - 1)
        self.m = mmap.mmap(fd, PAGE, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=self.base)

    def rd(self, off):
        self.m.seek(off); return struct.unpack("<I", self.m.read(4))[0]

    def wr(self, off, val):
        self.m.seek(off); self.m.write(struct.pack("<I", val & 0xFFFFFFFF))


def decode_status(v):
    b = []
    for name, m in [("AN_READY", ST_AN_READY), ("BKSV_VALID", ST_BKSV_VALID),
                    ("RDB_AUTH", ST_RDB_AUTH), ("CORE_AUTH", ST_CORE_AUTH),
                    ("AUTH_OK", ST_AUTH_OK), ("HDCP_READY", ST_HDCP_READY)]:
        if v & m:
            b.append(name)
    return " ".join(b) if b else "(none)"


def snap(core, tag):
    s = core.rd(CP_STATUS)
    print("  [%s] CP_STATUS=0x%08x {%s}  HDCP_CTL=0x%08x CP_CONFIG=0x%08x CP_TST=0x%08x CP_INTEG=0x%08x AN=%08x:%08x"
          % (tag, s, decode_status(s), core.rd(HDCP_CTL), core.rd(CP_CONFIG), core.rd(CP_TST),
             core.rd(CP_INTEGRITY), core.rd(AN1), core.rd(AN0)))
    return s


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "probe"
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    core = Blk(fd, CORE_BUS)
    keyl = Blk(fd, KEYLOADER_BUS)

    km = km_for(BKSV, KEYS)
    print("computed Km = 0x%014x  (BKSV=0x%010x, %d ones)" % (km, BKSV, bin(BKSV).count("1")))

    if mode == "probe":
        snap(core, "now")
    elif mode == "revert":
        core.wr(CP_TST, 0)
        core.wr(HDCP_CTL, CTL_FORCE_UNAUTH | CTL_CLR_RDB)
        core.wr(HDCP_CTL, 0)
        core.wr(CP_CONFIG, core.rd(CP_CONFIG) & ~CFG_ENABLE_KU)
        snap(core, "reverted")
    elif mode == "encrypt4":
        ST_CORE_AUTH = 1 << 3
        CFG_RDB_KEY_LOAD = 1 << 10
        SCHED, ENC_ONLY_WHEN_AUTH = 0xC0, 1 << 6
        TST_MODE_AN, TST_EXT_AN, TST_FORCE_KEY_VALID = 1 << 6, 1 << 7, 1 << 8
        snap(core, "before")
        core.wr(SCHED, core.rd(SCHED) | ENC_ONLY_WHEN_AUTH)
        core.wr(TST_AN0, 0); core.wr(TST_AN1, 0)
        core.wr(CP_TST, TST_FORCE_KEY_VALID | TST_MODE_AN | TST_EXT_AN)
        core.wr(BKSV0, BKSV & 0xFFFFFFFF); core.wr(BKSV1, (BKSV >> 32) & 0xFF)
        # set key base = 0
        core.wr(CP_CONFIG, core.rd(CP_CONFIG) & ~0x3FF)
        # load keys via BOTH the 0x809000 loader (addr 0..39) and RDB (base 0)
        for n in range(40):
            keyl.wr(K_ADR, n); keyl.wr(K_KY0, KEYS[n] & 0xFFFFFFFF)
            keyl.wr(K_KY1, (KEYS[n] >> 32) & 0xFFFFFF); keyl.wr(K_CTL, 1)
        core.wr(CP_CONFIG, core.rd(CP_CONFIG) | CFG_RDB_KEY_LOAD)
        for n in range(40):
            k = KEYS[n]
            core.wr(HDCP_KEY_2, (k >> 24) & 0xFFFFFFFF)
            core.wr(HDCP_KEY_1, ((k & 0xFFFFFF) << 8) | n)
        core.wr(CP_CONFIG, core.rd(CP_CONFIG) & ~CFG_RDB_KEY_LOAD)
        core.wr(CP_CONFIG, core.rd(CP_CONFIG) | CFG_ENABLE_KU)
        snap(core, "keys(both)+forcekey+KU")
        core.wr(HDCP_CTL, CTL_AUTH_REQ)
        got = -1
        for i in range(80):   # up to ~2s
            st = core.rd(CP_STATUS)
            if st & ST_CORE_AUTH:
                got = i; break
            time.sleep(0.025)
        snap(core, "after AUTH_REQUEST (core_auth@%d)" % got)
        ri = [core.rd(CP_INTEGRITY) for _ in range(4)]
        for _ in range(3):
            time.sleep(0.6); ri.append(core.rd(CP_INTEGRITY))
        print("  CP_INTEGRITY samples:", " ".join("0x%08x" % x for x in ri))
        print("  O_RI advancing:", len(set(ri)) > 1)
    elif mode == "encrypt3":
        # authoritative sequence: RDB key load -> ENABLE_KU -> I_AUTH_REQUEST
        ST_CORE_AUTH = 1 << 3
        CFG_RDB_KEY_LOAD = 1 << 10
        SCHED, ENC_ONLY_WHEN_AUTH = 0xC0, 1 << 6
        base = core.rd(CP_CONFIG) & 0x3FF   # keep current I_KEY_BASE_ADDRESS (0x80)
        snap(core, "before")
        # arm encrypt gate
        core.wr(SCHED, core.rd(SCHED) | ENC_ONLY_WHEN_AUTH)
        # fake sink BKSV (transmitter sums its keys at BKSV bit positions)
        core.wr(BKSV0, BKSV & 0xFFFFFFFF); core.wr(BKSV1, (BKSV >> 32) & 0xFF)
        # RDB software key load into the cipher key RAM at I_KEY_BASE_ADDRESS
        cfg = core.rd(CP_CONFIG)
        core.wr(CP_CONFIG, (cfg & ~0x3FF) | base | CFG_RDB_KEY_LOAD)
        for n in range(40):
            k = KEYS[n]
            core.wr(HDCP_KEY_2, (k >> 24) & 0xFFFFFFFF)
            core.wr(HDCP_KEY_1, ((k & 0xFFFFFF) << 8) | n)
        core.wr(CP_CONFIG, core.rd(CP_CONFIG) & ~CFG_RDB_KEY_LOAD)
        # arm Ku (block-cipher key) computation
        core.wr(CP_CONFIG, core.rd(CP_CONFIG) | CFG_ENABLE_KU)
        snap(core, "keys loaded + KU armed")
        # THE trigger: real authentication -> runs block cipher
        core.wr(HDCP_CTL, CTL_AUTH_REQ)
        # poll for CORE_AUTHENTICATED
        for i in range(40):
            st = core.rd(CP_STATUS)
            if st & ST_CORE_AUTH:
                break
            time.sleep(0.025)
        snap(core, "after AUTH_REQUEST (polled %d)" % i)
        an = (core.rd(AN1) << 32) | core.rd(AN0)
        # watch O_RI[31:16] for advancement = cipher really running
        ri0 = core.rd(CP_INTEGRITY)
        time.sleep(0.5)
        ri1 = core.rd(CP_INTEGRITY)
        print("  An=0x%016x  Km(computed)=0x%014x" % (an, km))
        print("  CP_INTEGRITY: 0x%08x -> 0x%08x  (O_RI %s)"
              % (ri0, ri1, "ADVANCING" if ri0 != ri1 else "static"))
    elif mode == "encrypt2":
        CP_INTEGRITY_CFG = 0x50
        ALWAYS_REKEY = 1 << 16
        snap(core, "before")
        for i in range(40):
            keyl.wr(K_ADR, i); keyl.wr(K_KY0, KEYS[i] & 0xFFFFFFFF)
            keyl.wr(K_KY1, (KEYS[i] >> 32) & 0xFFFFFF); keyl.wr(K_CTL, 1)
        core.wr(BKSV0, BKSV & 0xFFFFFFFF); core.wr(BKSV1, (BKSV >> 32) & 0xFF)
        core.wr(CP_CONFIG, core.rd(CP_CONFIG) | CFG_ENABLE_KU)
        # configure per-frame rekey on vsync
        core.wr(CP_INTEGRITY_CFG, core.rd(CP_INTEGRITY_CFG) | ALWAYS_REKEY | 0x0040)  # I_RATE=0x40
        snap(core, "after keys+BKSV+KU+rekey")
        # real auth request -> generates An, computes Km/Ks (inits block cipher)
        core.wr(HDCP_CTL, CTL_AUTH_REQ)
        time.sleep(0.1)
        s1 = snap(core, "after AUTH_REQUEST")
        an = (core.rd(AN1) << 32) | core.rd(AN0)
        print("  generated An = 0x%016x" % an)
        # now declare authenticated to start encryption without sink R0
        core.wr(HDCP_CTL, CTL_SET_RDB)
        time.sleep(0.15)
        snap(core, "after SET_RDB")
        # engage the scheduler's encrypt-when-authenticated gate
        SCHED = 0xC0
        ENC_ONLY_WHEN_AUTH = 1 << 6
        sc = core.rd(SCHED)
        core.wr(SCHED, sc | ENC_ONLY_WHEN_AUTH)
        time.sleep(0.15)
        print("  SCHEDULER_CONTROL 0x%08x -> 0x%08x" % (sc, core.rd(SCHED)))
        snap(core, "after SCHED.ENC_ONLY_WHEN_AUTH")
        print("  NOTE: for NeTV2 decode, An=0x%016x  Km=0x%014x" % (an, km))
    elif mode == "encrypt":
        snap(core, "before")
        # 1. load 40 keys via the 0x809000 key loader
        for i in range(40):
            keyl.wr(K_ADR, i)
            keyl.wr(K_KY0, KEYS[i] & 0xFFFFFFFF)
            keyl.wr(K_KY1, (KEYS[i] >> 32) & 0xFFFFFF)
            keyl.wr(K_CTL, 1)  # START
        print("  loaded 40 keys")
        # 2. inject fake sink BKSV
        core.wr(BKSV0, BKSV & 0xFFFFFFFF)
        core.wr(BKSV1, (BKSV >> 32) & 0xFF)
        snap(core, "after BKSV")
        # 3. external An = 0
        core.wr(TST_AN0, 0); core.wr(TST_AN1, 0)
        core.wr(CP_TST, TST_MODE_AN | TST_EXT_AN | TST_FORCE_KEY_VALID)
        snap(core, "after CP_TST(extAn|forceKey)")
        # 4. enable Ku computation
        core.wr(CP_CONFIG, core.rd(CP_CONFIG) | CFG_ENABLE_KU)
        time.sleep(0.05)
        snap(core, "after ENABLE_KU")
        # 5. reset Ku then force authenticated
        core.wr(HDCP_CTL, CTL_RESET_KU)
        core.wr(HDCP_CTL, 0)
        time.sleep(0.05)
        snap(core, "after RESET_KU")
        core.wr(HDCP_CTL, CTL_SET_RDB)
        time.sleep(0.1)
        snap(core, "after SET_RDB_AUTHENTICATED")
    os.close(fd)


if __name__ == "__main__":
    main()
