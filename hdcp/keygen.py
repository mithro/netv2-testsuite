#!/usr/bin/env python3
"""Self-consistent HDCP 1.x key system for a CLOSED loop we fully own
(RPi source <-> NeTV2 sink). No leaked master key needed.

We act as our own key authority: pick a random SYMMETRIC 40x40 matrix M of
56-bit values as the private master secret. A device with 40-bit KSV v gets
device private keys  keys = M . v  (each key k = sum of M[k][i] over set bits i
of v, mod 2^56). Because M is symmetric, the HDCP shared secret

    Km = sum(A_keys[j] for j in setbits(KSV_B))  ==  KSV_A^T M KSV_B
       = sum(B_keys[i] for i in setbits(KSV_A))

is identical on both ends -> authentication interoperates. Legally cleaner than
the leaked master key, and sufficient because both endpoints are ours.

Emits <out>/master.bin, source_keys.bin, sink_keys.bin (40 x 7-byte LE keys)
plus a JSON manifest with the KSVs. source_keys.bin -> the Pi's
vc4_hdcp_keys.bin; sink_keys.bin -> the NeTV2 gateware/firmware.
"""
import json
import os
import secrets
import sys

MASK = (1 << 56) - 1
N = 40


def balanced_ksv(rng=secrets):
    # 40-bit value with exactly 20 ones
    bits = [1] * 20 + [0] * 20
    # Fisher-Yates with a CSPRNG
    for i in range(39, 0, -1):
        j = rng.randbelow(i + 1)
        bits[i], bits[j] = bits[j], bits[i]
    v = 0
    for i, b in enumerate(bits):
        if b:
            v |= (1 << i)
    return v


def gen_master():
    M = [[0] * N for _ in range(N)]
    for i in range(N):
        for j in range(i, N):
            x = secrets.randbits(56)
            M[i][j] = x
            M[j][i] = x
    return M


def device_keys(M, ksv):
    setbits = [i for i in range(N) if (ksv >> i) & 1]
    return [sum(M[k][i] for i in setbits) & MASK for k in range(N)]


def km(keys, other_ksv):
    return sum(keys[j] for j in range(N) if (other_ksv >> j) & 1) & MASK


def write_keys(path, keys):
    with open(path, "wb") as f:
        for k in keys:
            f.write(int(k).to_bytes(7, "little"))


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "hdcp/keys"
    os.makedirs(out, exist_ok=True)
    M = gen_master()
    ksv_src = balanced_ksv()
    ksv_snk = balanced_ksv()
    src = device_keys(M, ksv_snk)   # source sums its keys over the SINK's KSV
    snk = device_keys(M, ksv_src)   # sink sums its keys over the SOURCE's KSV
    # NOTE: A_keys = M·KSV_A actually; Km_src = sum(A_keys[j] for j in KSV_B).
    # Build each side's keys from ITS OWN ksv:
    src = device_keys(M, ksv_src)
    snk = device_keys(M, ksv_snk)
    km_src = km(src, ksv_snk)       # source: sum own keys over sink KSV
    km_snk = km(snk, ksv_src)       # sink:   sum own keys over source KSV
    assert km_src == km_snk, "Km mismatch! (matrix not symmetric?)"

    write_keys(os.path.join(out, "source_keys.bin"), src)
    write_keys(os.path.join(out, "sink_keys.bin"), snk)
    with open(os.path.join(out, "manifest.json"), "w") as f:
        json.dump({
            "ksv_source": "0x%010x" % ksv_src,
            "ksv_sink": "0x%010x" % ksv_snk,
            "km_agreed": "0x%014x" % km_src,
            "note": "source_keys.bin -> Pi vc4_hdcp_keys.bin; sink_keys.bin -> NeTV2",
        }, f, indent=2)
    print("KSV_source = 0x%010x (%d ones)" % (ksv_src, bin(ksv_src).count("1")))
    print("KSV_sink   = 0x%010x (%d ones)" % (ksv_snk, bin(ksv_snk).count("1")))
    print("Km agreed  = 0x%014x  (source==sink: %s)" % (km_src, km_src == km_snk))
    print("wrote", out + "/{source_keys,sink_keys}.bin (280 bytes each), manifest.json")


if __name__ == "__main__":
    main()
