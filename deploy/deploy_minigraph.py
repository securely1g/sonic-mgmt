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


def deploy_mg(ansible_adhoc, testbed_name, testbed_file, vm_file="veos",
              ipv6_only_mgmt=False, deploy=True, save=True):
    """
    Main deploy-mg logic.

    Replicates the full testbed-cli.sh deploy-mg flow:
    1. Parse testbed file to get DUT list
    2. For each DUT:
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

    for dut_name in duts:
        logger.info("-" * 40)
        logger.info("Processing DUT: %s", dut_name)
        logger.info("-" * 40)

        duthost = SonicHost(ansible_adhoc, dut_name)

        # Phase 1: Gather facts
        facts = gather_facts(
            localhost, duthost, testbed_name, testbed_file,
            vm_file=vm_file, ipv6_only_mgmt=ipv6_only_mgmt
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


# ═══════════════════════════════════════════════════════════════
# pytest entry points
# ═══════════════════════════════════════════════════════════════

def pytest_addoption(parser):
    """Add custom CLI options for deploy-mg."""
    # These may already be defined by conftest.py, so use try/except
    try:
        parser.addoption("--vm-file", default="veos",
                         help="Virtual machine inventory file")
    except ValueError:
        pass
    try:
        parser.addoption("--ipv6-only-mgmt", action="store_true",
                         default=False, help="Use IPv6-only management")
    except ValueError:
        pass
    try:
        parser.addoption("--deploy", default="true",
                         help="Deploy to DUT (true/false)")
    except ValueError:
        pass
    try:
        parser.addoption("--save", default="true",
                         help="Save as startup-config (true/false)")
    except ValueError:
        pass


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
    # The fixture does all the work; this test just triggers it.
    pass
