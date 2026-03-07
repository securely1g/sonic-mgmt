"""
Deploy minigraph to SONiC DUT.

Main entry point for the Python rewrite of testbed-cli.sh deploy-mg.
Can be run as a pytest test or invoked from testbed-cli.sh.

Usage (as pytest, from sonic-mgmt root):
    cd /data/sonic-mgmt
    pytest deploy/deploy_minigraph.py \
        --inventory ansible/veos_vtb \
        --testbed vms-kvm-t0 \
        --testbed_file ansible/vtestbed.yaml

Usage (from testbed-cli.sh — drop-in replacement):
    The testbed-cli.sh deploy-mg command can call this script instead of
    ansible-playbook config_sonic_basedon_testbed.yml.
"""

import logging
import os
import sys

import pytest

# Add sonic-mgmt root to path so we can import tests.common
SONIC_MGMT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SONIC_MGMT_ROOT not in sys.path:
    sys.path.insert(0, SONIC_MGMT_ROOT)

from tests.common.testbed import TestbedInfo  # noqa: E402
from tests.common.devices.sonic import SonicHost  # noqa: E402
from tests.common.devices.local import Localhost  # noqa: E402
from deploy.facts_gatherer import gather_facts  # noqa: E402
from deploy.minigraph_deployer import generate_and_deploy  # noqa: E402
from deploy.post_deploy import post_deploy  # noqa: E402

logger = logging.getLogger(__name__)

ANSIBLE_DIR = os.path.join(SONIC_MGMT_ROOT, "ansible")


def deploy_mg(ansible_adhoc, testbed_name, testbed_file, vm_file="veos",
              ipv6_only_mgmt=False, deploy=True, save=True):
    """
    Main deploy-mg logic.

    Replicates the full testbed-cli.sh deploy-mg flow:
    1. Parse testbed file to get DUT list
    2. Pre-connect: check IPv4 reachability, fall back to IPv6
    3. For each DUT:
       a. Gather facts (topology, ports, VMs, VLANs, etc.)
       b. Generate minigraph XML from Jinja2 template
       c. Deploy minigraph to DUT and load it
       d. Run post-deploy config (TACACS, NTP, BGP, save)

    Args:
        ansible_adhoc: pytest-ansible ad-hoc fixture
        testbed_name: Name of the testbed (e.g. "vms-kvm-t0")
        testbed_file: Path to testbed YAML/CSV file
        vm_file: Virtual machine inventory file (default: "veos")
        ipv6_only_mgmt: Use IPv6-only management network
        deploy: Whether to deploy (False = generate only)
        save: Whether to save as startup-config
    """
    logger.info("=" * 60)
    logger.info("deploy-mg: testbed=%s, file=%s", testbed_name, testbed_file)
    logger.info("  deploy=%s, save=%s, ipv6_only=%s", deploy, save, ipv6_only_mgmt)
    logger.info("=" * 60)

    # Parse testbed using existing TestbedInfo class
    tb_info = TestbedInfo(testbed_file)
    tb_config = tb_info.testbed_topo.get(testbed_name)
    if tb_config is None:
        raise ValueError(
            f"Testbed '{testbed_name}' not found in {testbed_file}. "
            f"Available: {list(tb_info.testbed_topo.keys())}"
        )

    # Create ansible host objects (same pattern as tests/conftest.py)
    localhost = Localhost(ansible_adhoc)

    duts = tb_config['duts']
    logger.info("DUTs to deploy: %s", duts)

    # Create all DUT host objects (needed for cross-DUT fact aggregation)
    all_duthosts = []
    for dut_name in duts:
        dh = SonicHost(ansible_adhoc, dut_name)
        all_duthosts.append(dh)

    # ── Pre-connect: IPv4 → IPv6 auto-fallback ───────────────────────
    # YAML lines 37-60: try IPv4, if unreachable switch to IPv6
    for duthost in all_duthosts:
        _preconnect_check(duthost, localhost)

    # ── L1 switch configuration (Ixia testbeds) ─────────────────────
    # YAML lines 169-183
    ptf_image = tb_config.get('ptf_image_name', '')
    is_ixia = (ptf_image == 'docker-keysight-api-server')
    # L1 config is an include_tasks that runs on l1_switch group hosts;
    # typically not needed for KVM testbeds. Skip unless configure_l1 is set.

    for duthost in all_duthosts:
        logger.info("-" * 40)
        logger.info("Processing DUT: %s", duthost.hostname)
        logger.info("-" * 40)

        # Phase 1: Gather facts
        facts = gather_facts(
            localhost, duthost, testbed_name, testbed_file,
            vm_file=vm_file, ipv6_only_mgmt=ipv6_only_mgmt,
            deploy=deploy, all_duthosts=all_duthosts
        )

        # ── Simulated Y-cable for dualtor ────────────────────────────
        # YAML lines 318-320: include_tasks config_simulated_y_cable.yml
        if 'dualtor' in facts.topo and deploy:
            _configure_simulated_y_cable(
                duthost, localhost, facts, vm_file
            )

        # Phase 2+3: Generate and deploy minigraph
        generate_and_deploy(
            localhost, duthost, facts,
            deploy=deploy, save=save
        )

        # Phase 4: Post-deploy configuration
        if deploy:
            post_deploy(duthost, localhost, facts, save=save)

    logger.info("=" * 60)
    logger.info("deploy-mg complete for testbed %s", testbed_name)
    logger.info("=" * 60)


def _preconnect_check(duthost, localhost):
    """
    Pre-connection IPv4 reachability check with IPv6 fallback.

    YAML lines 37-60: saves original_ipv4_address, tries port 22,
    switches to ansible_hostv6 if IPv4 unreachable.
    """
    ansible_host = getattr(duthost, 'ansible_host', '') or ''

    # Only check if current host is IPv4 (no ':' in address)
    if ':' in ansible_host:
        return  # Already IPv6

    # Save original IPv4
    original_ipv4 = ansible_host

    try:
        result = localhost.wait_for(
            host=ansible_host,
            port=22,
            timeout=10,
            module_ignore_errors=True
        )
        if result.get('failed', False):
            raise Exception("IPv4 unreachable")
    except Exception:
        # Try IPv6 fallback
        ansible_hostv6 = getattr(duthost, 'ansible_hostv6', None)
        if ansible_hostv6:
            logger.info(
                "IPv4 (%s) unreachable for %s, switching to IPv6 (%s)",
                ansible_host, duthost.hostname, ansible_hostv6
            )
            # Update ansible_host to IPv6
            try:
                duthost.host.options['inventory_manager'].set_variable(
                    duthost.hostname, 'ansible_host', ansible_hostv6
                )
            except Exception:
                logger.warning("Could not update ansible_host via inventory_manager")
        else:
            logger.warning(
                "IPv4 (%s) unreachable for %s and no IPv6 available",
                ansible_host, duthost.hostname
            )


def _configure_simulated_y_cable(duthost, localhost, facts, vm_file):
    """
    Configure simulated Y-cable for dualtor topologies.

    YAML lines 318-320: include_tasks dualtor/config_simulated_y_cable.yml
    This sets up the mux simulator configuration on the DUT.
    """
    testbed_facts = facts.testbed_facts

    # Validations from config_simulated_y_cable.yml
    if len(testbed_facts.get('duts', [])) != 2:
        logger.warning("Dualtor requires exactly 2 DUTs, skipping Y-cable config")
        return

    if duthost.hostname not in testbed_facts['duts']:
        return

    logger.info("Configuring simulated Y-cable for %s", duthost.hostname)

    # Get vmhost server address
    script_path = os.path.join(ANSIBLE_DIR, "scripts", "vmhost_server_address.py")
    server_name = testbed_facts.get('server', '')
    if os.path.exists(script_path) and server_name:
        result = localhost.shell(
            f"python {script_path} --inv-file {vm_file} --server-name {server_name}",
            module_ignore_errors=True
        )
        mux_simulator_server = result.get('stdout', '').strip()

        # Set mux simulator port
        mux_simulator_port = getattr(duthost, 'mux_simulator_port', None)
        if not mux_simulator_port:
            # Look up from mux_simulator_http_port mapping
            mux_port_map = getattr(duthost, 'mux_simulator_http_port', {})
            if isinstance(mux_port_map, dict):
                mux_simulator_port = mux_port_map.get(facts.testbed_facts.get('conf-name', ''))

        dut_index = facts.dut_index
        dut_side = "upper_tor" if dut_index == 0 else "lower_tor"

        # Create mux_simulator.json on DUT
        mux_config = {
            "mux_simulator": {
                "server": mux_simulator_server,
                "port": mux_simulator_port,
                "vm_set": testbed_facts.get('group-name', ''),
                "side": dut_side
            }
        }
        import json
        duthost.copy(
            content=json.dumps(mux_config, indent=4),
            dest="/etc/sonic/mux_simulator.json",
            module_ignore_errors=True
        )


# ═══════════════════════════════════════════════════════════════
# pytest entry points
# ═══════════════════════════════════════════════════════════════

def pytest_addoption(parser):
    """Add custom CLI options for deploy-mg."""
    options = [
        ("--vm-file", {"default": "veos", "help": "Virtual machine inventory file"}),
        ("--ipv6-only-mgmt", {"action": "store_true", "default": False,
                              "help": "Use IPv6-only management"}),
        ("--deploy", {"default": "true", "help": "Deploy to DUT (true/false)"}),
        ("--save", {"default": "true", "help": "Save as startup-config (true/false)"}),
    ]
    for name, kwargs in options:
        try:
            parser.addoption(name, **kwargs)
        except ValueError:
            pass  # Already defined by conftest.py


@pytest.fixture(scope="session")
def deploy_minigraph(ansible_adhoc, request):
    """
    Pytest fixture for deploy-mg.

    Can be used directly from the test framework. Uses the same
    --testbed, --testbed_file, and --inventory options as sonic-mgmt tests.
    """
    testbed_name = request.config.getoption("--testbed")
    testbed_file = request.config.getoption("--testbed_file")
    vm_file = request.config.getoption("--vm-file", default="veos")
    ipv6_only = request.config.getoption("--ipv6-only-mgmt", default=False)
    do_deploy = request.config.getoption("--deploy", default="true").lower() == "true"
    do_save = request.config.getoption("--save", default="true").lower() == "true"

    if not testbed_name or not testbed_file:
        pytest.skip("--testbed and --testbed_file required for deploy-mg")

    deploy_mg(
        ansible_adhoc=ansible_adhoc,
        testbed_name=testbed_name,
        testbed_file=testbed_file,
        vm_file=vm_file,
        ipv6_only_mgmt=ipv6_only,
        deploy=do_deploy,
        save=do_save,
    )


def test_deploy_minigraph(deploy_minigraph):
    """
    Test entry point for deploy-mg.

    Run via:
        pytest deploy/deploy_minigraph.py \
            --testbed vms-kvm-t0 \
            --testbed_file ansible/vtestbed.yaml \
            --inventory ansible/veos_vtb
    """
    pass
