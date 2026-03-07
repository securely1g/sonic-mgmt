"""
Generate and deploy minigraph to SONiC DUT.

Replaces the template rendering + deploy block of
ansible/config_sonic_basedon_testbed.yml by using pytest-ansible
module calls (duthost.shell, duthost.template, etc.).
"""

import logging
import os
import time

logger = logging.getLogger(__name__)

ANSIBLE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "ansible")


def generate_and_deploy(localhost, duthost, facts, deploy=True, save=True):
    """
    Generate minigraph XML and optionally deploy to DUT.

    Args:
        localhost: Localhost ansible host
        duthost: SonicHost ansible host (the DUT)
        facts: DeployFacts from facts_gatherer
        deploy: Whether to deploy to DUT (False = local generation only)
        save: Whether to save config as startup-config
    """
    topo = facts.topo
    hostname = duthost.hostname

    # ── Local-only generation (deploy=false) ─────────────────────────
    if not deploy:
        _generate_local(localhost, duthost, facts)
        return

    logger.info("Deploying minigraph to %s (topo=%s)", hostname, topo)

    # ── init_cfg_profile path ────────────────────────────────────────
    # YAML lines 610-640: alternative to minigraph — load config_db.json
    if facts.init_cfg_profile:
        _deploy_init_cfg_profile(duthost, facts)
    else:
        # ── Save original minigraph ──────────────────────────────────
        # YAML line 582
        logger.info("Backing up existing minigraph")
        duthost.shell(
            "mv /etc/sonic/minigraph.xml /etc/sonic/minigraph.xml.orig",
            module_ignore_errors=True
        )

    # ── Deploy certificates ──────────────────────────────────────────
    # YAML lines 440-535: telemetry and restapi certs via openssl
    _deploy_certs(duthost, localhost, facts)

    # ── Docker proxy ─────────────────────────────────────────────────
    # YAML lines 540-560
    _configure_docker_proxy(duthost, facts)

    # ── Generate and copy minigraph to DUT ───────────────────────────
    # YAML line 603: template minigraph_template.j2 → /etc/sonic/minigraph.xml
    if not facts.init_cfg_profile:
        logger.info("Rendering minigraph template to DUT")
        template_src = os.path.join(ANSIBLE_DIR, "templates", "minigraph_template.j2")
        duthost.template(
            src=template_src,
            dest="/etc/sonic/minigraph.xml"
        )

    # ── Configlet files ──────────────────────────────────────────────
    # YAML lines 642-656
    configlet_script = os.path.join(ANSIBLE_DIR, "vars", "configlet", topo, "apply_clet.sh")
    if os.path.exists(configlet_script):
        logger.info("Copying configlet files for topo=%s", topo)
        configlet_dir = os.path.join(ANSIBLE_DIR, "vars", "configlet", topo)
        duthost.copy(src=configlet_dir + "/", dest="/etc/sonic/")

    # ── Core analyzer ────────────────────────────────────────────────
    # YAML lines 658-706: read vault vars, write to rc.json, enable+start service
    _configure_core_analyzer(duthost, facts)

    # ── SNMP community ───────────────────────────────────────────────
    # YAML lines 708-713
    if not facts.init_cfg_profile:
        duthost.lineinfile(
            name="/etc/sonic/snmp.yml",
            regexp="^snmp_rocommunity:",
            line=f"snmp_rocommunity: {facts.snmp_rocommunity}",
            module_ignore_errors=True
        )

    # ── Docker status (debug) ────────────────────────────────────────
    # YAML lines 715-716
    result = duthost.shell("docker ps", module_ignore_errors=True)
    logger.debug("Docker status:\n%s", result.get('stdout', ''))

    # ── Start topology service ───────────────────────────────────────
    # YAML lines 718-724: when start_topo_service is defined
    if facts.start_topo_service:
        logger.info("Starting topology service for multi-ASIC platform")
        for attempt in range(3):
            result = duthost.shell(
                "systemctl start topology.service",
                module_ignore_errors=True
            )
            if result.get('rc', 1) == 0:
                break
            logger.warning("topology.service start attempt %d failed, retrying...", attempt + 1)
            time.sleep(10)

    # ── Cleanup before loading ───────────────────────────────────────
    # YAML lines 726-735
    logger.info("Cleaning up /etc/sonic before loading minigraph")
    duthost.file(path="/etc/sonic/acl.json", state="absent")
    duthost.file(path="/etc/sonic/port_config.json", state="absent")

    # ── Re-run port_alias for mx topo ────────────────────────────────
    # YAML line 738
    if topo == "mx":
        duthost.port_alias(hwsku=facts.hwsku)

    # ── Preload for multi-ASIC / T2 ─────────────────────────────────
    # YAML lines 741-750
    if ('t2' in topo or facts.num_asics > 1) and facts.dut_type == 'kvm':
        logger.info("Preloading minigraph for multi-ASIC/T2 platform")
        duthost.shell("config load_minigraph -y && config save -y")
        # Wait for pmon service (retries 30, delay 10)
        for attempt in range(30):
            result = duthost.shell(
                "systemctl is-active --quiet pmon",
                module_ignore_errors=True
            )
            if result.get('rc') == 0:
                break
            time.sleep(10)

    # ── Copy dhcp_server_mx.json for mx topo ─────────────────────────
    # YAML lines 752-756
    if topo == "mx":
        dhcp_src = os.path.join(ANSIBLE_DIR, "golden_config_db", "dhcp_server_mx.json")
        if os.path.exists(dhcp_src):
            logger.info("Copying dhcp_server config for mx topo")
            duthost.copy(src=dhcp_src, dest="/tmp/dhcp_server.json")

    # ── Copy smartswitch_t1.json ─────────────────────────────────────
    # YAML lines 758-763: BEFORE golden_config_db generation
    if (topo in ["t1-smartswitch-ha", "t1-28-lag", "smartswitch-t1", "t1-48-lag"]
            and facts.is_light_mode):
        ss_src = os.path.join(ANSIBLE_DIR, "golden_config_db", "smartswitch_t1.json")
        if os.path.exists(ss_src):
            logger.info("Copying smartswitch config")
            duthost.copy(src=ss_src, dest="/tmp/smartswitch.json")

    # ── Create DNS config ────────────────────────────────────────────
    # YAML lines 765-768
    dns_template = os.path.join(ANSIBLE_DIR, "templates", "dns_config.j2")
    if os.path.exists(dns_template):
        duthost.template(
            src=dns_template,
            dest="/tmp/dns_config.json"
        )

    # ── BCM DNX SOC properties adjustment for Nokia T2 ───────────────
    # YAML lines 769-799
    _adjust_soc_properties(duthost, facts)

    # ── Generate golden_config_db.json ───────────────────────────────
    # YAML lines 801-808
    logger.info("Generating golden_config_db.json")
    golden_kwargs = {
        'topo_name': topo,
        'port_index_map': facts.port_index_map,
        'hwsku': facts.hwsku,
        'is_light_mode': facts.is_light_mode,
        'module_ignore_errors': True,
    }
    # Pass configuration for t1-filterleaf-lag
    if topo == 't1-filterleaf-lag':
        golden_kwargs['vm_configuration'] = facts.configuration
    duthost.generate_golden_config_db(**golden_kwargs)

    # ── Macsec profile for T2 ────────────────────────────────────────
    # YAML lines 810-823
    enable_macsec = getattr(duthost, 'enable_macsec', None)
    if 't2' in topo and enable_macsec:
        _deploy_macsec_profile(duthost, facts)

    # ── Load minigraph / config reload ───────────────────────────────
    if facts.init_cfg_profile:
        # YAML line 887: config reload for init_cfg_profile
        logger.info("Running config reload for init_cfg_profile")
        duthost.shell("config reload -y")
    else:
        # YAML lines 851-888: load minigraph with override_config
        _load_minigraph(duthost, facts)

    # ── DSCP_TO_TC_MAP cleanup for specific HWSKUs ───────────────────
    # YAML lines 890-895
    if (facts.hwsku == 'cisco-8101-p4-32x100-vs'
            and not facts.ipv6_only_mgmt):
        duthost.shell(
            'redis-cli -n 4 del "DSCP_TO_TC_MAP|AZURE"',
            module_ignore_errors=True
        )

    # ── Wait for switch to come back ─────────────────────────────────
    # YAML lines 897-913: with IPv4/IPv6 address logic
    if (facts.ipv6_only_mgmt
            and getattr(duthost, 'ansible_hostv6', None)):
        wait_host = duthost.ansible_hostv6
    elif facts.original_ipv4_address:
        wait_host = facts.original_ipv4_address
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
    # YAML lines 915-921
    if (facts.ipv6_only_mgmt
            and getattr(duthost, 'ansible_hostv6', None)):
        logger.info("Switching ansible_host to IPv6: %s", duthost.ansible_hostv6)
        # Update the ansible host for subsequent tasks
        # In pytest-ansible, this may need host_pattern manipulation
        duthost.host.options['inventory_manager'].set_variable(
            duthost.hostname, 'ansible_host', duthost.ansible_hostv6
        )


def _generate_local(localhost, duthost, facts):
    """Generate minigraph locally without deploying (YAML line 433)."""
    topo = facts.topo
    hostname = duthost.hostname
    logger.info("Generating minigraph locally for %s (topo=%s)", hostname, topo)

    template_src = os.path.join(ANSIBLE_DIR, "templates", "minigraph_template.j2")
    dest = os.path.join(ANSIBLE_DIR, "minigraph", f"{hostname}.{topo}.xml")
    localhost.template(src=template_src, dest=dest)
    logger.info("Minigraph written to %s", dest)


def _deploy_init_cfg_profile(duthost, facts):
    """
    Deploy config_db.json from init_cfg_profile instead of minigraph.

    YAML lines 610-640.
    """
    import yaml as _yaml

    logger.info("Using init_cfg_profile: %s", facts.init_cfg_profile)

    # Load profile definitions
    profiles_file = os.path.join(ANSIBLE_DIR, "vars", "init_cfg_profiles.yml")
    with open(profiles_file, 'r') as f:
        profiles = _yaml.safe_load(f) or {}

    cfg_profile = facts.init_cfg_profile
    actual_config = profiles.get(cfg_profile)
    if not actual_config:
        raise ValueError(f"init_cfg_profile '{cfg_profile}' not found in {profiles_file}")

    logger.info("Config profile value: %s", actual_config)
    duthost.copy(src=actual_config, dest="/tmp/config_db.json")
    duthost.shell("cat /tmp/config_db.json > /etc/sonic/config_db.json")


def _deploy_certs(duthost, localhost, facts):
    """
    Deploy telemetry and restapi certificates via openssl.

    YAML lines 440-535: reads cert content from vault vars,
    generates certs using openssl req on the DUT.
    """
    # Telemetry certs
    telemetry_certs = getattr(duthost, 'telemetry_certs', None)
    if telemetry_certs:
        logger.info("Deploying telemetry certificates")
        _deploy_cert_set(duthost, telemetry_certs)

    # RestAPI certs
    restapi_certs = getattr(duthost, 'restapi_certs', None)
    if restapi_certs:
        logger.info("Deploying restapi certificates")
        _deploy_cert_set(duthost, restapi_certs)


def _deploy_cert_set(duthost, certs):
    """
    Deploy a set of certificates via openssl.

    Mirrors deploy_certs.yml — generates certs with openssl req command.
    """
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
    duthost.file(path=dir_path, state="directory", mode='0755')

    # Generate server cert using openssl
    if server_key and server_crt and subject_server:
        duthost.shell(
            f'openssl req -x509 -sha256 -nodes -newkey rsa:2048 '
            f'-keyout "{server_key}" -subj "/CN={subject_server}" '
            f'-out "{server_crt}"',
            module_ignore_errors=True
        )

    # Generate dsmsroot cert using openssl
    if dsmsroot_key and dsmsroot_cer and subject_client:
        duthost.shell(
            f'openssl req -x509 -sha256 -nodes -newkey rsa:2048 '
            f'-keyout "{dsmsroot_key}" -subj "/CN={subject_client}" '
            f'-out "{dsmsroot_cer}"',
            module_ignore_errors=True
        )


def _configure_docker_proxy(duthost, facts):
    """Set up docker HTTP proxy if configured (YAML lines 540-560)."""
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


def _configure_core_analyzer(duthost, facts):
    """
    Configure core analyzer with vault secrets.

    YAML lines 658-706: reads corefile_uploader vault var, writes
    account_key and https_proxy into core_analyzer.rc.json,
    enables and starts core_uploader.service.
    """
    # Check if rc.json exists
    result = duthost.shell(
        "test -f /etc/sonic/core_analyzer.rc.json && echo exists",
        module_ignore_errors=True
    )
    if 'exists' not in result.get('stdout', ''):
        return

    corefile_uploader = facts.corefile_uploader
    if not corefile_uploader:
        return

    # Read account key from vault vars
    core_key = ""
    core_proxy = ""
    try:
        azure_storage = corefile_uploader.get('azure_sonic_core_storage', {})
        core_key = azure_storage.get('account_key', '')
    except (AttributeError, KeyError):
        pass

    try:
        env = corefile_uploader.get('env', {})
        core_proxy = env.get('https_proxy', '')
    except (AttributeError, KeyError):
        pass

    # Write account_key into core_analyzer.rc.json
    if core_key:
        duthost.lineinfile(
            name="/etc/sonic/core_analyzer.rc.json",
            regexp=r'(^.*)account_key',
            line=r'\1account_key": "' + core_key + '",',
            backrefs=True,
            module_ignore_errors=True
        )

    # Write https_proxy into core_analyzer.rc.json
    if core_proxy:
        duthost.lineinfile(
            name="/etc/sonic/core_analyzer.rc.json",
            regexp=r'(^.*)https_proxy',
            line=r'\1https_proxy": "' + core_proxy + '"',
            backrefs=True,
            module_ignore_errors=True
        )

    # Enable and start core_uploader service
    if core_key:
        duthost.shell("systemctl enable core_uploader.service",
                      module_ignore_errors=True)
        duthost.shell("systemctl start core_uploader.service",
                      module_ignore_errors=True)


def _adjust_soc_properties(duthost, facts):
    """
    Adjust BCM DNX SOC properties for Nokia-IXR7250 T2.

    YAML lines 769-799: adjusts appl_param_active_links_thr_high
    based on active fabric cards.
    """
    if not ('t2' in facts.topo
            and facts.switch_type in ('voq', 'fabric')
            and 'Nokia-IXR7250' in facts.hwsku):
        return

    try:
        if facts.sup_dut and duthost.hostname == facts.sup_dut:
            # Supervisor: count active fabric cards
            result = duthost.shell(
                'show chassis module status | grep "FABRIC-CARD" | grep "Online" | wc -l',
                module_ignore_errors=True
            )
            fabric_links = int(result.get('stdout', '0').strip()) * 24
            # Store for linecards to reference
            facts._fabric_links = fabric_links
        elif duthost.hostname != facts.sup_dut:
            # Linecard: adjust SOC properties
            fabric_links = getattr(facts, '_fabric_links', 0)
            if fabric_links <= 0:
                return

            sonic_hw_platform = getattr(duthost, 'sonic_hw_platform', '')
            device_config_path = f"/usr/share/sonic/device/{sonic_hw_platform}/{facts.hwsku}/*"

            # Get current high threshold
            result = duthost.shell(
                f"grep -ir appl_param_active_links_thr_high {device_config_path} "
                f"| sed -n 's/.*appl_param_active_links_thr_high=\\([0-9]*\\).*/\\1/p'",
                module_ignore_errors=True
            )
            if result.get('rc') != 0 or not result.get('stdout', '').strip():
                return

            lines = result['stdout'].strip().split('\n')
            high_active_links = max(int(x) for x in lines if x.strip())
            new_active_links = int((2/3) * fabric_links)

            if high_active_links > fabric_links:
                duthost.shell(
                    f"grep -rl appl_param_active_links_thr_high {device_config_path} "
                    f"| xargs sudo sed -i 's/appl_param_active_links_thr_high=.*/"
                    f"appl_param_active_links_thr_high={new_active_links}/g'",
                    module_ignore_errors=True
                )
    except Exception:
        logger.debug("SOC properties adjustment skipped (may not be defined)")


def _deploy_macsec_profile(duthost, facts):
    """
    Deploy macsec profile for T2 topology.

    YAML lines 810-823.
    """
    logger.info("Deploying macsec profile for T2")

    # Copy profile.json
    profile_src = os.path.join(
        os.path.dirname(ANSIBLE_DIR), "tests", "common", "macsec", "profile.json"
    )
    if os.path.exists(profile_src):
        duthost.copy(src=profile_src, dest="/tmp/profile.json")

    # Copy golden_config_db_t2 template
    golden_t2_src = os.path.join(ANSIBLE_DIR, "templates", "golden_config_db_t2.j2")
    if os.path.exists(golden_t2_src):
        duthost.copy(src=golden_t2_src, dest="/tmp/golden_config_db_t2.j2")

    # Generate golden_config_db with macsec profile
    macsec_profile = getattr(duthost, 'macsec_profile', None)
    if macsec_profile:
        duthost.generate_golden_config_db(
            topo_name=facts.topo,
            macsec_profile=macsec_profile,
            num_asics=facts.num_asics,
            module_ignore_errors=True
        )


def _load_minigraph(duthost, facts):
    """
    Load minigraph on DUT.

    YAML lines 851-888: handles normal mode, IPv6 async mode,
    and --override_config fallback.
    """
    hostname = duthost.hostname
    logger.info("Loading minigraph on %s", hostname)

    if not facts.ipv6_only_mgmt:
        # Normal mode
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
            module_async=300,
            module_poll=0,
            module_ignore_errors=True
        )
        # Pause briefly to allow load to start
        time.sleep(5)
