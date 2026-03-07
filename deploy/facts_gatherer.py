"""
Gather facts needed for minigraph generation and deployment.

Replaces the facts-gathering section (~first 250 lines) of
ansible/config_sonic_basedon_testbed.yml by calling the same custom
Ansible modules via pytest-ansible's ad-hoc runner.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from tests.common.testbed import TestbedInfo

logger = logging.getLogger(__name__)


@dataclass
class DeployFacts:
    """All facts needed for minigraph generation and deployment."""
    testbed: Dict[str, Any] = field(default_factory=dict)
    testbed_facts: Dict[str, Any] = field(default_factory=dict)
    topo: str = ""
    topo_config: Dict[str, Any] = field(default_factory=dict)
    port_alias: List[str] = field(default_factory=list)
    port_alias_asic: List[str] = field(default_factory=list)
    port_index_map: Dict[str, Any] = field(default_factory=dict)
    front_panel_asic_ifnames: List[str] = field(default_factory=list)
    front_panel_asic_ifs_asic_id: List[str] = field(default_factory=list)
    vm_info: Dict[str, Any] = field(default_factory=dict)
    vlan: Dict[str, Any] = field(default_factory=dict)
    tunnel: Dict[str, Any] = field(default_factory=dict)
    dual_tor: Dict[str, Any] = field(default_factory=dict)
    mux_cable: Dict[str, Any] = field(default_factory=dict)
    golden_config: Dict[str, Any] = field(default_factory=dict)
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

    # Host/interface mappings
    host_if_indexes: List[int] = field(default_factory=list)
    vlan_intfs: List[str] = field(default_factory=list)
    interface_to_vms: List[Dict] = field(default_factory=list)
    ifindex_to_vms: List[int] = field(default_factory=list)
    intf_names: Dict[str, List[str]] = field(default_factory=dict)
    vm_asic_ifnames: Dict[str, List[str]] = field(default_factory=dict)
    vm_asic_ids: Dict[str, List[str]] = field(default_factory=dict)

    # Service config
    tacacs_passkey: str = ""
    tacacs_servers: List[str] = field(default_factory=list)
    tacacs_enabled_by_default: bool = True
    use_ptf_tacacs: bool = True
    snmp_rocommunity: str = "public"
    ntp_servers: List[str] = field(default_factory=list)
    dns_servers: List[str] = field(default_factory=list)
    syslog_servers: List[str] = field(default_factory=list)

    # IPv6 management
    ipv6_only_mgmt: bool = False
    sonic_mgmt_ipv6: bool = False

    # VoQ / chassis
    voq_chassis: bool = False
    switch_type: Optional[str] = None
    all_sysports: List[Any] = field(default_factory=list)
    all_inbands: Dict[str, Any] = field(default_factory=dict)
    all_inbands_ipv6: Dict[str, Any] = field(default_factory=dict)


def gather_facts(localhost, duthost, testbed_name, testbed_file,
                 vm_file="veos", ipv6_only_mgmt=False):
    """
    Gather all facts needed for minigraph generation and deployment.

    This mirrors the first half of config_sonic_basedon_testbed.yml:
    testbed parsing, topology facts, port aliases, VM info, VLAN config, etc.

    Args:
        localhost: Localhost ansible host (for delegate_to localhost tasks)
        duthost: SonicHost ansible host (the DUT)
        testbed_name: Name of the testbed (e.g. "vms-kvm-t0")
        testbed_file: Path to testbed YAML/CSV file
        vm_file: Virtual machine inventory file (default: "veos")
        ipv6_only_mgmt: Whether to use IPv6-only management

    Returns:
        DeployFacts with all gathered information
    """
    facts = DeployFacts()
    facts.ipv6_only_mgmt = ipv6_only_mgmt

    # ── 1. Testbed facts ─────────────────────────────────────────────
    # Use the test_facts Ansible module (ansible/library/test_facts.py)
    # This is what config_sonic_basedon_testbed.yml calls as its first step.
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

    # ── 2. Basic facts ───────────────────────────────────────────────
    facts.topo = facts.testbed_facts['topo']
    facts.dut_index = facts.testbed_facts['duts_map'].get(duthost.hostname, 0)
    facts.num_asics = getattr(duthost, 'num_asics', 1) or 1
    facts.hwsku = duthost.facts.get('hwsku', 'Force10-S6000')

    facts.vm_base = facts.testbed_facts.get('vm_base', '')
    if facts.vm_base is None:
        facts.vm_base = ''

    ptf_image = facts.testbed_facts.get('ptf_image_name', '')
    facts.is_ixia_testbed = (ptf_image == 'docker-keysight-api-server')

    # Light mode for smartswitch topologies
    if facts.topo in ["t1-smartswitch-ha", "t1-28-lag", "smartswitch-t1", "t1-48-lag"]:
        facts.is_light_mode = True

    logger.info("Testbed: %s, topo: %s, DUT: %s (index %d), hwsku: %s",
                testbed_name, facts.topo, duthost.hostname, facts.dut_index, facts.hwsku)

    # ── 3. Topology facts ────────────────────────────────────────────
    logger.info("Gathering topology facts for topo=%s", facts.topo)
    topo_result = localhost.topo_facts(
        topo=facts.topo,
        hwsku=facts.hwsku,
        testbed_name=testbed_name
    )
    facts.topo_config = topo_result['ansible_facts'].get('vm_topo_config', {})
    facts.vm_topo_config = facts.topo_config

    # ── 4. Connection graph ──────────────────────────────────────────
    logger.info("Gathering connection graph facts")
    try:
        conn_result = localhost.conn_graph_facts(
            host=duthost.hostname,
            module_ignore_errors=True
        )
        facts.conn_graph = conn_result.get('ansible_facts', {})
    except Exception:
        logger.warning("Could not get connection graph facts (non-fatal)")

    # ── 5. Port alias mapping ────────────────────────────────────────
    # When deploying, get port info from DUT; otherwise use local data
    logger.info("Gathering port alias info for hwsku=%s", facts.hwsku)
    port_result = duthost.port_alias(
        hwsku=facts.hwsku,
        card_type=getattr(duthost, 'card_type', 'fixed'),
        hostname=duthost.hostname,
        num_asic=facts.num_asics
    )
    port_facts = port_result.get('ansible_facts', {})
    facts.port_alias = port_facts.get('port_alias', [])
    facts.port_alias_asic = port_facts.get('port_alias_asic', [])
    facts.port_index_map = port_facts.get('port_index_map', {})
    facts.front_panel_asic_ifnames = port_facts.get('front_panel_asic_ifnames', [])
    facts.front_panel_asic_ifs_asic_id = port_facts.get('front_panel_asic_ifs_asic_id', [])

    # ── 6. Fabric info ───────────────────────────────────────────────
    logger.info("Gathering fabric info")
    fabric_result = localhost.fabric_info(
        num_fabric_asic=getattr(duthost, 'num_fabric_asics', 0) or 0,
        asics_host_basepfx=getattr(duthost, 'asics_host_ip', None),
        asics_host_basepfx6=getattr(duthost, 'asics_host_ipv6', None),
        module_ignore_errors=True
    )
    facts.fabric_info = fabric_result.get('ansible_facts', {})

    # ── 7. VM info ───────────────────────────────────────────────────
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

    # ── 8. Host interfaces ───────────────────────────────────────────
    if 'host_interfaces_by_dut' in facts.topo_config:
        all_host_ifs = facts.topo_config['host_interfaces_by_dut'][facts.dut_index]
        disabled_ifs = facts.topo_config.get('disabled_host_interfaces_by_dut', [[]])[facts.dut_index]
        facts.host_if_indexes = [i for i in all_host_ifs if i not in disabled_ifs]

        # VLAN interfaces for T0 topology
        dut_type = facts.topo_config.get('dut_type', '')
        if 'tor' in dut_type.lower():
            facts.vlan_intfs = [facts.port_alias[i] for i in facts.host_if_indexes]

    # ── 9. Interface-to-VM mappings ──────────────────────────────────
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

    # ── 10. VLAN config for T0 ───────────────────────────────────────
    dut_type = facts.topo_config.get('dut_type', '')
    if 'host_interfaces_by_dut' in facts.topo_config and 'tor' in dut_type.lower():
        logger.info("Gathering VLAN config for T0 topology")
        vlan_result = localhost.vlan_config(
            vm_topo_config=facts.topo_config,
            port_alias=facts.port_alias,
            module_ignore_errors=True
        )
        facts.vlan = vlan_result.get('ansible_facts', {})

    # ── 11. Tunnel config ────────────────────────────────────────────
    logger.info("Gathering tunnel config")
    tunnel_result = localhost.tunnel_config(
        vm_topo_config=facts.topo_config,
        module_ignore_errors=True
    )
    facts.tunnel = tunnel_result.get('ansible_facts', {})

    # ── 12. Dualtor facts ────────────────────────────────────────────
    if 'dualtor' in facts.topo or 'cable' in facts.topo:
        logger.info("Gathering dual ToR facts")
        dt_result = localhost.dual_tor_facts(
            hostname=duthost.hostname,
            testbed_facts=facts.testbed_facts,
            hostvars={},  # TODO: pass actual hostvars if needed
            vm_config=facts.topo_config,
            port_alias=facts.port_alias,
            vlan_intfs=facts.vlan_intfs,
            module_ignore_errors=True
        )
        facts.dual_tor = dt_result.get('ansible_facts', {})

    if 'dualtor' in facts.topo:
        logger.info("Gathering mux cable facts")
        mux_result = localhost.mux_cable_facts(
            topo_name=facts.topo,
            module_ignore_errors=True
        )
        facts.mux_cable = mux_result.get('ansible_facts', {})

    # ── 13. IPv6 management config ───────────────────────────────────
    facts.sonic_mgmt_ipv6 = ipv6_only_mgmt
    if ipv6_only_mgmt:
        logger.info("Configuring IPv6-only management mode")
        # Override service configs with IPv6 variants
        # These would normally come from group_vars/<group>/ipv6.yml
        # For now, the caller must provide them or they stay as defaults

    # ── 14. TACACS defaults ──────────────────────────────────────────
    facts.use_ptf_tacacs = True
    facts.tacacs_enabled_by_default = True

    ptf_ip = facts.testbed_facts.get('ptf_ip', '')
    if facts.testbed_facts.get('multi_servers_tacacs_ip'):
        facts.tacacs_servers = [facts.testbed_facts['multi_servers_tacacs_ip']]
    elif ptf_ip:
        facts.tacacs_servers = [ptf_ip]

    if ipv6_only_mgmt:
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
