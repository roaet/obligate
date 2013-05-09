#Copyright (c) 2012 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import netaddr
import time

from sqlalchemy.orm import sessionmaker

from models import melange
from quark.db import models as quarkmodels


interface_tenant = dict()
interfaces = dict()
interface_network = dict()
interface_ip = dict()
port_cache = dict()


def loadSession():
    #metadata = Base.metadata
    Session = sessionmaker(bind=melange.engine)
    session = Session()
    return session


def flush_db():
    quarkmodels.BASEV2.metadata.drop_all(melange.engine)
    quarkmodels.BASEV2.metadata.create_all(melange.engine)


def do_and_time_quietly(label, fx, **kwargs):
    do_and_time(label, fx, True, **kwargs)


def do_and_time(label, fx, quiet=False, **kwargs):
    start_time = time.time()
    if not quiet:
        print "start:" + label
    try:
        fx(**kwargs)
    except Exception as e:
        print "Error during " + label
        raise e
    end_time = time.time()
    if not quiet:
        print "end  :" + label
    if not quiet:
        print "delta:" + label + " = " +\
            str(end_time - start_time) + " seconds"
    return end_time - start_time


def migrate_networks(session=None):
    """1. Migrate the m.ip_blocks -> q.quark_networks

    Migration of ip_blocks to networks requires one take into
    consideration that blocks can have 'children' blocks. A scan of
    the melange tables shows though that this feature hasn't been
    used.

    An ip_block has a cidr which maps to a corresponding subnet
    in quark.
    """
    blocks = session.query(melange.IpBlocks).all()
    networks = dict()
    """Create the networks using the network_id. It is assumed that
    a network can only belong to one tenant"""
    for block in blocks:
        if block.network_id not in networks:
            networks[block.network_id] = {
                "tenant_id": block.tenant_id,
                "name": block.network_name,
            }
        elif networks[block.network_id]["tenant_id"] != block.tenant_id:
            print "Found different tenant on network. wtf"
            raise Exception

    for net in networks:
        q_network = quarkmodels.Network(id=net,
                                        tenant_id=networks[net]["tenant_id"],
                                        name=networks[net]["name"])
        session.add(q_network)

    for block in blocks:
        q_subnet = quarkmodels.Subnet(id=block.id,
                                      network_id=block.network_id,
                                      cidr=block.cidr)
        session.add(q_subnet)
        migrate_ips(session=session, block=block)
        migrate_routes(session=session, block=block)


def migrate_routes(session=None, block=None):
    routes = session.query(melange.IpRoutes)\
                    .filter_by(source_block_id=block.id).all()
    for route in routes:
        q_route = quarkmodels.Route(id=route.id,
                                    cidr=route.netmask,
                                    tenant_id=block.tenant_id,
                                    gateway=route.gateway,
                                    created_at=block.created_at,
                                    subnet_id=block.id)
        session.add(q_route)


def migrate_ips(session=None, block=None):
    """3. Migrate m.ip_addresses -> q.quark_ip_addresses
    This migration is complicated. I believe q.subnets will need to be
    populated during this step as well. m.ip_addresses is scattered all
    over the place and it is not a 1:1 relationship between m -> q.
    Some more thought will be needed for this one.

    First we need to use m.ip_addresses to find the relationship between
    the ip_block and the m.interfaces. After figuring out that it will
    then be possible to create a q.subnet connected to the network.

    """
    addresses = session.query(melange.IpAddresses)\
                       .filter_by(ip_block_id=block.id).all()
    for address in addresses:
        """Populate interface_network cache"""
        interface = address.interface_id
        if interface is not None and\
                interface not in interface_network:
            interface_network[interface] = block.network_id
        if interface in interface_network and\
                interface_network[interface] != block.network_id:
            print "Found interface with different network id: " +\
                block.network_id

        deallocated = False
        deallocated_at = None
        """If marked for deallocation put it into the quark ip table
        as deallocated
        """
        if address.marked_for_deallocation == 1:
            deallocated = True
            deallocated_at = address.deallocated_at

        preip = netaddr.IPAddress(address.address)
        version = preip.version
        ip = netaddr.IPAddress(address.address).ipv6()
        q_ip = quarkmodels.IPAddress(id=address.id,
                                     created_at=address.created_at,
                                     tenant_id=block.tenant_id,
                                     network_id=block.network_id,
                                     subnet_id=block.id,
                                     version=version,
                                     address_readable=address.address,
                                     deallocated_at=deallocated_at,
                                     _deallocated=deallocated,
                                     address=int(ip))
        """Populate interface_ip cache"""
        if interface not in interface_ip:
            interface_ip[interface] = set()
        interface_ip[interface].add(q_ip)

        session.add(q_ip)


def migrate_interfaces(session=None):
    interfaces = session.query(melange.Interfaces).all()
    no_network_count = 0
    for interface in interfaces:
        if interface.id not in interface_network:
            no_network_count += 1
            continue
        network_id = interface_network[interface.id]
        interface_tenant[interface.id] = interface.tenant_id
        q_port = quarkmodels.Port(id=interface.id,
                                  device_id=interface.device_id,
                                  tenant_id=interface.tenant_id,
                                  created_at=interface.created_at,
                                  backend_key="NVP_TEMP_KEY",
                                  network_id=network_id)
        port_cache[interface.id] = q_port
        session.add(q_port)
    print "warn :Found " + str(no_network_count) +\
        " interfaces with no network"


def associate_ips_with_ports(session=None):
    for port in port_cache:
        q_port = port_cache[port]
        for ip in interface_ip[port]:
            q_port.ip_addresses.append(ip)


def migrate_allocatable_ips(session=None, block=None):
    addresses = session.query(melange.AllocatableIPs)\
                       .filter_by(ip_block_id=block.id).all()
    for address in addresses:
        """If marked for deallocation put it into the quark ip table
        as deallocated
        """
        preip = netaddr.IPAddress(address.address)
        version = preip.version
        ip = netaddr.IPAddress(address.address).ipv6()
        q_ip = quarkmodels.IPAddress(id=address.id,
                                     created_at=address.created_at,
                                     tenant_id=block.tenant_id,
                                     network_id=block.network_id,
                                     subnet_id=block.id,
                                     version=version,
                                     address_readable=address.address,
                                     _deallocated=True,
                                     address=int(ip))
        session.add(q_ip)


def _to_mac_range(val):
    cidr_parts = val.split("/")
    prefix = cidr_parts[0]
    prefix = prefix.replace(':', '')
    prefix = prefix.replace('-', '')
    prefix_length = len(prefix)
    if prefix_length < 6 or prefix_length > 10:
        pass
        #raise quark_exceptions.InvalidMacAddressRange(cidr=val)

    diff = 12 - len(prefix)
    if len(cidr_parts) > 1:
        mask = int(cidr_parts[1])
    else:
        mask = 48 - diff * 4
    mask_size = 1 << (48 - mask)
    prefix = "%s%s" % (prefix, "0" * diff)
    try:
        cidr = "%s/%s" % (str(netaddr.EUI(prefix)).replace("-", ":"), mask)
    except netaddr.AddrFormatError:
        pass
        #raise quark_exceptions.InvalidMacAddressRange(cidr=val)
    prefix_int = int(prefix, base=16)
    return cidr, prefix_int, prefix_int + mask_size


def migrate_macs(session=None):
    """2. Migrate the m.mac_address -> q.quark_mac_addresses
    This is the next simplest but the relationship between quark_networks
    and quark_mac_addresses may be complicated to set up (if it exists)
    """
    """Only migrating the first mac_address_range from melange."""
    mac_range = session.query(melange.MacAddressRanges).first()
    cidr = mac_range.cidr
    cidr, first_address, last_address = _to_mac_range(cidr)

    q_range = quarkmodels.MacAddressRange(id=mac_range.id,
                                          cidr=cidr,
                                          created_at=mac_range.created_at,
                                          first_address=first_address,
                                          next_auto_assign_mac=first_address,
                                          last_address=last_address)
    session.add(q_range)

    res = session.query(melange.MacAddresses).all()
    no_network_count = 0
    for mac in res:
        if mac.interface_id not in interface_network:
            no_network_count += 1
            continue
        tenant_id = interface_tenant[mac.interface_id]
        q_mac = quarkmodels.MacAddress(tenant_id=tenant_id,
                                       created_at=mac.created_at,
                                       mac_address_range_id=mac_range.id,
                                       address=mac.address)
        q_port = port_cache[mac.interface_id]
        q_port.mac_address = q_mac.address
        session.add(q_mac)

    print "warn :skipped " + str(no_network_count) + " mac addresses"


def migrate_commit(session):
    """4. Commit the changes to the database"""
    session.commit()


def migrate(session):
    """
    This will migrate an existing melange database to a new quark
    database. Below melange is referred to as m and quark as q.
    """
    totes = 0.0
    totes += do_and_time("migrate networks, subnets, routes, and ips",
                         migrate_networks, session=session)
    totes += do_and_time("migrate ports", migrate_interfaces,
                         session=session)
    totes += do_and_time("associating ips with ports",
                         associate_ips_with_ports, session=session)
    totes += do_and_time("migrate macs and ranges",
                         migrate_macs, session=session)
    totes += do_and_time("commit changes", migrate_commit, session=session)
    print "Total: " + str(totes) + " seconds"
    exit(0)


if __name__ == "__main__":
    session = loadSession()
    flush_db()
    migrate(session)