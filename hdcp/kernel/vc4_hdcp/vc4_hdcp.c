// SPDX-License-Identifier: GPL-2.0
/*
 * vc4_hdcp - BCM2835 (VideoCore IV) HDMI HDCP 1.x transmitter bring-up module.
 *
 * Loadable prototype of the HDCP-output sequence for the Raspberry Pi HDMI
 * transmitter, for use as the reference implementation to fold into the vc4
 * DRM driver. Drives the two Broadcom HDCP register blocks directly:
 *   - key-RAM loader   @ VC 0x7e809000 (ARM-phys 0x20809000)
 *   - cipher/auth core @ VC 0x7e902000 (ARM-phys 0x20902000)
 *
 * Register map cross-checked against Broadcom RDB headers (rpi-open-firmware
 * broadcom/bcm2708_chip/{hdcp,hdmicore}.h) and the GPL STB header bchp_hdmi.h.
 *
 * debugfs control (/sys/kernel/debug/vc4_hdcp/):
 *   status  (read)  - dump CP status/config/integrity + key-loader state
 *   start   (write) - load keys -> enable Ku -> AUTH_REQUEST -> poll CORE_AUTH
 *   stop    (write) - force core unauthenticated / clear state
 *
 * NOTE: reaching CORE_AUTHENTICATED (and thus real pixel encryption) requires a
 * responding downstream HDCP sink for the DDC handshake; without one the engine
 * stalls at O_AN_READY by design. This module implements the transmitter side.
 */
#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/io.h>
#include <linux/debugfs.h>
#include <linux/delay.h>
#include <linux/slab.h>

#define KEYLOADER_PHYS  0x20809000u
#define CORE_PHYS       0x20902000u
#define BLK_LEN         0x1000u

/* key-loader block */
#define K_CTL   0x00  /* START[0] DONE[1] DISHDCP[2] */
#define K_ADR   0x04
#define K_KY0   0x08
#define K_KY1   0x0c
#define K_START BIT(0)
#define K_DONE  BIT(1)

/* HDMI core HDCP registers (offsets from CORE_PHYS) */
#define BKSV0        0x10
#define BKSV1        0x14
#define AN0          0x18
#define AN1          0x1c
#define HDCP_KEY_1   0x3c
#define HDCP_KEY_2   0x40
#define HDCP_CTL     0x44
#define CP_STATUS    0x48
#define CP_INTEGRITY 0x4c
#define CP_INTEG_CFG 0x50
#define CP_CONFIG    0x54
#define CP_TST       0x58
#define SCHED_CTL    0xc0

/* HDCP_CTL bits */
#define CTL_AUTH_REQUEST   BIT(0)
#define CTL_CLR_RDB_AUTH   BIT(1)
#define CTL_SET_RDB_AUTH   BIT(2)
#define CTL_FORCE_UNAUTH   BIT(3)
#define CTL_RESET_KU       BIT(16)
/* CP_STATUS bits */
#define ST_AN_READY        BIT(0)
#define ST_BKSV_VALID      BIT(1)
#define ST_RDB_AUTH        BIT(2)
#define ST_CORE_AUTH       BIT(3)
#define ST_AUTH_OK         BIT(4)
#define ST_HDCP_READY      BIT(31)
/* CP_CONFIG bits */
#define CFG_RDB_KEY_LOAD   BIT(10)
#define CFG_ENABLE_KU      BIT(19)
#define CFG_KEY_BASE_MASK  0x3ff
/* SCHEDULER_CONTROL bits */
#define SCH_ENC_ONLY_WHEN_AUTH BIT(6)

static void __iomem *keyl;
static void __iomem *core;
static struct dentry *dbg;

/* deterministic test key set (matches the python reference tooling) */
static u64 test_key(int i)
{
	return (0x123456789ABCDEULL * (u64)(i + 1)) & 0x00FFFFFFFFFFFFFFULL;
}

static u64 compute_km(u64 bksv)
{
	u64 km = 0;
	int i;

	for (i = 0; i < 40; i++)
		if (bksv & (1ULL << i))
			km = (km + test_key(i)) & 0x00FFFFFFFFFFFFFFULL;
	return km;
}

#define BKSV_TEST 0xAAAAAAAAAAULL  /* 40 bits, 20 ones */

static void hdcp_load_keys_rdb(void)
{
	u32 cfg = readl(core + CP_CONFIG);
	int n;

	/* base = 0, enable register-database key load */
	cfg = (cfg & ~CFG_KEY_BASE_MASK);
	writel(cfg | CFG_RDB_KEY_LOAD, core + CP_CONFIG);
	for (n = 0; n < 40; n++) {
		u64 k = test_key(n);

		writel((u32)((k >> 24) & 0xffffffff), core + HDCP_KEY_2);
		writel((u32)(((k & 0xffffff) << 8) | n), core + HDCP_KEY_1);
	}
	writel(readl(core + CP_CONFIG) & ~CFG_RDB_KEY_LOAD, core + CP_CONFIG);
}

static int hdcp_start(void)
{
	u32 st;
	int i, core_auth = -1;
	u64 an, km;

	/* arm the encrypt-when-authenticated gate */
	writel(readl(core + SCHED_CTL) | SCH_ENC_ONLY_WHEN_AUTH, core + SCHED_CTL);
	/* inject a balanced fake sink KSV (Km = sum of our keys at BKSV bits) */
	writel((u32)(BKSV_TEST & 0xffffffff), core + BKSV0);
	writel((u32)((BKSV_TEST >> 32) & 0xff), core + BKSV1);
	/* load device keys into the cipher key RAM */
	hdcp_load_keys_rdb();
	/* arm block-cipher key (Ku) computation */
	writel(readl(core + CP_CONFIG) | CFG_ENABLE_KU, core + CP_CONFIG);
	/* trigger real authentication -> runs the block cipher */
	writel(CTL_AUTH_REQUEST, core + HDCP_CTL);
	for (i = 0; i < 80; i++) {
		st = readl(core + CP_STATUS);
		if (st & ST_CORE_AUTH) {
			core_auth = i;
			break;
		}
		usleep_range(20000, 25000);
	}
	an = ((u64)readl(core + AN1) << 32) | readl(core + AN0);
	km = compute_km(BKSV_TEST);
	st = readl(core + CP_STATUS);
	pr_info("vc4_hdcp: after AUTH_REQUEST core_auth@%d CP_STATUS=0x%08x AN=0x%016llx Km=0x%014llx CP_INTEGRITY=0x%08x\n",
		core_auth, st, an, km, readl(core + CP_INTEGRITY));
	if (!(st & ST_CORE_AUTH))
		pr_info("vc4_hdcp: stalled before CORE_AUTHENTICATED (needs a responding HDCP sink for the DDC handshake)\n");
	return core_auth;
}

static void hdcp_stop(void)
{
	writel(readl(core + SCHED_CTL) & ~SCH_ENC_ONLY_WHEN_AUTH, core + SCHED_CTL);
	writel(0, core + CP_TST);
	writel(CTL_FORCE_UNAUTH | CTL_CLR_RDB_AUTH, core + HDCP_CTL);
	writel(0, core + HDCP_CTL);
	writel(readl(core + CP_CONFIG) & ~CFG_ENABLE_KU, core + CP_CONFIG);
	pr_info("vc4_hdcp: stopped, CP_STATUS=0x%08x\n", readl(core + CP_STATUS));
}

static int status_show(struct seq_file *s, void *unused)
{
	u32 st = readl(core + CP_STATUS);

	seq_printf(s, "key-loader KEY_CTL = 0x%08x (DONE=%d)\n",
		   readl(keyl + K_CTL), !!(readl(keyl + K_CTL) & K_DONE));
	seq_printf(s, "CP_STATUS    = 0x%08x [%s%s%s%s%s%s]\n", st,
		   st & ST_AN_READY ? "AN_READY " : "",
		   st & ST_BKSV_VALID ? "BKSV_VALID " : "",
		   st & ST_RDB_AUTH ? "RDB_AUTH " : "",
		   st & ST_CORE_AUTH ? "CORE_AUTH " : "",
		   st & ST_AUTH_OK ? "AUTH_OK " : "",
		   st & ST_HDCP_READY ? "HDCP_READY" : "");
	seq_printf(s, "HDCP_CTL     = 0x%08x\n", readl(core + HDCP_CTL));
	seq_printf(s, "CP_CONFIG    = 0x%08x\n", readl(core + CP_CONFIG));
	seq_printf(s, "CP_INTEGRITY = 0x%08x (O_RI[31:16]=0x%04x)\n",
		   readl(core + CP_INTEGRITY), readl(core + CP_INTEGRITY) >> 16);
	seq_printf(s, "SCHED_CTL    = 0x%08x\n", readl(core + SCHED_CTL));
	seq_printf(s, "AN           = 0x%08x%08x\n", readl(core + AN1), readl(core + AN0));
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(status);

static ssize_t start_write(struct file *f, const char __user *u, size_t n, loff_t *p)
{
	hdcp_start();
	return n;
}
static const struct file_operations start_fops = { .write = start_write, .open = simple_open, .llseek = noop_llseek };

static ssize_t stop_write(struct file *f, const char __user *u, size_t n, loff_t *p)
{
	hdcp_stop();
	return n;
}
static const struct file_operations stop_fops = { .write = stop_write, .open = simple_open, .llseek = noop_llseek };

static int __init vc4_hdcp_init(void)
{
	keyl = ioremap(KEYLOADER_PHYS, BLK_LEN);
	core = ioremap(CORE_PHYS, BLK_LEN);
	if (!keyl || !core) {
		pr_err("vc4_hdcp: ioremap failed\n");
		if (keyl) iounmap(keyl);
		if (core) iounmap(core);
		return -ENOMEM;
	}
	dbg = debugfs_create_dir("vc4_hdcp", NULL);
	debugfs_create_file("status", 0444, dbg, NULL, &status_fops);
	debugfs_create_file("start", 0200, dbg, NULL, &start_fops);
	debugfs_create_file("stop", 0200, dbg, NULL, &stop_fops);
	pr_info("vc4_hdcp: loaded; keyl=%p core=%p CORE_REV=0x%08x\n",
		keyl, core, readl(core));
	return 0;
}

static void __exit vc4_hdcp_exit(void)
{
	debugfs_remove_recursive(dbg);
	iounmap(keyl);
	iounmap(core);
	pr_info("vc4_hdcp: unloaded\n");
}

module_init(vc4_hdcp_init);
module_exit(vc4_hdcp_exit);
MODULE_LICENSE("GPL");
MODULE_AUTHOR("netv2 HDCP bring-up");
MODULE_DESCRIPTION("BCM2835 HDMI HDCP transmitter bring-up (prototype for vc4)");
