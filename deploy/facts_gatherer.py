"""
Gather facts needed for minigraph generation and deployment.

Replaces the facts-gathering section (~first 400 lines) of
ansible/config_sonic_basedon_testbed.yml by calling the same custom
Ansible modules via pytest-ansible's ad-hoc runner.
"""

import logging
import os
import yaml
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

ANSIBLE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "ansible")


@dataclass
class DeployFacts:
    """All facts needed for minigraph generation and deployment."""
    testbed_facts: Dict[str, Any] = field(default_factory=dict)
    topo: str = ""
    topo_config: Dict[str, Any] = field(default_factory=dict)

    # Topology variables from vars/topo_{{ topo }}.yml
    topo_vars: Dict[str, Any] = field(default_factory=dict)
    configuration: Dict[str, Any] = field(default_factory=dict)
    configuration_properties: Dict[str, Any] = field(default_factory=dict)

    # Port info
    port_alias: List[str] = field(default_factory=list)
    port_alias_asic: List[str] = field(default_factory=list)
    port_index_map: Dict[str, Any] = field(default_factory=dict)
    port_alias_map: Dict[str, Any] = field(default_factory=dict)
    front_panel_asic_ifnames: List[str] = field(default_factory=list)
    front_panel_asic_ifs_asic_id: List[str] = field(default_factory=list)

    # VM info
    vm_info: Dict[str, Any] = field(default_factory=dict)
    vlan: Dict[str, Any] = field(default_factory=dict)
    tunnel: Dict[str, Any] = field(default_factory=dict)
    dual_tor: Dict[str, Any] = field(default_factory=dict)
    mux_cable: Dict[str, Any] = field(default_factory=dict)
    conn_graph: Dict[str, Any] = field(default_factory=dict)
    fabric_info: Dict[str, Any] = field(default_factory=dict)

    # Scalar facts computed during gathering
    dut_index: int = 0
    num_asics: int = 1
    hwsku: str = ""
    vm_base: str = ""
    is_ixia_testbed: bool = False
    is_light_mode: bool = False
    vm_topo_config: Dict[str, Any] = field(default_factory=dict)
    dut_type: str = "kvm"

    # Host/interface mappings
    host_if_indexes: List[int] = field(default_factory=list)
    vlan_intfs: List[str] = field(default_factory=list)
    interface_to_vms: List[Dict] = field(default_factory=list)
    ifindex_to_vms: List[int] = field(default_factory=list)
    intf_names: Dict[str, List[str]] = field(default_factory=dict)
    vm_asic_ifnames: Dict[str, List[str]] = field(default_factory=dict)
    vm_asic_ids: Dict[str, List[str]] = field(default_factory=dict)
    portchannel_config: Dict[str, Any] = field(default_factory=dict)

    # Service config
    tacacs_passkey: str = ""
    tacacs_servers: List[str] = field(default_factory=list)
    tacacs_group: str = ""
    tacacs_enabled_by_default: bool = True
    use_ptf_tacacs: bool = True
    snmp_rocommunity: str = "public"
    snmp_servers: List[str] = field(default_factory=list)
    ntp_servers: List[str] = field(default_factory=list)
    dns_servers: List[str] = field(default_factory=list)
    syslog_servers: List[str] = field(default_factory=list)
    forced_mgmt_routes: List[str] = field(default_factory=list)

    # IPv6 management
    ipv6_only_mgmt: bool = False
    sonic_mgmt_ipv6: bool = False
    target_mgmt_ip: str = ""
    mgmt_subnet_mask_length: int = 24
    original_ipv4_address: str = ""

    # VoQ / chassis
    voq_chassis: bool = False
    switch_type: Optional[str] = None
    card_type: str = "fixed"
    all_sysports: List[Any] = field(default_factory=list)
    all_inbands: Dict[str, Any] = field(default_factory=dict)
    all_inbands_ipv6: Dict[str, Any] = field(default_factory=dict)
    all_loopback4096: Dict[str, Any] = field(default_factory=dict)
    all_loopback4096_ipv6: Dict[str, Any] = field(default_factory=dict)
    all_slots: Dict[str, Any] = field(default_factory=dict)
    sup_dut: Optional[str] = None
    asics_present: List[str] = field(default_factory=list)
    switchids: List[str] = field(default_factory=list)
    slot_num: Optional[str] = None
    sort_by_index: bool = True

    # Feature flags (edge cases)
    enable_tunnel_qos_remap: bool = False
    enable_compute_ai_deployment: bool = False
    init_cfg_profile: Optional[str] = None
    start_topo_service: bool = False

    # Corefile uploader
    corefile_uploader: Dict[str, Any] = field(default_factory=dict)

    # VoQ asic topo config
    asic_topo_config: Dict[str, Any] = field(default_factory=dict)


def gather_facts(localhost, duthost, testbed_name, testbed_file,
                 vm_file="veos", ipv6_only_mgmt=False, deploy=True,
                 all_duthosts=None):
    """
    Gather all facts needed for minigraph generation and deployment.

    Args:
        localhost: Localhost ansible host (for delegate_to localhost tasks)
        duthost: SonicHost ansible host (the DUT)
        testbed_name: Name of the testbed (e.g. "vms-kvm-t0")
        testbed_file: Path to testbed YAML/CSV file
        vm_file: Virtual machine inventory file (default: "veos")
        ipv6_only_mgmt: Whether to use IPv6-only management
        deploy: Whether we're deploying (affects port_alias source)
        all_duthosts: List of all DUT hosts in this testbed (for VoQ cross-DUT facts)

    Returns:
        DeployFacts with all gathered information
    """
    facts = DeployFacts()
    facts.ipv6_only_mgmt = ipv6_only_mgmt

    # ── 1. Testbed facts ─────────────────────────────────────────────
    logger.info("Gathering testbed facts for '%s' from '%s'", testbed_name, testbed_file)
    tb_result = localhost.test_facts(
        testbed_name=testbed_name,
        testbed_file=testbed_file
    )
    facts.testbed_facts = tb_result['ansible_facts']['testbed_facts']

    # Validate DUT belongs to this testbed
    if duthost.hostname not in facts.testbed_facts['duts']:
        raise ValueError(
            f"DUT {duthost.hostname} does not belong to testbed {testbed_name}. "
            f"DUTs in testbed: {facts.testbed_facts['duts']}"
        )

    # ── 2. IPv6 management configuration ────────────────────────────
    # YAML lines 89-125: include_vars ipv6.yml, override service configs
    facts.sonic_mgmt_ipv6 = ipv6_only_mgmt
    if ipv6_only_mgmt:
        _load_ipv6_vars(duthost, facts)

    # ── 3. Basic facts ───────────────────────────────────────────────
    facts.topo = facts.testbed_facts['topo']
    facts.dut_index = facts.testbed_facts['duts_map'].get(duthost.hostname, 0)
    facts.num_asics = getattr(duthost, 'num_asics', 1) or 1
    facts.hwsku = duthost.facts.get('hwsku', 'Force10-S6000')
    facts.dut_type = getattr(duthost, 'type', 'kvm') or 'kvm'
    facts.card_type = getattr(duthost, 'card_type', 'fixed') or 'fixed'
    facts.switch_type = getattr(duthost, 'switch_type', None)
    facts.switchids = getattr(duthost, 'switchids', []) or []
    facts.slot_num = getattr(duthost, 'slot_num', None)
    facts.start_topo_service = getattr(duthost, 'start_topo_service', False) or False
    facts.init_cfg_profile = getattr(duthost, 'init_cfg_profile', None)

    facts.vm_base = facts.testbed_facts.get('vm_base', '')
    if facts.vm_base is None:
        facts.vm_base = ''

    ptf_image = facts.testbed_facts.get('ptf_image_name', '')
    facts.is_ixia_testbed = (ptf_image == 'docker-keysight-api-server')

    # Light mode for smartswitch topologies
    if facts.topo in ["t1-smartswitch-ha", "t1-28-lag", "smartswitch-t1", "t1-48-lag"]:
        facts.is_light_mode = True

    # Sort by index: false for voq+kvm
    facts.sort_by_index = True
    if facts.switch_type == 'voq' and facts.dut_type == 'kvm':
        facts.sort_by_index = False

    logger.info("Testbed: %s, topo: %s, DUT: %s (index %d), hwsku: %s, "
                "switch_type: %s, card_type: %s",
                testbed_name, facts.topo, duthost.hostname, facts.dut_index,
                facts.hwsku, facts.switch_type, facts.card_type)

    # ── 4. Find supervisor DUT and asics_present ─────────────────────
    # YAML lines 196-201: find card_type=='supervisor' DUT
    if all_duthosts:
        for dh in all_duthosts:
            ct = getattr(dh, 'card_type', None)
            if ct == 'supervisor':
                facts.sup_dut = dh.hostname
                facts.asics_present = getattr(dh, 'asics_present', []) or []
                break

    # ── 5. Topology facts ────────────────────────────────────────────
    logger.info("Gathering topology facts for topo=%s", facts.topo)
    topo_result = localhost.topo_facts(
        topo=facts.topo,
        hwsku=facts.hwsku,
        testbed_name=testbed_name,
        asics_present=facts.asics_present,
        card_type=facts.card_type,
    )
    facts.topo_config = topo_result['ansible_facts'].get('vm_topo_config', {})
    facts.vm_topo_config = facts.topo_config
    facts.asic_topo_config = topo_result['ansible_facts'].get('asic_topo_config', {})

    # ── 6. Load topology variables from vars/topo_{{ topo }}.yml ─────
    # YAML line 579: include_vars: "vars/topo_{{ topo }}.yml"
    # These are needed by the Jinja2 minigraph templates
    _load_topo_vars(facts)

    # ── 7. Connection graph ──────────────────────────────────────────
    logger.info("Gathering connection graph facts")
    conn_kwargs = {
        'host': duthost.hostname,
        'module_ignore_errors': True,
    }
    if facts.is_ixia_testbed and facts.testbed_facts.get('inv_name'):
        conn_kwargs['group'] = facts.testbed_facts['inv_name']
    if facts.forced_mgmt_routes:
        conn_kwargs['forced_mgmt_routes'] = facts.forced_mgmt_routes
    try:
        conn_result = localhost.conn_graph_facts(**conn_kwargs)
        facts.conn_graph = conn_result.get('ansible_facts', {})
    except Exception:
        logger.warning("Could not get connection graph facts (non-fatal)")

    # ── 8. Port alias mapping ────────────────────────────────────────
    logger.info("Gathering port alias info for hwsku=%s", facts.hwsku)
    if deploy:
        # Get from DUT when deploying
        port_result = duthost.port_alias(
            hwsku=facts.hwsku,
            card_type=facts.card_type,
            hostname=duthost.hostname,
            switchids=facts.switchids,
            num_asic=facts.num_asics,
            sort_by_index=facts.sort_by_index,
        )
    else:
        # Get from localhost when not deploying
        port_result = localhost.port_alias(
            hwsku=facts.hwsku,
            num_asic=facts.num_asics,
            card_type=facts.card_type,
            hostname=duthost.hostname,
            switchids=facts.switchids,
            slotid=facts.slot_num,
            sort_by_index=facts.sort_by_index,
        )
    port_facts = port_result.get('ansible_facts', {})
    facts.port_alias = port_facts.get('port_alias', [])
    facts.port_alias_asic = port_facts.get('port_alias_asic', [])
    facts.port_index_map = port_facts.get('port_index_map', {})
    facts.port_alias_map = port_facts.get('port_alias_map', {})
    facts.front_panel_asic_ifnames = port_facts.get('front_panel_asic_ifnames', [])
    facts.front_panel_asic_ifs_asic_id = port_facts.get('front_panel_asic_ifs_asic_id', [])

    # ── 9. Fabric info ───────────────────────────────────────────────
    logger.info("Gathering fabric info")
    fabric_result = localhost.fabric_info(
        num_fabric_asic=getattr(duthost, 'num_fabric_asics', 0) or 0,
        asics_host_basepfx=getattr(duthost, 'asics_host_ip', None),
        asics_host_basepfx6=getattr(duthost, 'asics_host_ipv6', None),
        module_ignore_errors=True
    )
    facts.fabric_info = fabric_result.get('ansible_facts', {})

    # ── 10. VoQ chassis cross-DUT facts ──────────────────────────────
    # YAML lines 265-295: loop over ansible_play_batch to aggregate
    # sysports, inbands, loopback4096, slots from all DUTs
    if all_duthosts:
        _gather_voq_cross_dut_facts(facts, all_duthosts)

    # VoQ asic_topo_config remap for slot
    if (facts.switch_type == 'voq' and facts.slot_num is not None
            and facts.asic_topo_config):
        new_config = {facts.slot_num: facts.asic_topo_config.get('slot0', {})}
        facts.asic_topo_config = new_config

    # Set voq_chassis flag
    facts.voq_chassis = (
        facts.switch_type == 'voq'
        and getattr(duthost, 'voq_inband_ip', None) is not None
    )

    # ── 11. VM info ──────────────────────────────────────────────────
    is_vm_topo = ('ptf' not in facts.topo) and ('cable' not in facts.topo)
    if is_vm_topo:
        logger.info("Gathering VM info (base_vm=%s)", facts.vm_base)
        vm_result = localhost.testbed_vm_info(
            base_vm=facts.vm_base,
            topo=facts.topo,
            vm_file=vm_file,
            servers_info=facts.testbed_facts.get('servers', {})
        )
        facts.vm_info = vm_result.get('ansible_facts', {})

    # ── 12. Host interfaces ──────────────────────────────────────────
    if 'host_interfaces_by_dut' in facts.topo_config:
        all_host_ifs = facts.topo_config['host_interfaces_by_dut'][facts.dut_index]
        disabled_ifs = facts.topo_config.get(
            'disabled_host_interfaces_by_dut', [[]]
        )
        if facts.dut_index < len(disabled_ifs):
            disabled = disabled_ifs[facts.dut_index]
        else:
            disabled = []
        facts.host_if_indexes = [i for i in all_host_ifs if i not in disabled]

        # VLAN interfaces for T0 topology
        dut_type = facts.topo_config.get('dut_type', '')
        if 'tor' in dut_type.lower():
            facts.vlan_intfs = [facts.port_alias[i] for i in facts.host_if_indexes]

    # ── 13. Interface-to-VM mappings ─────────────────────────────────
    if 'cable' not in facts.topo and 'vm' in facts.topo_config:
        for vm_name, vm_config in sorted(facts.topo_config['vm'].items()):
            ports = vm_config.get('interface_indexes', [[]])[facts.dut_index]
            facts.interface_to_vms.append({'name': vm_name, 'ports': ports})
            facts.ifindex_to_vms.extend(ports)

            # Map VM names to interface names
            for port_idx in ports:
                if vm_name not in facts.intf_names:
                    facts.intf_names[vm_name] = []
                facts.intf_names[vm_name].append(facts.port_alias[port_idx])

            # ASIC interface names if available
            if facts.front_panel_asic_ifnames:
                for port_idx in ports:
                    if vm_name not in facts.vm_asic_ifnames:
                        facts.vm_asic_ifnames[vm_name] = []
                        facts.vm_asic_ids[vm_name] = []
                    facts.vm_asic_ifnames[vm_name].append(
                        facts.front_panel_asic_ifnames[port_idx]
                    )
                    facts.vm_asic_ids[vm_name].append(
                        facts.front_panel_asic_ifs_asic_id[port_idx]
                    )

    # ── 14. VLAN config for T0 ───────────────────────────────────────
    dut_type = facts.topo_config.get('dut_type', '')
    vlan_config_arg = facts.vlan.get('vlan_config', None)
    if 'host_interfaces_by_dut' in facts.topo_config and 'tor' in dut_type.lower():
        logger.info("Gathering VLAN config for T0 topology")
        vlan_result = localhost.vlan_config(
            vm_topo_config=facts.topo_config,
            port_alias=facts.port_alias,
            vlan_config=vlan_config_arg,
            module_ignore_errors=True
        )
        facts.vlan = vlan_result.get('ansible_facts', {})

    # Portchannel config for T0
    if 'host_interfaces_by_dut' in facts.topo_config and 'tor' in dut_type.lower():
        facts.portchannel_config = facts.topo_config.get('DUT', {}).get(
            'portchannel_config', {}
        )

    # ── 15. Tunnel config ────────────────────────────────────────────
    logger.info("Gathering tunnel config")
    tunnel_config_arg = facts.tunnel.get('tunnel_config', None)
    tunnel_result = localhost.tunnel_config(
        vm_topo_config=facts.topo_config,
        tunnel_config=tunnel_config_arg,
        module_ignore_errors=True
    )
    facts.tunnel = tunnel_result.get('ansible_facts', {})

    # ── 16. Dualtor facts ────────────────────────────────────────────
    if 'dualtor' in facts.topo or 'cable' in facts.topo:
        logger.info("Gathering dual ToR facts")
        # Need hostvars for all DUTs
        hostvars = {}
        if all_duthosts:
            for dh in all_duthosts:
                hostvars[dh.hostname] = getattr(dh, 'host', {})
        dt_result = localhost.dual_tor_facts(
            hostname=duthost.hostname,
            testbed_facts=facts.testbed_facts,
            hostvars=hostvars,
            vm_config=facts.topo_config,
            port_alias=facts.port_alias,
            vlan_intfs=facts.vlan_intfs,
            vlan_config=facts.vlan.get('vlan_config', None),
            module_ignore_errors=True
        )
        facts.dual_tor = dt_result.get('ansible_facts', {})

    if 'dualtor' in facts.topo:
        logger.info("Gathering mux cable facts")
        mux_result = localhost.mux_cable_facts(
            topo_name=facts.topo,
            vlan_config=facts.vlan.get('vlan_config', None),
            module_ignore_errors=True
        )
        facts.mux_cable = mux_result.get('ansible_facts', {})

    # ── 17. Feature flags ────────────────────────────────────────────
    # enable_tunnel_qos_remap for T1 dualtor
    hwsku_list_dualtor_t1 = ['ACS-MSN4600C', 'Arista-7260CX3-C64']
    topo_dut_type = facts.topo_config.get('dut_type', '').lower()
    if (topo_dut_type in ('leafrouter', 'backendleafrouter')
            and facts.hwsku in hwsku_list_dualtor_t1
            and not facts.is_ixia_testbed):
        facts.enable_tunnel_qos_remap = True

    # enable_compute_ai_deployment for Cisco HWSKUs
    hwsku_list_compute_ai = [
        'Cisco-8111-O64', 'Cisco-8111-O32', 'Cisco-8122-O64',
        'Cisco-8122-O64S2', 'Cisco-8122-O128'
    ]
    if (facts.hwsku in hwsku_list_compute_ai
            and not facts.is_ixia_testbed
            and 'isolated' in facts.topo):
        facts.enable_compute_ai_deployment = True

    # ── 18. TACACS defaults ──────────────────────────────────────────
    facts.use_ptf_tacacs = True
    facts.tacacs_enabled_by_default = True

    # Load service config from host vars (group_vars)
    facts.tacacs_passkey = getattr(duthost, 'tacacs_passkey', '') or ''
    facts.snmp_rocommunity = getattr(duthost, 'snmp_rocommunity', 'public') or 'public'
    facts.ntp_servers = getattr(duthost, 'ntp_servers', []) or []
    facts.dns_servers = getattr(duthost, 'dns_servers', []) or []
    facts.syslog_servers = getattr(duthost, 'syslog_servers', []) or []
    facts.snmp_servers = getattr(duthost, 'snmp_servers', []) or []
    facts.tacacs_group = getattr(duthost, 'tacacs_group', '') or ''
    facts.forced_mgmt_routes = getattr(duthost, 'forced_mgmt_routes', []) or []
    facts.corefile_uploader = getattr(duthost, 'corefile_uploader', {}) or {}

    # Update TACACS server to PTF IP
    ptf_ip = facts.testbed_facts.get('ptf_ip', '')
    if not facts.sonic_mgmt_ipv6:
        if facts.testbed_facts.get('multi_servers_tacacs_ip'):
            facts.tacacs_servers = [facts.testbed_facts['multi_servers_tacacs_ip']]
        elif ptf_ip:
            facts.tacacs_servers = [ptf_ip]
    else:
        ptf_ipv6 = facts.testbed_facts.get('ptf_ipv6', '')
        if not ptf_ipv6:
            raise ValueError(
                f"IPv6-only management mode requested but ptf_ipv6 is not "
                f"configured in testbed file for {duthost.hostname}"
            )
        # Strip CIDR prefix
        facts.tacacs_servers = [ptf_ipv6.split('/')[0]]

    logger.info("Facts gathering complete for %s", duthost.hostname)
    return facts


def _load_topo_vars(facts):
    """
    Load topology variables from vars/topo_{{ topo }}.yml.

    YAML line 579: include_vars: "vars/topo_{{ topo }}.yml"

    These variables (topology, configuration, configuration_properties)
    are used by the Jinja2 minigraph templates.
    """
    topo_file = os.path.join(ANSIBLE_DIR, "vars", f"topo_{facts.topo}.yml")
    if not os.path.exists(topo_file):
        logger.warning("Topo vars file not found: %s", topo_file)
        return

    logger.info("Loading topology variables from %s", topo_file)
    with open(topo_file, 'r') as f:
        topo_vars = yaml.safe_load(f) or {}

    facts.topo_vars = topo_vars
    facts.configuration = topo_vars.get('configuration', {})
    facts.configuration_properties = topo_vars.get('configuration_properties', {})


def _load_ipv6_vars(duthost, facts):
    """
    Load IPv6 management configuration from group_vars.

    YAML lines 89-125: include_vars ipv6.yml + override service configs.
    """
    facts.sonic_mgmt_ipv6 = True

    # Try to load IPv6 group_vars
    # In the YAML playbook: include_vars: "group_vars/{{ group_names[1] }}/ipv6.yml"
    group_names = getattr(duthost, 'group_names', [])
    if len(group_names) > 1:
        ipv6_file = os.path.join(
            ANSIBLE_DIR, "group_vars", group_names[1], "ipv6.yml"
        )
        if os.path.exists(ipv6_file):
            logger.info("Loading IPv6 vars from %s", ipv6_file)
            with open(ipv6_file, 'r') as f:
                ipv6_vars = yaml.safe_load(f) or {}

            # Override service configs with IPv6 variants
            facts.ntp_servers = ipv6_vars.get(
                'ntp_servers_ipv6',
                getattr(duthost, 'ntp_servers', []) or []
            )
            facts.dns_servers = ipv6_vars.get(
                'dns_servers_ipv6',
                getattr(duthost, 'dns_servers', []) or []
            )
            facts.syslog_servers = ipv6_vars.get(
                'syslog_servers_ipv6',
                getattr(duthost, 'syslog_servers', []) or []
            )
            facts.tacacs_servers = ipv6_vars.get(
                'tacacs_servers_ipv6',
                getattr(duthost, 'tacacs_servers', []) or []
            )
            facts.snmp_servers = ipv6_vars.get(
                'snmp_servers_ipv6',
                getattr(duthost, 'snmp_servers', []) or []
            )
            facts.forced_mgmt_routes = ipv6_vars.get(
                'forced_mgmt_routes_ipv6',
                getattr(duthost, 'forced_mgmt_routes', []) or []
            )
            facts.tacacs_group = ipv6_vars.get(
                'tacacs_group_ipv6',
                getattr(duthost, 'tacacs_group', '') or ''
            )
            facts.tacacs_passkey = ipv6_vars.get(
                'tacacs_passkey_ipv6',
                getattr(duthost, 'tacacs_passkey', '') or ''
            )
            facts.tacacs_enabled_by_default = ipv6_vars.get(
                'tacacs_enabled_by_default_ipv6',
                True
            )

    # Set target management IP for IPv6
    ansible_hostv6 = getattr(duthost, 'ansible_hostv6', None)
    if ansible_hostv6:
        facts.target_mgmt_ip = ansible_hostv6
        facts.mgmt_subnet_mask_length = 64


def _gather_voq_cross_dut_facts(facts, all_duthosts):
    """
    Gather VoQ chassis facts that require cross-DUT aggregation.

    YAML lines 265-295: loops over ansible_play_batch to combine
    sysports, inband IPs, loopback4096, and slot info from all DUTs.
    """
    for dh in all_duthosts:
        hostname = dh.hostname

        # Aggregate sysports
        sysports = getattr(dh, 'sysports', None)
        if sysports:
            facts.all_sysports.extend(sysports)

        # Aggregate inband IPs (v4)
        voq_inband_ip = getattr(dh, 'voq_inband_ip', None)
        if voq_inband_ip:
            facts.all_inbands[hostname] = voq_inband_ip

        # Aggregate inband IPs (v6)
        voq_inband_ipv6 = getattr(dh, 'voq_inband_ipv6', None)
        if voq_inband_ipv6:
            facts.all_inbands_ipv6[hostname] = voq_inband_ipv6

        # Aggregate loopback4096 (v4)
        loopback4096_ip = getattr(dh, 'loopback4096_ip', None)
        if loopback4096_ip:
            facts.all_loopback4096[hostname] = loopback4096_ip

        # Aggregate loopback4096 (v6)
        loopback4096_ipv6 = getattr(dh, 'loopback4096_ipv6', None)
        if loopback4096_ipv6:
            facts.all_loopback4096_ipv6[hostname] = loopback4096_ipv6

        # Aggregate slot info
        slot_num = getattr(dh, 'slot_num', None)
        if slot_num is not None:
            facts.all_slots[hostname] = slot_num
