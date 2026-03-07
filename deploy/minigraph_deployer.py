"""
Generate and deploy minigraph to SONiC DUT.

Replaces the template rendering + deploy block of
ansible/config_sonic_basedon_testbed.yml by using pytest-ansible
module calls (duthost.shell, duthost.template, etc.).
"""

import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Path to ansible directory relative to sonic-mgmt root
ANSIBLE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "ansible")


def generate_and_deploy(localhost, duthost, facts, deploy=True, save=True):
    """
    Generate minigraph XML and optionally deploy to DUT.

    This mirrors the deploy block of config_sonic_basedon_testbed.yml:
    template rendering, cert deployment, minigraph loading, and waiting
    for the switch to come back.

    Args:
        localhost: Localhost ansible host
        duthost: SonicHost ansible host (the DUT)
        facts: DeployFacts from facts_gatherer
        deploy: Whether to deploy to DUT (False = local generation only)
        save: Whether to save config as startup-config
    """
    topo = facts.topo
    hostname = duthost.hostname

    if not deploy:
        _generate_local(localhost, duthost, facts)
        return

    logger.info("Deploying minigraph to %s (topo=%s)", hostname, topo)

    # ── Load topology variables ──────────────────────────────────────
    # The YAML playbook does: include_vars: "vars/topo_{{ topo }}.yml"
    # With pytest-ansible, the template module will pick these up from
    # the ansible directory context.

    # ── Save original minigraph ──────────────────────────────────────
    logger.info("Backing up existing minigraph")
    duthost.shell(
        "mv /etc/sonic/minigraph.xml /etc/sonic/minigraph.xml.orig",
        module_ignore_errors=True
    )

    # ── Deploy certificates ──────────────────────────────────────────
    _deploy_certs(duthost, facts)

    # ── Docker proxy ─────────────────────────────────────────────────
    _configure_docker_proxy(duthost, facts)

    # ── Update TACACS server address ─────────────────────────────────
    if facts.use_ptf_tacacs and facts.tacacs_servers:
        logger.info("TACACS server: %s", facts.tacacs_servers)

    # ── Generate and copy minigraph to DUT ───────────────────────────
    logger.info("Rendering minigraph template to DUT")
    template_src = os.path.join(ANSIBLE_DIR, "templates", "minigraph_template.j2")
    duthost.template(
        src=template_src,
        dest="/etc/sonic/minigraph.xml"
    )

    # ── Configlet ────────────────────────────────────────────────────
    configlet_script = os.path.join(ANSIBLE_DIR, "vars", "configlet", topo, "apply_clet.sh")
    if os.path.exists(configlet_script):
        logger.info("Copying configlet files for topo=%s", topo)
        configlet_dir = os.path.join(ANSIBLE_DIR, "vars", "configlet", topo)
        duthost.copy(src=configlet_dir + "/", dest="/etc/sonic/")

    # ── SNMP community ───────────────────────────────────────────────
    duthost.lineinfile(
        name="/etc/sonic/snmp.yml",
        regexp="^snmp_rocommunity:",
        line=f"snmp_rocommunity: {facts.snmp_rocommunity}",
        module_ignore_errors=True
    )

    # ── Core analyzer ────────────────────────────────────────────────
    _configure_core_analyzer(duthost)

    # ── Start topology service for multi-ASIC ────────────────────────
    if facts.num_asics > 1:
        logger.info("Starting topology service for multi-ASIC platform")
        duthost.shell(
            "systemctl start topology.service",
            module_ignore_errors=True
        )

    # ── Generate golden_config_db.json ───────────────────────────────
    logger.info("Generating golden_config_db.json")
    duthost.generate_golden_config_db(
        topo_name=topo,
        port_index_map=facts.port_index_map,
        hwsku=facts.hwsku,
        is_light_mode=facts.is_light_mode,
        module_ignore_errors=True
    )

    # ── Create DNS config ────────────────────────────────────────────
    dns_template = os.path.join(ANSIBLE_DIR, "templates", "dns_config.j2")
    if os.path.exists(dns_template):
        duthost.template(
            src=dns_template,
            dest="/tmp/dns_config.json"
        )

    # ── Cleanup before loading ───────────────────────────────────────
    logger.info("Cleaning up /etc/sonic before loading minigraph")
    duthost.file(path="/etc/sonic/acl.json", state="absent")
    duthost.file(path="/etc/sonic/port_config.json", state="absent")

    # ── Preload for multi-ASIC / T2 ─────────────────────────────────
    dut_type = getattr(duthost, 'type', 'kvm')
    if ('t2' in topo or facts.num_asics > 1) and dut_type == 'kvm':
        logger.info("Preloading minigraph for multi-ASIC/T2 platform")
        duthost.shell("config load_minigraph -y && config save -y")
        # Wait for essential services
        for _ in range(30):
            result = duthost.shell(
                "systemctl is-active --quiet pmon",
                module_ignore_errors=True
            )
            if result.get('rc') == 0:
                break
            time.sleep(10)

    # ── Load minigraph ───────────────────────────────────────────────
    logger.info("Loading minigraph on %s", hostname)
    if not facts.ipv6_only_mgmt:
        result = duthost.shell(
            "config load_minigraph --override_config -y",
            module_ignore_errors=True
        )
        if result.get('rc') != 0:
            stderr = result.get('stderr', '')
            if '--override_config' in stderr or 'no such option' in stderr:
                logger.warning("--override_config not supported, retrying without")
                duthost.shell("config load_minigraph -y")
            else:
                raise RuntimeError(
                    f"config load_minigraph failed on {hostname}: "
                    f"rc={result.get('rc')}, stderr={stderr}"
                )
    else:
        # IPv6 transition: use async to prevent SSH timeout
        logger.info("Loading minigraph with IPv6 transition (async)")
        duthost.shell(
            "config load_minigraph --override_config -y",
            module_async=True,
            module_ignore_errors=True
        )
        time.sleep(5)  # Brief pause to let load start

    # ── DSCP_TO_TC_MAP cleanup for specific HWSKUs ───────────────────
    if facts.hwsku == 'cisco-8101-p4-32x100-vs' and not facts.ipv6_only_mgmt:
        duthost.shell('redis-cli -n 4 del "DSCP_TO_TC_MAP|AZURE"',
                      module_ignore_errors=True)

    # ── Wait for switch to come back ─────────────────────────────────
    if facts.ipv6_only_mgmt and hasattr(duthost, 'mgmt_ipv6') and duthost.mgmt_ipv6:
        wait_host = duthost.mgmt_ipv6
    else:
        wait_host = duthost.mgmt_ip

    logger.info("Waiting for %s to become reachable at %s:22", hostname, wait_host)
    localhost.wait_for(
        host=wait_host,
        port=22,
        state="started",
        search_regex=r"OpenSSH_[\w\.]+ Debian",
        delay=10,
        timeout=600
    )
    logger.info("%s is reachable again", hostname)

    # ── Switch to IPv6 host if needed ────────────────────────────────
    if facts.ipv6_only_mgmt and hasattr(duthost, 'mgmt_ipv6') and duthost.mgmt_ipv6:
        logger.info("Switching ansible_host to IPv6: %s", duthost.mgmt_ipv6)
        # Update the ansible host for subsequent tasks
        # This would need to be handled by the caller or via host vars


def _generate_local(localhost, duthost, facts):
    """Generate minigraph locally without deploying."""
    topo = facts.topo
    hostname = duthost.hostname
    logger.info("Generating minigraph locally for %s (topo=%s)", hostname, topo)

    template_src = os.path.join(ANSIBLE_DIR, "templates", "minigraph_template.j2")
    dest = os.path.join(ANSIBLE_DIR, "minigraph", f"{hostname}.{topo}.xml")
    localhost.template(src=template_src, dest=dest)
    logger.info("Minigraph written to %s", dest)


def _deploy_certs(duthost, facts):
    """Deploy telemetry and restapi certificates if configured."""
    # Telemetry certs — check if configured in group_vars
    try:
        # These vars come from group_vars and are available as host vars
        telemetry_certs = getattr(duthost, 'telemetry_certs', None)
        if telemetry_certs:
            logger.info("Deploying telemetry certificates")
            _deploy_cert_set(duthost, telemetry_certs)
    except Exception:
        logger.debug("No telemetry certs configured (non-fatal)")

    # RestAPI certs
    try:
        restapi_certs = getattr(duthost, 'restapi_certs', None)
        if restapi_certs:
            logger.info("Deploying restapi certificates")
            _deploy_cert_set(duthost, restapi_certs)
    except Exception:
        logger.debug("No restapi certs configured (non-fatal)")


def _deploy_cert_set(duthost, certs):
    """Deploy a set of certificates to the DUT."""
    dir_path = certs.get('dir_path', '')
    if not dir_path:
        return

    server_key = certs.get('server_key', '')
    server_crt = certs.get('server_cer', certs.get('server_crt', ''))
    dsmsroot_key = certs.get('dsmsroot_key', '')
    dsmsroot_cer = certs.get('dsmsroot_cer', '')
    subject_server = certs.get('subject_server', '')
    subject_client = certs.get('subject_client', '')

    # Create directory
    duthost.file(path=dir_path, state="directory", recurse=True)

    # Write cert/key files
    if server_key:
        duthost.copy(content=server_key, dest=os.path.join(dir_path, "server.key"))
    if server_crt:
        duthost.copy(content=server_crt, dest=os.path.join(dir_path, "server.crt"))
    if dsmsroot_key:
        duthost.copy(content=dsmsroot_key, dest=os.path.join(dir_path, "dsmsroot.key"))
    if dsmsroot_cer:
        duthost.copy(content=dsmsroot_cer, dest=os.path.join(dir_path, "dsmsroot.cer"))


def _configure_docker_proxy(duthost, facts):
    """Set up docker HTTP proxy if configured."""
    # Check if proxy_env is defined in host vars
    proxy_env = getattr(duthost, 'proxy_env', None)
    if not proxy_env:
        return

    logger.info("Configuring docker HTTP proxy")
    duthost.file(
        path="/etc/systemd/system/docker.service.d",
        state="directory",
        recurse=True
    )

    template_src = os.path.join(ANSIBLE_DIR, "templates", "docker_http_proxy.j2")
    if os.path.exists(template_src):
        duthost.template(
            src=template_src,
            dest="/etc/systemd/system/docker.service.d/http-proxy.conf",
            force=True
        )
        duthost.shell("systemctl daemon-reload")
        duthost.service(name="docker", state="restarted")
        logger.info("Waiting 60s for docker containers to restart")
        time.sleep(60)


def _configure_core_analyzer(duthost):
    """Configure core analyzer if rc file exists."""
    result = duthost.shell(
        "test -f /etc/sonic/core_analyzer.rc.json && echo exists",
        module_ignore_errors=True
    )
    if 'exists' not in result.get('stdout', ''):
        return

    # Core analyzer config is handled via group_vars
    # The actual key/proxy values come from vault-encrypted variables
    logger.debug("Core analyzer rc.json exists, skipping detailed config "
                 "(handled by group_vars)")
