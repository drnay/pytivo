import logging
import re
import socket
import struct
import time
import uuid
from threading import Timer
from urllib.parse import quote

import zeroconf

import config
from plugin import GetPlugin

SHARE_TEMPLATE = '/TiVoConnect?Command=QueryContainer&Container=%s'
PLATFORM_MAIN = 'pyTivo'
PLATFORM_VIDEO = 'pc/pyTivo'    # For the nice icon


# It's possible this function should live somewhere else, but for now this
# is the only module that needs it. -mjl
def bytes2str(data):
    """
    Convert bytes to str as utf-8. sequence values (and keys) will also be converted.
    """
    # pylint: disable=bad-whitespace,multiple-statements

    if isinstance(data, bytes):  return data.decode('utf-8')
    if isinstance(data, dict):   return dict(map(bytes2str, data.items()))
    if isinstance(data, tuple):  return map(bytes2str, data)
    return data

def log_serviceinfo(logger, info):
    """
    Write interesting attributes from a ServiceInfo to the log.
    Information written depends on the log level, basic info
    is written w/ log level INFO, if the log level is DEBUG
    more the basic info plus more (all properties) is written
    w/ log level DEBUG.
    """

    try:
        debugging = logger.isEnabledFor(logging.DEBUG)
        log_level = logging.INFO

        log_info = {'name': info.name,
                    'address': socket.inet_ntoa(info.address),
                    'port': info.port}
        log_hdr = "\n  {address}:{port} {name}\n"
        log_fmt = log_hdr

        if debugging:
            log_level = logging.DEBUG
            if info.server != info.name:
                log_info['server'] = info.server
                log_fmt += "    server: {server}\n"

            for (k, v) in info.properties.items():
                li_k = "prop_" + bytes2str(k)
                log_info[li_k] = v
                log_fmt += "    {k}: {{{li_k}}}\n".format(k=k, li_k=li_k)

        logger.log(log_level, log_fmt.format(**log_info))

    except:
        logger.exception("exception in log_tivo_serviceinfo")


class ZCListener:
    # pylint: disable=redefined-builtin

    def __init__(self, names):
        self.names = names

    def remove_service(self, server, type, name):
        self.names.remove(name.replace('.' + type, ''))

    def add_service(self, server, type, name):
        self.names.append(name.replace('.' + type, ''))

class ZCBroadcast:
    def __init__(self, logger):
        """ Announce our shares via Zeroconf. """
        self.share_names = []
        self.share_info = []
        self.logger = logger
        self.rz = zeroconf.Zeroconf()
        self.renamed = {}
        old_titles = self.scan()
        address = socket.inet_aton(config.get_ip())
        port = int(config.getPort())
        logger.info('Announcing pytivo shares ({}:{})...'.format(config.get_ip(), port))
        for section, settings in config.getShares():
            try:
                plugin = GetPlugin(settings['type'])
                ct = plugin.CONTENT_TYPE
                # if the plugin provides a test for validity use it otherwise assume valid
                if hasattr(plugin, 'is_valid') and not plugin.is_valid(section, settings):
                    logger.warning('share "%s" is invalid. It will be ignored (maybe check that path exists)', section)
                    continue
            except Exception as e:
                logger.error('ZCBroadcast.__init__: raised %s: %s', e.__class__.__name__, e)
                continue

            if ct.startswith('x-container/'):
                if 'video' in ct:
                    platform = PLATFORM_VIDEO
                else:
                    platform = PLATFORM_MAIN

                logger.info('Registering: %s' % section)
                self.share_names.append(section)

                desc = {b'path': bytes(SHARE_TEMPLATE % quote(section), 'utf-8'),
                        b'platform': bytes(platform, 'utf-8'),
                        b'protocol': b'http',
                        b'tsn': bytes('{%s}' % uuid.uuid4(), 'utf-8')}
                tt = ct.split('/')[1]
                title = section
                count = 1
                while title in old_titles:
                    # debugging info while I try to figure out what this loop is for
                    logger.info(" title b4: {}".format(title))
                    count += 1
                    title = '%s [%d]' % (section, count)
                    self.renamed[section] = title
                    # more debugging info
                    logger.info(" title after: {}\n section: {}".format(title, section))

                info = zeroconf.ServiceInfo('_%s._tcp.local.' % tt,
                                            '%s._%s._tcp.local.' % (title, tt),
                                            address, port, 0, 0, desc)

                log_serviceinfo(self.logger, info)
                self.rz.register_service(info)
                self.share_info.append(info)


    def scan(self):
        """ Look for TiVos using Zeroconf. """
        VIDS = '_tivo-videos._tcp.local.'
        names = []

        self.logger.info('Scanning for TiVos...\n')

        # Get the names of servers offering TiVo videos
        browser = zeroconf.ServiceBrowser(self.rz, VIDS, None, ZCListener(names))

        # Give them a second (or more if no one has responded in the 1st second) to respond
        time.sleep(1)
        max_sec_to_wait = 10
        sec_waited = 0
        while not names and sec_waited < max_sec_to_wait:
            sec_waited += 1
            time.sleep(1)

        # Any results?
        if names:
            config.tivos_found = True

        # Now get the addresses -- this is the slow part
        for name in names:
            info = self.rz.get_service_info(VIDS, name + '.' + VIDS)
            log_serviceinfo(self.logger, info)

            if info:
                tsn = info.properties.get(b'TSN')
                if config.get_togo('all'):
                    tsn = info.properties.get(b'tsn', tsn)
                if tsn:
                    if isinstance(tsn, bytes):
                        tsn = tsn.decode('utf-8')
                    address = socket.inet_ntoa(info.address)
                    port = info.port
                    config.tivos[tsn] = {'name': name, 'address': address,
                                         'port': port}
                    # info.properties has bytes keys and values, but we'd rather
                    # deal with str keys and values, so convert them before adding
                    # them to our tivos dict.
                    config.tivos[tsn].update(bytes2str(info.properties))

# Debugging information on what services have been found:
#        try:
#            all_services = zeroconf.ZeroconfServiceTypes.find(self.rz)
#            self.logger.info("All services found")
#            for s in all_services:
#                self.logger.info("  {}".format(s))
#        except Exception as e:
#            self.logger.error(e)


        return names

    def shutdown(self):
        self.logger.info('Unregistering: %s' % ', '.join(self.share_names))
        for info in self.share_info:
            self.rz.unregister_service(info)
        self.rz.close()

class Beacon:
    def __init__(self):
        self.UDPSock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.UDPSock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.services = []
        self.timer = None

        self.platform = PLATFORM_VIDEO
        for section, settings in config.getShares():
            try:
                ct = GetPlugin(settings['type']).CONTENT_TYPE
            except:
                continue
            if ct in ('x-container/tivo-music', 'x-container/tivo-photos'):
                self.platform = PLATFORM_MAIN
                break

        if config.get_zc():
            logger = logging.getLogger('pyTivo.beacon')
            try:
                self.bd = ZCBroadcast(logger)
            except Exception as e:
                logger.debug('Beacon.__init__: raised %s: %s', e.__class__.__name__, e)
                logger.error('Zeroconf failure')
                self.bd = None
        else:
            self.bd = None

    def add_service(self, service):
        self.services.append(service)
        self.send_beacon()

    def format_services(self):
        return ';'.join(self.services)

    def format_beacon(self, conntype, services=True):
        beacon = ['tivoconnect=1',
                  'method=%s' % conntype,
                  'identity={%s}' % config.getGUID(),
                  'machine=%s' % socket.gethostname(),
                  'platform=%s' % self.platform]

        if services:
            beacon.append('services=' + self.format_services())
        else:
            beacon.append('services=TiVoMediaServer:0/http')

        return '\n'.join(beacon) + '\n'

    def send_beacon(self):
        beacon_ips = config.getBeaconAddresses()
        beacon = self.format_beacon('broadcast')
        for beacon_ip in beacon_ips.split():
            if beacon_ip != 'listen':
                try:
                    packet = bytes(beacon, "utf-8")
                    while packet:
                        result = self.UDPSock.sendto(packet, (beacon_ip, 2190))
                        if result < 0:
                            break
                        packet = packet[result:]
                except Exception as e:
                    print(e)

    def start(self):
        self.send_beacon()
        self.timer = Timer(60, self.start)
        self.timer.start()

    def stop(self):
        self.timer.cancel()
        if self.bd:
            self.bd.shutdown()

    @staticmethod
    def recv_bytes(sock, length):
        block = ''
        while len(block) < length:
            add = sock.recv(length - len(block))
            if not add:
                break
            block += add
        return block

    @staticmethod
    def recv_packet(sock):
        length = struct.unpack('!I', Beacon.recv_bytes(sock, 4))[0]
        return Beacon.recv_bytes(sock, length)

    @staticmethod
    def send_packet(sock, packet):
        sock.sendall(struct.pack('!I', len(packet)) + packet)

    def listen(self):
        """ For the direct-connect, TCP-style beacon """
        import _thread

        def server():
            TCPSock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            TCPSock.bind(('', 2190))
            TCPSock.listen(5)

            while True:
                # Wait for a connection
                client, address = TCPSock.accept()

                # Accept (and discard) the client's beacon
                self.recv_packet(client)

                # Send ours
                self.send_packet(client, self.format_beacon('connected'))

                client.close()

        _thread.start_new_thread(server, ())

    def get_name(self, address):
        """ Exchange beacons, and extract the machine name. """
        our_beacon = self.format_beacon('connected', False)
        machine_name = re.compile('machine=(.*)\n').search

        try:
            tsock = socket.socket()
            tsock.connect((address, 2190))
            self.send_packet(tsock, our_beacon)
            tivo_beacon = self.recv_packet(tsock)
            tsock.close()
            name = machine_name(tivo_beacon).groups()[0]
        except:
            name = address

        return name
