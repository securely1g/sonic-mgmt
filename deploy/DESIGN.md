# Plan: Rewrite `deploy-mg` from Ansible YAML to Python (using pytest-ansible)

## Background

`testbed-cli.sh deploy-mg` deploys a minigraph configuration to a SONiC DUT. The current implementation is a bash function that calls a 1193-line Ansible YAML playbook (`config_sonic_basedon_testbed.yml`). This plan rewrites it into Python, reusing the existing `pytest-ansible` patterns already used in sonic-mgmt tests.

## Current Call Chain

```
testbed-cli.sh deploy-mg <testbed> <inventory> <vault-password>
  └─ deploy_minigraph()  [bash function]
       ├─ read_yaml() / read_csv()  → parse testbed file, extract $duts
       └─ ansible-playbook -i $inventory config_sonic_basedon_testbed.yml \
            --vault-password-file=$passfile -l $duts \
            -e testbed_name=$testbed_name -e testbed_file=$tbfile \
            -e vm_file=$vmfile -e deploy=true -e save=true
```

The YAML playbook does:
1. **Facts gathering** — calls 11 custom Ansible modules (`test_facts`, `topo_facts`, `port_alias`, `testbed_vm_info`, `conn_graph_facts`, `fabric_info`, `dual_tor_facts`, `mux_cable_facts`, `vlan_config`, `tunnel_config`, `generate_golden_config_db`)
2. **Minigraph generation** — renders Jinja2 templates (`minigraph_template.j2` → includes `minigraph_cpg.j2`, `minigraph_dpg.j2`, `minigraph_png.j2`, `minigraph_device.j2`, `minigraph_meta.j2`, `minigraph_link_meta.j2`)
3. **Deploy to DUT** — SSH: copy minigraph XML, deploy certs, run `config load_minigraph --override_config -y`
4. **Post-deploy** — TACACS config, NTP/chrony sync, BMP enable, config save, sshd timeout reset, cache cleanup

## How pytest-ansible Works in sonic-mgmt

The existing test framework already has a clean pattern for calling Ansible modules from Python:

```python
# pytest-ansible provides the `ansible_adhoc` fixture
# AnsibleHostBase (tests/common/devices/base.py) wraps it:
#   - Any attribute access matching an Ansible module name becomes a module call
#   - Works for both built-in modules AND custom modules in ansible/library/

duthost = SonicHost(ansible_adhoc, "vlab-01")

# Built-in modules:
duthost.shell("show version")                          # → ansible -m shell
duthost.copy(src="file.xml", dest="/etc/sonic/")       # → ansible -m copy
duthost.template(src="tmpl.j2", dest="/tmp/out")       # → ansible -m template
duthost.service(name="ntp", state="restarted")         # → ansible -m service
duthost.command("ntpd -gq")                            # → ansible -m command
duthost.file(path="/tmp/foo", state="absent")          # → ansible -m file
duthost.lineinfile(name="/etc/foo", regexp="...", line="...")

# Custom modules (from ansible/library/):
localhost.test_facts(testbed_name="vms-kvm-t0", testbed_file="vtestbed.yaml")
localhost.topo_facts(topo="t0", hwsku="Force10-S6000")
duthost.port_alias(hwsku="Force10-S6000")
localhost.testbed_vm_info(base_vm="VM0100", topo="t0", vm_file="veos")

# Results come back as dicts:
result = duthost.shell("show ip bgp summary")
print(result['stdout'])
print(result['rc'])

facts = localhost.test_facts(...)['ansible_facts']['testbed_facts']
```

**Key classes:**
- `AnsibleHostBase` (`tests/common/devices/base.py`) — base wrapper, `__getattr__` dispatches to ansible modules
- `SonicHost` (`tests/common/devices/sonic.py`) — SONiC DUT, inherits AnsibleHostBase
- `Localhost` (`tests/common/devices/local.py`) — for delegate_to localhost tasks

## Existing Testbed Parsers (No New Parser Needed)

There are already **three** testbed parsers in the codebase. We reuse the existing ones:

### 1. `tests/common/testbed.py` → `TestbedInfo` class (RECOMMENDED)
- Used by the pytest `tbinfo` fixture in `conftest.py`
- Most full-featured: parses YAML/CSV, calculates PTF index maps, normalizes topo names, loads topology properties from `vars/topo_*.yml`
- Returns `testbed_topo[testbed_name]` dict with `duts`, `duts_map`, `topo`, `ptf_ip`, `vm_base`, `inv_name`, etc.
- **This is what we should use** — it's already Python and already does everything we need

### 2. `ansible/library/test_facts.py` → `ParseTestbedTopoinfo` class
- Used as an Ansible custom module (`test_facts`) called from YAML playbooks
- Nearly identical parsing logic to `TestbedInfo`
- Returns `testbed_facts` via `ansible_facts` — this is what `config_sonic_basedon_testbed.yml` calls

### 3. `testbed-cli.sh` → `read_yaml()` / `read_csv()` bash functions
- Shell-level parsing, calls Python one-liners for YAML
- **This is what we're replacing**

## Proposed Architecture

```
sonic-mgmt/
├── deploy/                              # NEW — all new Python code lives here
│   ├── __init__.py
│   ├── deploy_minigraph.py              # Main entry point (pytest-callable + CLI)
│   ├── facts_gatherer.py               # Phase 1: Gather facts via pytest-ansible modules
│   ├── minigraph_deployer.py           # Phase 2+3: Generate & deploy using ansible modules
│   └── post_deploy.py                  # Phase 4: Post-deploy config via ansible modules
├── ansible/
│   ├── config_sonic_basedon_testbed.yml # KEEP for backward compat
│   ├── library/                         # KEEP — custom modules, reused directly
│   └── templates/                       # KEEP — Jinja2 templates, referenced from deploy/
└── tests/common/
    ├── testbed.py                       # KEEP — TestbedInfo class, imported by deploy/
    └── devices/                         # KEEP — AnsibleHostBase, SonicHost, Localhost
```

## Phase Breakdown

### Phase 1: Facts Gathering (`deploy/facts_gatherer.py`)

**Replaces:** First ~250 lines of `config_sonic_basedon_testbed.yml` (all the `set_fact`, `test_facts`, `topo_facts`, `port_alias`, etc.)

Uses `TestbedInfo` from `tests/common/testbed.py` for testbed parsing, and calls existing custom Ansible modules via pytest-ansible's ad-hoc runner for everything else:

```python
from tests.common.testbed import TestbedInfo

def gather_facts(localhost, duthost, testbed_name, testbed_file, vm_file="veos"):
    """Gather all facts needed for minigraph generation and deployment."""
    
    # 1. Testbed parsing — reuse existing TestbedInfo class
    tb_info = TestbedInfo(testbed_file)
    testbed_facts = tb_info.testbed_topo[testbed_name]
    topo = testbed_facts['topo']['name']
    
    # 2. Can also call the Ansible module version for compat if needed:
    # tb_result = localhost.test_facts(
    #     testbed_name=testbed_name,
    #     testbed_file=testbed_file
    # )['ansible_facts']['testbed_facts']
    
    # 3. Topology facts (custom module: ansible/library/topo_facts.py)
    hwsku = duthost.facts.get('hwsku', 'Force10-S6000')
    topo_result = localhost.topo_facts(
        topo=topo,
        hwsku=hwsku,
        testbed_name=testbed_name
    )
    vm_topo_config = topo_result['ansible_facts']['vm_topo_config']
    
    # 4. Connection graph (custom module: ansible/library/conn_graph_facts.py)
    conn_facts = localhost.conn_graph_facts(
        host=duthost.hostname,
        module_ignore_errors=True
    )
    
    # 5. Port alias mapping (custom module: ansible/library/port_alias.py)
    port_info = duthost.port_alias(hwsku=hwsku)  # or localhost for local mode
    port_alias = port_info['ansible_facts']['port_alias']
    
    # 6. Fabric info (custom module: ansible/library/fabric_info.py)
    fabric = localhost.fabric_info(
        num_fabric_asic=duthost.facts.get('num_fabric_asics', 0)
    )
    
    # 7. VM info (custom module: ansible/library/testbed_vm_info.py)
    vm_info = localhost.testbed_vm_info(
        base_vm=testbed_facts.get('vm_base', ''),
        topo=topo,
        vm_file=vm_file
    )
    
    # 8. VLAN config for T0 (custom module: ansible/library/vlan_config.py)
    vlan_facts = {}
    if 'tor' in vm_topo_config.get('dut_type', '').lower():
        vlan_result = localhost.vlan_config(
            vm_topo_config=vm_topo_config,
            port_alias=port_alias
        )
        vlan_facts = vlan_result['ansible_facts']
    
    # 9. Tunnel config (custom module: ansible/library/tunnel_config.py)
    tunnel_facts = localhost.tunnel_config(vm_topo_config=vm_topo_config)
    
    # 10. Dualtor facts if applicable
    dual_tor_facts = {}
    if 'dualtor' in topo:
        dt_result = localhost.dual_tor_facts(
            hostname=duthost.hostname,
            testbed_facts=testbed_facts,
            vm_config=vm_topo_config,
            port_alias=port_alias
        )
        dual_tor_facts = dt_result['ansible_facts']
    
    # 11. Golden config DB generation
    golden_config = localhost.generate_golden_config_db(
        topo_name=topo,
        hwsku=hwsku
    )
    
    return DeployFacts(
        testbed=testbed_facts,
        topo_config=vm_topo_config,
        port_alias=port_alias,
        vm_info=vm_info,
        vlan=vlan_facts,
        tunnel=tunnel_facts,
        dual_tor=dual_tor_facts,
        golden_config=golden_config,
        conn_graph=conn_facts,
    )
```

**Effort:** ~200 lines, 2 days

### Phase 2+3: Generate & Deploy Minigraph (`deploy/minigraph_deployer.py`)

**Replaces:** The template rendering + deploy block (~lines 400-1100 of the YAML)

```python
def generate_and_deploy(localhost, duthost, facts, deploy=True, save=True):
    """Generate minigraph XML and optionally deploy to DUT."""
    
    topo = facts.testbed['topo']['name']
    
    # === GENERATE ===
    # Render minigraph using Jinja2 template (same template as YAML playbook)
    # The template module needs all the same variables the YAML had as facts
    template_vars = _build_template_vars(duthost, facts)
    
    if not deploy:
        # Local-only generation
        localhost.template(
            src="templates/minigraph_template.j2",
            dest=f"minigraph/{duthost.hostname}.{topo}.xml",
            **template_vars
        )
        return
    
    # === PRE-DEPLOY ===
    # Save original minigraph
    duthost.shell("mv /etc/sonic/minigraph.xml /etc/sonic/minigraph.xml.orig",
                  module_ignore_errors=True)
    
    # Deploy certificates if needed
    _deploy_certs(duthost, facts)
    
    # Set docker proxy if configured
    _configure_docker_proxy(duthost, facts)
    
    # === DEPLOY ===
    # Render minigraph directly to DUT
    duthost.template(
        src="templates/minigraph_template.j2",
        dest="/etc/sonic/minigraph.xml",
        **template_vars
    )
    
    # Update SNMP community
    duthost.lineinfile(
        name="/etc/sonic/snmp.yml",
        regexp="^snmp_rocommunity:",
        line=f"snmp_rocommunity: {facts.snmp_rocommunity}"
    )
    
    # Generate golden_config_db.json
    duthost.generate_golden_config_db(
        topo_name=topo,
        hwsku=duthost.facts['hwsku']
    )
    
    # Create DNS config
    duthost.template(
        src="templates/dns_config.j2",
        dest="/tmp/dns_config.json"
    )
    
    # Cleanup before loading
    duthost.file(path="/etc/sonic/acl.json", state="absent")
    duthost.file(path="/etc/sonic/port_config.json", state="absent")
    
    # Load minigraph
    result = duthost.shell(
        "config load_minigraph --override_config -y",
        module_ignore_errors=True
    )
    if result.get('rc') != 0 and '--override_config' in result.get('stderr', ''):
        duthost.shell("config load_minigraph -y")
    
    # Wait for switch to come back
    localhost.wait_for(
        host=duthost.mgmt_ip,
        port=22,
        state="started",
        search_regex=r"OpenSSH_[\w\.]+ Debian",
        delay=10,
        timeout=600
    )


def _deploy_certs(duthost, facts):
    """Deploy telemetry and restapi certificates."""
    # Telemetry certs
    if hasattr(facts, 'telemetry_certs') and facts.telemetry_certs:
        # ... cert deployment logic
        pass
    
    # RestAPI certs
    if hasattr(facts, 'restapi_certs') and facts.restapi_certs:
        # ... cert deployment logic
        pass


def _configure_docker_proxy(duthost, facts):
    """Set up docker HTTP proxy if configured."""
    if not hasattr(facts, 'proxy_env') or not facts.proxy_env:
        return
    
    duthost.file(
        path="/etc/systemd/system/docker.service.d",
        state="directory",
        recurse=True
    )
    duthost.template(
        src="templates/docker_http_proxy.j2",
        dest="/etc/systemd/system/docker.service.d/http-proxy.conf",
        force=True
    )
    duthost.shell("systemctl daemon-reload")
    duthost.service(name="docker", state="restarted")
    # Wait for containers to restart
    import time
    time.sleep(60)
```

**Effort:** ~300 lines, 3 days

### Phase 4: Post-Deploy (`deploy/post_deploy.py`)

**Replaces:** Everything after `config load_minigraph` in the YAML

```python
def post_deploy(duthost, localhost, facts, save=True):
    """Post-deployment configuration: NTP, TACACS, BGP, save."""
    
    topo = facts.testbed['topo']['name']
    
    # === NTP / CHRONY SYNC ===
    _sync_time(duthost)
    
    # === BMP CONFIG ===
    bmp_check = duthost.shell(
        "config -h | grep 'BMP-related configuration'",
        module_ignore_errors=True
    )
    if bmp_check.get('rc') == 0:
        duthost.command("sudo config bmp enable bgp-neighbor-table")
        duthost.command("sudo config bmp enable bgp-rib-in-table")
        duthost.command("sudo config bmp enable bgp-rib-out-table")
    
    # === BGP STARTUP ===
    duthost.shell("config bgp startup all")
    
    # === TACACS ===
    _configure_tacacs(duthost, facts)
    
    # === SAVE CONFIG ===
    if save:
        duthost.shell("config save -y")
    
    # === CLEANUP ===
    # Remove running golden config
    duthost.file(path="/etc/sonic/running_golden_config.json", state="absent")
    
    # Cleanup cached facts
    localhost.shell(
        "python ../tests/common/cache/facts_cache.py",
        module_ignore_errors=True
    )
    
    # Reset sshd timeout
    duthost.shell(
        'sed -i "s/^ClientAliveInterval [0-9].*/ClientAliveInterval 900/g" /etc/ssh/sshd_config'
    )
    duthost.service(name="sshd", state="restarted")
    
    # Enable IPv6
    duthost.sysctl(
        name="net.ipv6.conf.all.disable_ipv6",
        value="0",
        sysctl_set=True
    )


def _sync_time(duthost):
    """Sync DUT time with NTP or chrony."""
    chrony_check = duthost.service(name="chrony", module_ignore_errors=True)
    
    if chrony_check.get('status', {}).get('LoadState') == 'not-found':
        # Use NTP
        duthost.service(name="ntp", state="started", module_ignore_errors=True)
        duthost.service(name="ntp", state="stopped")
        duthost.command("ntpd -gq", module_ignore_errors=True)
        duthost.command("hwclock --systohc", module_ignore_errors=True)
        duthost.service(name="ntp", state="restarted")
    else:
        # Use chrony
        duthost.service(name="chrony", state="stopped")
        duthost.command("chronyd -F 1 -q", module_ignore_errors=True)
        duthost.service(name="chrony", state="restarted")


def _configure_tacacs(duthost, facts):
    """Configure TACACS authentication."""
    tacacs_cmds = [
        f"config tacacs passkey {facts.tacacs_passkey}",
        "config tacacs authtype pap",
        "config aaa authentication login tacacs+",
    ]
    for cmd in tacacs_cmds:
        duthost.shell(cmd, module_ignore_errors=True)
    
    if facts.use_ptf_tacacs:
        duthost.shell("config tacacs authtype login", module_ignore_errors=True)
        duthost.shell('config aaa authorization "tacacs+ local"', module_ignore_errors=True)
        duthost.shell('config aaa accounting "tacacs+ local"', module_ignore_errors=True)
```

**Effort:** ~150 lines, 1-2 days

### Main Entry Point (`deploy/deploy_minigraph.py`)

```python
"""
Deploy minigraph to SONiC DUT.

Usage (as pytest, from sonic-mgmt root):
    cd /data/sonic-mgmt
    pytest deploy/deploy_minigraph.py --inventory ansible/veos_vtb \
        --testbed vms-kvm-t0 \
        --testbed_file ansible/vtestbed.yaml

Usage (from testbed-cli.sh — drop-in replacement):
    python deploy/deploy_minigraph.py --testbed-name vms-kvm-t0 \
        --inventory ansible/veos_vtb \
        --testbed-file ansible/vtestbed.yaml
"""

import pytest
from tests.common.testbed import TestbedInfo
from tests.common.devices.sonic import SonicHost
from tests.common.devices.local import Localhost
from deploy.facts_gatherer import gather_facts
from deploy.minigraph_deployer import generate_and_deploy
from deploy.post_deploy import post_deploy


def deploy_mg(ansible_adhoc, testbed_name, testbed_file, vm_file="veos",
              ipv6_only_mgmt=False, deploy=True, save=True):
    """Main deploy-mg logic."""
    
    # Parse testbed using existing TestbedInfo class
    tb_info = TestbedInfo(testbed_file)
    tb_config = tb_info.testbed_topo[testbed_name]
    
    # Create ansible host objects (same pattern as tests/conftest.py)
    localhost = Localhost(ansible_adhoc)
    
    duts = tb_config['duts']
    for dut_name in duts:
        duthost = SonicHost(ansible_adhoc, dut_name)
        
        # Phase 1: Gather facts
        facts = gather_facts(localhost, duthost, testbed_name, testbed_file, vm_file)
        
        # Phase 2+3: Generate and deploy
        generate_and_deploy(localhost, duthost, facts, deploy=deploy, save=save)
        
        # Phase 4: Post-deploy
        if deploy:
            post_deploy(duthost, localhost, facts, save=save)
    
    print("Done")


# === pytest entry point ===
@pytest.fixture
def deploy_minigraph(ansible_adhoc, tbinfo, request):
    """Pytest fixture for deploy-mg. Can be used directly from test framework."""
    testbed_name = request.config.getoption("--testbed")
    testbed_file = request.config.getoption("--testbed_file")
    deploy_mg(
        ansible_adhoc=ansible_adhoc,
        testbed_name=testbed_name,
        testbed_file=testbed_file,
    )


def test_deploy_minigraph(deploy_minigraph):
    """Dummy test to allow running via: pytest deploy_minigraph.py ..."""
    pass
```

## Conditional Logic (Edge Cases)

The YAML playbook has extensive conditional blocks for special topologies. These should be handled as pluggable handlers:

| Condition | YAML Lines | Python Handler |
|---|---|---|
| Dualtor (`'dualtor' in topo`) | ~50 lines | `_handle_dualtor()` |
| VoQ Chassis (`switch_type == 'voq'`) | ~80 lines | `_handle_voq_chassis()` |
| Smartswitch (`t1-28-lag`, `smartswitch-t1`) | ~30 lines | `_handle_smartswitch()` |
| IPv6-only management | ~40 lines | `_handle_ipv6_mgmt()` |
| T2 topology | ~20 lines | `_handle_t2()` |
| WAN topologies | ~15 lines | `_handle_wan()` |
| Ixia/Keysight testbed | ~20 lines | `_handle_ixia()` |
| Macsec profile | ~15 lines | `_handle_macsec()` |

**Recommendation:** Start with the common path (T0/T1, non-dualtor, non-chassis) and add handlers incrementally.

## Migration Strategy

### Step 1: Core implementation (Week 1)
- Implement Phases 1-4 using existing `TestbedInfo` + pytest-ansible modules
- Validate facts match what the YAML playbook produces

### Step 2: End-to-end testing (Week 2)
- Test on a T0 topology end-to-end
- Add `testbed-cli.sh deploy-mg-py` as new subcommand
- Run both old and new in parallel, compare results

### Step 3: Edge cases & cutover (Week 2-3)
- Add edge case handlers as needed
- Replace `testbed-cli.sh deploy-mg` to call Python version
- Keep YAML playbook for backward compatibility

## Estimated Effort

| Phase | Lines of Python | Effort |
|---|---|---|
| 1. Facts via pytest-ansible (reuse `TestbedInfo`) | ~200 | 2 days |
| 2+3. Generate + Deploy | ~300 | 3 days |
| 4. Post-deploy | ~150 | 1-2 days |
| Edge case handlers | ~200 | 2-3 days |
| Testing & integration | — | 2-3 days |
| **Total** | **~850** | **~2 weeks** |

## Benefits

- **No new testbed parser** — reuses existing `TestbedInfo` from `tests/common/testbed.py`
- **Consistent** — uses same pytest-ansible patterns (`duthost.shell()`, `duthost.copy()`, etc.) as the test suite
- **Debuggable** — Python with proper stack traces vs opaque YAML
- **Testable** — each phase can be unit tested independently
- **Reusable** — facts gathering can be shared with test fixtures
- **Maintainable** — conditionals as Python if/else vs nested YAML `when:` blocks
