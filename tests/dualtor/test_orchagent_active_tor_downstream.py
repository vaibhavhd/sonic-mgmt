import contextlib
import logging
import pytest
import random
import ipaddress
import scapy.all as scapyall

from ptf import testutils
from tests.common.dualtor.dual_tor_mock import *
from tests.common.dualtor.dual_tor_utils import dualtor_info
from tests.common.dualtor.dual_tor_utils import flush_neighbor
from tests.common.dualtor.dual_tor_utils import get_t1_ptf_ports
from tests.common.dualtor.dual_tor_utils import crm_neighbor_checker
from tests.common.dualtor.dual_tor_utils import build_packet_to_server
from tests.common.dualtor.dual_tor_utils import mux_cable_server_ip
from tests.common.dualtor.dual_tor_utils import get_ptf_server_intf_index

from tests.common.dualtor.mux_simulator_control import toggle_all_simulator_ports
from tests.common.dualtor.server_traffic_utils import ServerTrafficMonitor
from tests.common.dualtor.tunnel_traffic_utils import tunnel_traffic_monitor
from tests.common.fixtures.ptfhost_utils import run_icmp_responder
from tests.common.fixtures.ptfhost_utils import run_garp_service
from tests.common.fixtures.ptfhost_utils import change_mac_addresses


pytestmark = [
    pytest.mark.topology('t0'),
    pytest.mark.usefixtures('apply_mock_dual_tor_tables',
                            'apply_mock_dual_tor_kernel_configs',
                            'apply_active_state_to_orchagent',
                            'run_garp_service',
                            'run_icmp_responder')
]


def test_active_tor_remove_neighbor_downstream_active(
    conn_graph_facts, ptfadapter, ptfhost,
    rand_selected_dut, rand_unselected_dut, tbinfo,
    set_crm_polling_interval,
    tunnel_traffic_monitor, vmhost
):
    """
    @Verify those two scenarios:
    If the neighbor entry of a server is present on active ToR,
    all traffic to server should be directly forwarded.
    If the neighbor entry of a server is removed, all traffic to server
    should be dropped and no tunnel traffic.
    """
    @contextlib.contextmanager
    def stop_garp(ptfhost):
        """Temporarily stop garp service."""
        ptfhost.shell("supervisorctl stop garp_service")
        yield
        ptfhost.shell("supervisorctl start garp_service")

    tor = rand_selected_dut
    test_params = dualtor_info(ptfhost, rand_selected_dut, rand_unselected_dut, tbinfo)
    server_ipv4 = test_params["target_server_ip"]

    pkt, exp_pkt = build_packet_to_server(tor, ptfadapter, server_ipv4)
    ptf_t1_intf = random.choice(get_t1_ptf_ports(tor, tbinfo))
    logging.info("send traffic to server %s from ptf t1 interface %s", server_ipv4, ptf_t1_intf)
    server_traffic_monitor = ServerTrafficMonitor(
        tor, ptfhost, vmhost, tbinfo, test_params["selected_port"],
        conn_graph_facts, exp_pkt, existing=True, is_mocked=is_mocked_dualtor(tbinfo)
    )
    tunnel_monitor = tunnel_traffic_monitor(tor, existing=False)
    with crm_neighbor_checker(tor), tunnel_monitor, server_traffic_monitor:
        testutils.send(ptfadapter, int(ptf_t1_intf.strip("eth")), pkt, count=10)

    logging.info("send traffic to server %s after removing neighbor entry", server_ipv4)
    server_traffic_monitor = ServerTrafficMonitor(
        tor, ptfhost, vmhost, tbinfo, test_params["selected_port"],
        conn_graph_facts, exp_pkt, existing=False, is_mocked=is_mocked_dualtor(tbinfo)
    )    # for real dualtor testbed, leave the neighbor restoration to garp service
    flush_neighbor_ct = flush_neighbor(tor, server_ipv4, restore=is_t0_mocked_dualtor)
    with crm_neighbor_checker(tor), stop_garp(ptfhost), flush_neighbor_ct, tunnel_monitor, server_traffic_monitor:
        testutils.send(ptfadapter, int(ptf_t1_intf.strip("eth")), pkt, count=10)

    logging.info("send traffic to server %s after neighbor entry is restored", server_ipv4)
    server_traffic_monitor = ServerTrafficMonitor(
        tor, ptfhost, vmhost, tbinfo, test_params["selected_port"],
        conn_graph_facts, exp_pkt, existing=True, is_mocked=is_mocked_dualtor(tbinfo)
    )
    with crm_neighbor_checker(tor), tunnel_monitor, server_traffic_monitor:
        testutils.send(ptfadapter, int(ptf_t1_intf.strip("eth")), pkt, count=10)


def test_downstream_ecmp_nexthops(
    ptfadapter,
    rand_selected_dut, tbinfo,
    toggle_all_simulator_ports,
    tor_mux_intfs
    ):
    dst_server_ipv4 = "1.1.1.2"
    nexthops_count = 4
    def check_nexthops_balance(downlink_ints):
        send_packet, exp_pkt = build_packet_to_server(rand_selected_dut, ptfadapter, dst_server_ipv4)
        exp_pkt.set_do_not_care_scapy(scapyall.IP, "src")
        exp_pkt.set_do_not_care_scapy(scapyall.IP, "dst")
        # expect this packet to be sent to downlinks (active mux) and uplink (stanby mux)
        expected_downlink_ports =  [get_ptf_server_intf_index(rand_selected_dut, tbinfo, iface) for iface in downlink_ints]
        logging.info("Expecting packets in downlink ports {}".format(expected_downlink_ports))
        expected_packets = [exp_pkt] * len(expected_downlink_ports)

        ptf_t1_intf = random.choice(get_t1_ptf_ports(rand_selected_dut, tbinfo))
        testutils.send(ptfadapter, int(ptf_t1_intf.strip("eth")), send_packet, count=10)
        # expect ECMP hashing to work and distribute downlink traffic evenly to every nexthop
        testutils.verify_each_packet_on_each_port(ptfadapter, expected_packets, ports=expected_downlink_ports)

        if len(downlink_ints) < nexthops_count:
            # Some nexthop is now connected to standby mux, and the packets will be sent towards portchanel ints
            expected_uplink_ports = get_t1_ptf_ports(rand_selected_dut, tbinfo)
            testutils.send(ptfadapter, int(ptf_t1_intf.strip("eth")), send_packet, count=10)
            # Verify that the packets are also sent to uplinks (in case of standby MUX)
            testutils.verify_any_packet_any_port(ptfadapter, expected_packets, ports=expected_uplink_ports)


    set_mux_state(rand_selected_dut, tbinfo, 'active', tor_mux_intfs, toggle_all_simulator_ports)
    standby_tor = rand_selected_dut

    iface_server_map = get_random_interfaces(standby_tor, nexthops_count)
    interface_to_server = dict()
    for interface, servers in iface_server_map.items():
        interface_to_server[interface] = servers['server_ipv4'].split("/")[0]

    nexthop_servers = list(interface_to_server.values())
    nexthop_interfaces = list(interface_to_server.keys())

    logging.info("Verify traffic to this route destination is distributed equally")

    logging.info("Add route with four nexthops, where four muxes are active")
    add_nexthop_routes(standby_tor, dst_server_ipv4, nexthops=nexthop_servers[0:nexthops_count])

    logging.info("Verify traffic to this route destination is distributed to four server ports")
    check_nexthops_balance(nexthop_interfaces)

    ### Sequentially set four mux states to standby

    logging.info("Simulate nexthop1 mux state change to Standby")
    interface, server = interface_to_server.items()[0]
    set_mux_state(rand_selected_dut, tbinfo, 'standby', [interface], toggle_all_simulator_ports)
    logging.info("Verify traffic to this route destination is distributed to three server ports and one tunnel nexthop")
    check_nexthops_balance(nexthop_interfaces[1:4])

    logging.info("Simulate nexthop2 mux state change to Standby")
    interface, server = interface_to_server.items()[1]
    set_mux_state(rand_selected_dut, tbinfo, 'standby', [interface], toggle_all_simulator_ports)
    logging.info("Verify traffic to this route destination is distributed to two server ports and two tunnel nexthop")
    check_nexthops_balance(nexthop_interfaces[2:4])

    logging.info("Simulate nexthop3 mux state change to Standby")
    interface, server = interface_to_server.items()[2]
    set_mux_state(rand_selected_dut, tbinfo, 'standby', [interface], toggle_all_simulator_ports)
    logging.info("Verify traffic to this route destination is distributed to one server port and three tunnel nexthop")
    check_nexthops_balance(nexthop_interfaces[3:4])

    logging.info("Simulate nexthop4 mux state change to Standby")
    interface, server = interface_to_server.items()[3]
    set_mux_state(rand_selected_dut, tbinfo, 'standby', [interface], toggle_all_simulator_ports)
    logging.info("Verify traffic to this route destination is distributed to four tunnel nexthops")
    check_nexthops_balance(nexthop_interfaces[3:4])

    ### Revert two mux states to active

    logging.info("Simulate nexthop4 mux state change to Active")
    interface, server = interface_to_server.items()[3]
    set_mux_state(rand_selected_dut, tbinfo, 'active', [interface], toggle_all_simulator_ports)
    logging.info("Verify traffic to this route destination is distributed to one server port and three tunnel nexthop")
    check_nexthops_balance(nexthop_interfaces[3:4])

    logging.info("Simulate nexthop3 mux state change to Active")
    interface, server = interface_to_server.items()[2]
    logging.info("Verify traffic to this route destination is distributed to two server ports and two tunnel nexthop")
    check_nexthops_balance(nexthop_interfaces[2:4])
