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
    # YAML lines 923-998
    _sync_time(duthost, facts)

    # ── Remove PortChannel IPs for t1-filterleaf-lag ─────────────────
    # YAML lines 990-998
    if topo == "t1-filterleaf-lag":
        _remove_portchannel_ips(duthost)

    # ── BMP config ───────────────────────────────────────────────────
    # YAML lines 1001-1020
    _configure_bmp(duthost)

    # ── Static routes for WAN ────────────────────────────────────────
    # YAML lines 1022-1027
    if topo == "wan-3link-tg":
        logger.info("Configuring static routes for WAN trex passthrough")
        duthost.command("config route add prefix 48.0.0.0/8 nexthop 10.0.0.59",
                        module_ignore_errors=True)
        duthost.command("config route add prefix 16.0.0.0/8 nexthop 10.0.0.57",
                        module_ignore_errors=True)

    # ── BGP startup ──────────────────────────────────────────────────
    # YAML line 1029
    logger.info("Starting all BGP sessions")
    duthost.shell("config bgp startup all", module_ignore_errors=True)

    # ── Smartswitch DPU config ───────────────────────────────────────
    # YAML lines 1031-1047: copy dpu_extra.json and load DPU config
    if (topo in ["t1-smartswitch-ha", "t1-28-lag", "smartswitch-t1", "t1-48-lag"]
            and facts.is_light_mode):
        _configure_smartswitch(duthost, facts)

    # ── TACACS ───────────────────────────────────────────────────────
    # YAML lines 1049-1078
    _configure_tacacs(duthost, facts)

    # ── Configlet application ────────────────────────────────────────
    # YAML lines 1080-1083
    configlet_script = os.path.join(
        ANSIBLE_DIR, "vars", "configlet", topo, "apply_clet.sh"
    )
    if os.path.exists(configlet_script):
        logger.info("Applying configlet script")
        duthost.shell("bash /etc/sonic/apply_clet.sh", module_ignore_errors=True)

    # ── ISIS config for WAN topologies ───────────────────────────────
    # YAML lines 1085-1097
    if topo in ["wan-ecmp", "wan-pub-isis"]:
        _configure_isis(duthost, facts)

    # ── Dualtor gRPC config ──────────────────────────────────────────
    # YAML lines 1099-1140
    if 'dualtor-mixed' in topo or 'dualtor-aa' in topo:
        _configure_dualtor_grpc(duthost)

    # ── TSB for T2 ───────────────────────────────────────────────────
    # YAML line 1142
    if 't2' in topo:
        logger.info("Executing TSB on T2 DUT")
        duthost.shell("TSB", module_ignore_errors=True)

    # ── Save config ──────────────────────────────────────────────────
    # YAML lines 1144-1146
    if save:
        logger.info("Saving configuration")
        duthost.shell("config save -y")

    # ── Cleanup ──────────────────────────────────────────────────────
    # YAML lines 1148-1169
    _cleanup(duthost, localhost, facts)

    # ── Reset sshd timeout ───────────────────────────────────────────
    # YAML lines 1175-1177
    logger.info("Resetting sshd ClientAliveInterval to 900")
    duthost.shell(
        'sed -i "s/^ClientAliveInterval [0-9].*/ClientAliveInterval 900/g" '
        '/etc/ssh/sshd_config && systemctl restart sshd',
        module_ignore_errors=True
    )

    # ── Enable IPv6 ──────────────────────────────────────────────────
    # YAML lines 1179-1183
    duthost.sysctl(
        name="net.ipv6.conf.all.disable_ipv6",
        value="0",
        sysctl_set=True,
        module_ignore_errors=True
    )

    logger.info("Post-deploy configuration complete for %s", hostname)


def _sync_time(duthost, facts):
    """
    Sync DUT time with NTP or chrony.

    YAML lines 923-998: checks chrony availability, handles T2 KVM
    ntpsec TimeoutSec, ntp vs chrony sync sequences.
    """
    logger.info("Syncing system time")

    # Check if chrony is available
    chrony_check = duthost.service(
        name="chrony",
        module_ignore_errors=True
    )
    chrony_status = chrony_check.get('status', {})
    has_chrony = chrony_status.get('LoadState', 'not-found') != 'not-found'

    if not has_chrony:
        # ── NTP sync ─────────────────────────────────────────────────
        logger.info("Syncing time via NTP")

        # Add TimeoutSec for T2 KVM (YAML lines 955-965)
        if 't2' in facts.topo and facts.dut_type == 'kvm':
            result = duthost.shell(
                "grep -q 'TimeoutSec=' /lib/systemd/system/ntpsec.service",
                module_ignore_errors=True
            )
            if result.get('rc') != 0:
                duthost.shell(
                    'sed -i \'/^\\[Service\\]/a TimeoutSec=600\' '
                    '/lib/systemd/system/ntpsec.service',
                    module_ignore_errors=True
                )
            duthost.shell("systemctl daemon-reload", module_ignore_errors=True)

        duthost.service(name="ntp", state="started", module_ignore_errors=True)
        duthost.service(name="ntp", state="stopped", module_ignore_errors=True)
        duthost.command("ntpd -gq", module_ignore_errors=True)
        duthost.command("hwclock --systohc", module_ignore_errors=True)
        duthost.service(name="ntp", state="restarted", enabled=True,
                        module_ignore_errors=True)
    else:
        # ── Chrony sync ──────────────────────────────────────────────
        logger.info("Syncing time via chrony")
        duthost.service(name="chrony", state="started", module_ignore_errors=True)
        duthost.service(name="chrony", state="stopped", module_ignore_errors=True)
        duthost.command("chronyd -F 1 -q", module_ignore_errors=True)
        duthost.service(name="chrony", state="restarted", enabled=True,
                        module_ignore_errors=True)


def _remove_portchannel_ips(duthost):
    """
    Remove specific PortChannel IPs for t1-filterleaf-lag.

    YAML lines 990-998.
    """
    logger.info("Removing PortChannel IPs for t1-filterleaf-lag")
    duthost.shell(
        '''for pc in PortChannel1 PortChannel3 PortChannel4 PortChannel6; do
            for ip in $(redis-cli -n 4 keys "PORTCHANNEL_INTERFACE|$pc|*" | cut -d'|' -f3); do
                echo "Removing $pc $ip"
                config interface ip remove $pc $ip || true
            done
        done''',
        module_ignore_errors=True
    )


def _configure_bmp(duthost):
    """Enable BMP if supported (YAML lines 1001-1020)."""
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
    """
    Configure TACACS authentication.

    YAML lines 1049-1078.
    """
    # Base TACACS config
    if facts.tacacs_enabled_by_default:
        logger.info("Configuring TACACS")
        if facts.tacacs_passkey:
            duthost.shell(
                f"config tacacs passkey {facts.tacacs_passkey}",
                module_ignore_errors=True
            )
        duthost.shell("config tacacs authtype pap", module_ignore_errors=True)
        duthost.shell("config aaa authentication login tacacs+",
                      module_ignore_errors=True)

    # PTF TACACS server config
    if facts.use_ptf_tacacs:
        logger.info("Configuring PTF TACACS server")
        duthost.shell("config tacacs authtype login", module_ignore_errors=True)
        duthost.shell('config aaa authorization tacacs+',
                      module_ignore_errors=True)
        duthost.shell('config aaa accounting "tacacs+ local"',
                      module_ignore_errors=True)

        # MX topo additional authorization
        if facts.topo == "mx":
            duthost.shell('config aaa authorization "tacacs+ local"',
                          module_ignore_errors=True)


def _configure_smartswitch(duthost, facts):
    """
    Load smartswitch DPU configuration.

    YAML lines 1031-1047.
    """
    logger.info("Loading smartswitch DPU configuration")
    dpu_src = os.path.join(
        ANSIBLE_DIR, "golden_config_db", "smartswitch_dpu_extra.json"
    )
    if os.path.exists(dpu_src):
        duthost.copy(src=dpu_src, dest="/tmp/dpu_extra.json")
        duthost.load_extra_dpu_config(
            hwsku=facts.hwsku,
            host_username=getattr(duthost, 'sonic_login', 'admin'),
            host_passwords=getattr(duthost, 'sonic_default_passwords', []),
            module_ignore_errors=True
        )


def _configure_isis(duthost, facts):
    """Configure ISIS for WAN topologies (YAML lines 1085-1097)."""
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
    """
    Configure insecure gRPC for dualtor-mixed/dualtor-aa topologies.

    YAML lines 1099-1140: checks grpc_secrets.json, switches
    secure→insecure, restarts pmon.
    """
    grpc_file = "/etc/sonic/grpc_secrets.json"

    # Check if file exists
    stat_result = duthost.shell(
        f"test -f {grpc_file} && echo exists",
        module_ignore_errors=True
    )
    if 'exists' not in stat_result.get('stdout', ''):
        return

    logger.info("Switching gRPC to insecure mode for dualtor testing")

    # Read before (for logging)
    before = duthost.shell(f"cat {grpc_file}", module_ignore_errors=True)
    logger.debug("gRPC config before: %s", before.get('stdout', ''))

    # Switch secure → insecure
    duthost.shell(
        f'sed -E -i \'s/"type": "secure"/"type": "insecure"/\' {grpc_file}',
        module_ignore_errors=True
    )

    # Read after (for logging)
    after = duthost.shell(f"cat {grpc_file}", module_ignore_errors=True)
    logger.debug("gRPC config after: %s", after.get('stdout', ''))

    # Restart pmon
    duthost.service(name="pmon", state="restarted", module_ignore_errors=True)


def _cleanup(duthost, localhost, facts):
    """
    Clean up temporary files and caches.

    YAML lines 1148-1169.
    """
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
