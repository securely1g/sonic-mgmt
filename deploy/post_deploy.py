"""
Post-deployment configuration for SONiC DUT.

Replaces the post-load_minigraph section of
ansible/config_sonic_basedon_testbed.yml: NTP/chrony sync,
TACACS config, BGP startup, config save, cache cleanup, etc.
"""

import logging
import os

logger = logging.getLogger(__name__)

ANSIBLE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "ansible")


def post_deploy(duthost, localhost, facts, save=True):
    """
    Post-deployment configuration after minigraph is loaded.

    Args:
        duthost: SonicHost ansible host (the DUT)
        localhost: Localhost ansible host
        facts: DeployFacts from facts_gatherer
        save: Whether to save config as startup-config
    """
    topo = facts.topo
    hostname = duthost.hostname

    logger.info("Running post-deploy configuration on %s", hostname)

    # ── NTP / Chrony time sync ───────────────────────────────────────
    _sync_time(duthost)

    # ── BMP config ───────────────────────────────────────────────────
    _configure_bmp(duthost)

    # ── BGP startup ──────────────────────────────────────────────────
    logger.info("Starting all BGP sessions")
    duthost.shell("config bgp startup all", module_ignore_errors=True)

    # ── Smartswitch DPU config ───────────────────────────────────────
    if topo in ["t1-smartswitch-ha", "t1-28-lag", "smartswitch-t1", "t1-48-lag"]:
        if facts.is_light_mode:
            _configure_smartswitch(duthost, facts)

    # ── TACACS ───────────────────────────────────────────────────────
    _configure_tacacs(duthost, facts)

    # ── Configlet application ────────────────────────────────────────
    configlet_script = os.path.join(
        ANSIBLE_DIR, "vars", "configlet", topo, "apply_clet.sh"
    )
    if os.path.exists(configlet_script):
        logger.info("Applying configlet script")
        duthost.shell("bash /etc/sonic/apply_clet.sh", module_ignore_errors=True)

    # ── ISIS config for WAN topologies ───────────────────────────────
    if topo in ["wan-ecmp", "wan-pub-isis"]:
        _configure_isis(duthost, facts)

    # ── WAN static routes ────────────────────────────────────────────
    if topo == "wan-3link-tg":
        logger.info("Configuring static routes for WAN trex passthrough")
        duthost.command("config route add prefix 48.0.0.0/8 nexthop 10.0.0.59")
        duthost.command("config route add prefix 16.0.0.0/8 nexthop 10.0.0.57")

    # ── Dualtor grpc config ──────────────────────────────────────────
    if 'dualtor-mixed' in topo or 'dualtor-aa' in topo:
        _configure_dualtor_grpc(duthost)

    # ── TSB for T2 ───────────────────────────────────────────────────
    if 't2' in topo:
        logger.info("Executing TSB on T2 DUT")
        duthost.shell("TSB", module_ignore_errors=True)

    # ── Save config ──────────────────────────────────────────────────
    if save:
        logger.info("Saving configuration")
        duthost.shell("config save -y")

    # ── Cleanup ──────────────────────────────────────────────────────
    _cleanup(duthost, localhost, facts)

    # ── Reset sshd timeout ───────────────────────────────────────────
    logger.info("Resetting sshd ClientAliveInterval to 900")
    duthost.shell(
        'sed -i "s/^ClientAliveInterval [0-9].*/ClientAliveInterval 900/g" '
        '/etc/ssh/sshd_config',
        module_ignore_errors=True
    )
    duthost.service(name="sshd", state="restarted", module_ignore_errors=True)

    # ── Enable IPv6 ──────────────────────────────────────────────────
    duthost.shell(
        "sysctl -w net.ipv6.conf.all.disable_ipv6=0",
        module_ignore_errors=True
    )

    logger.info("Post-deploy configuration complete for %s", hostname)


def _sync_time(duthost):
    """Sync DUT time with NTP or chrony."""
    logger.info("Syncing system time")

    # Check if chrony is available
    chrony_check = duthost.shell(
        "systemctl list-unit-files chrony.service 2>/dev/null | grep chrony",
        module_ignore_errors=True
    )
    has_chrony = chrony_check.get('rc') == 0 and 'chrony' in chrony_check.get('stdout', '')

    if not has_chrony:
        # Use NTP
        logger.info("Syncing time via NTP")
        duthost.service(name="ntp", state="started", module_ignore_errors=True)
        duthost.service(name="ntp", state="stopped", module_ignore_errors=True)
        duthost.command("ntpd -gq", module_ignore_errors=True)
        duthost.command("hwclock --systohc", module_ignore_errors=True)
        duthost.service(name="ntp", state="restarted", module_ignore_errors=True)
    else:
        # Use chrony
        logger.info("Syncing time via chrony")
        duthost.service(name="chrony", state="started", module_ignore_errors=True)
        duthost.service(name="chrony", state="stopped", module_ignore_errors=True)
        duthost.command("chronyd -F 1 -q", module_ignore_errors=True)
        duthost.service(name="chrony", state="restarted", module_ignore_errors=True)


def _configure_bmp(duthost):
    """Enable BMP if supported."""
    bmp_check = duthost.shell(
        "config -h | grep 'BMP-related configuration'",
        module_ignore_errors=True
    )
    if bmp_check.get('rc') != 0:
        return

    logger.info("Enabling BMP configuration")
    duthost.command("sudo config bmp enable bgp-neighbor-table",
                    module_ignore_errors=True)
    duthost.command("sudo config bmp enable bgp-rib-in-table",
                    module_ignore_errors=True)
    duthost.command("sudo config bmp enable bgp-rib-out-table",
                    module_ignore_errors=True)


def _configure_tacacs(duthost, facts):
    """Configure TACACS authentication."""
    if facts.tacacs_enabled_by_default:
        logger.info("Configuring TACACS")
        cmds = [
            f"config tacacs passkey {facts.tacacs_passkey}" if facts.tacacs_passkey else None,
            "config tacacs authtype pap",
            "config aaa authentication login tacacs+",
        ]
        for cmd in cmds:
            if cmd:
                duthost.shell(cmd, module_ignore_errors=True)

    if facts.use_ptf_tacacs:
        logger.info("Configuring PTF TACACS server")
        duthost.shell("config tacacs authtype login", module_ignore_errors=True)
        duthost.shell('config aaa authorization "tacacs+ local"',
                      module_ignore_errors=True)
        duthost.shell('config aaa accounting "tacacs+ local"',
                      module_ignore_errors=True)

        if facts.topo == "mx":
            duthost.shell('config aaa authorization "tacacs+ local"',
                          module_ignore_errors=True)


def _configure_smartswitch(duthost, facts):
    """Load smartswitch DPU configuration."""
    logger.info("Loading smartswitch DPU configuration")
    smartswitch_json = os.path.join(
        ANSIBLE_DIR, "golden_config_db", "smartswitch_dpu_extra.json"
    )
    if os.path.exists(smartswitch_json):
        duthost.copy(src=smartswitch_json, dest="/tmp/dpu_extra.json")
        duthost.load_extra_dpu_config(
            hwsku=facts.hwsku,
            host_username=getattr(duthost, 'sonic_login', 'admin'),
            module_ignore_errors=True
        )


def _configure_isis(duthost, facts):
    """Configure ISIS for WAN topologies."""
    logger.info("Configuring ISIS for %s", facts.topo)
    isis_template = os.path.join(ANSIBLE_DIR, "templates", "isis_config.j2")
    if os.path.exists(isis_template):
        duthost.template(
            src=isis_template,
            dest="/etc/sonic/isis_config.json"
        )
        duthost.shell(
            "sonic-cfggen -j /etc/sonic/isis_config.json -w",
            module_ignore_errors=True
        )
        # Restart BGP container (workaround for isisd issue)
        duthost.shell("docker restart bgp", module_ignore_errors=True)


def _configure_dualtor_grpc(duthost):
    """Configure insecure gRPC for dualtor-mixed/dualtor-aa topologies."""
    grpc_file = "/etc/sonic/grpc_secrets.json"
    result = duthost.shell(
        f"test -f {grpc_file} && echo exists",
        module_ignore_errors=True
    )
    if 'exists' not in result.get('stdout', ''):
        return

    logger.info("Switching gRPC to insecure mode for dualtor testing")
    duthost.shell(
        f'sed -E -i \'s/"type": "secure"/"type": "insecure"/\' {grpc_file}',
        module_ignore_errors=True
    )
    duthost.service(name="pmon", state="restarted", module_ignore_errors=True)


def _cleanup(duthost, localhost, facts):
    """Clean up temporary files and caches."""
    logger.info("Cleaning up")

    # Remove running golden config
    duthost.file(
        path="/etc/sonic/running_golden_config.json",
        state="absent",
        module_ignore_errors=True
    )

    # Remove per-ASIC golden configs
    if facts.num_asics > 1:
        for i in range(facts.num_asics):
            duthost.file(
                path=f"/etc/sonic/running_golden_config{i}.json",
                state="absent",
                module_ignore_errors=True
            )

    # Cleanup cached facts
    cache_script = os.path.join(
        os.path.dirname(ANSIBLE_DIR), "tests", "common", "cache", "facts_cache.py"
    )
    if os.path.exists(cache_script):
        localhost.shell(
            f"python {cache_script}",
            module_ignore_errors=True
        )
