import re
import traceback
from swift import gettext_ as _
from zerocloud.common import SwiftPath, ZvmNode, ZvmChannel, is_zvm_path, \
    ACCESS_READABLE, ACCESS_CDR, ACCESS_WRITABLE, parse_location, ACCESS_RANDOM, \
    has_control_chars, DEVICE_MAP, is_swift_path, ACCESS_NETWORK

CHANNEL_TYPE_MAP = {
    'stdin': 0,
    'stdout': 0,
    'stderr': 0,
    'input': 3,
    'output': 3,
    'debug': 0,
    'image': 1,
    'sysimage': 3
}
ENV_ITEM = 'name=%s, value=%s\n'
STD_DEVICES = ['stdin', 'stdout', 'stderr']


# quotes commas as \x2c for [env] stanza in nvram file
# see ZRT docs
def quote_for_env(val):
    return re.sub(r',', '\\x2c', str(val))


class ClusterConfigParsingError(Exception):
    def __init__(self, msg):
        self.msg = msg

    def __str__(self):
        return str(self.msg)


class ClusterConfigParser(object):
    def __init__(self, sysimage_devices, default_content_type,
                 parser_config,
                 list_account_callback, list_container_callback):
        """
        Create a new parser instance

        :param sysimage_devices: dict of known system image devices
        :param default_content_type: default content type to use for writable objects
        :param parser_config: configuration dictionary
        :param list_account_callback: callback function that can be called with
                (account_name, mask) to get a list of container names in account
                that match the mask regex
        :param list_container_callback: callback function that can be called with
                (account_name, container_name, mask) to get a list of object names in container
                that match the mask regex
        """
        self.sysimage_devices = sysimage_devices
        self.list_account = list_account_callback
        self.list_container = list_container_callback
        self.nodes = {}
        self.node_list = []
        self.default_content_type = default_content_type
        self.node_id = 1
        self.total_count = 0
        self.parser_config = parser_config

    def find_objects(self, path, **kwargs):
        """
        Find all objects in SwiftPath with wildcards

        :param path: SwiftPath object that has wildcards in url string
        :param **kwargs: optional arguments for list_container, list_account callbacks

        :returns list of object names, raises ClusterConfigParsingError on empty list
        :raises ClusterConfigParsingError: on all errors
        """
        temp_list = []
        if '*' in path.container:
            mask = re.compile(re.escape(path.container).replace('\\*', '.*'))
            try:
                containers = self.list_account(path.account, mask=mask, **kwargs)
            except Exception:
                raise ClusterConfigParsingError(_('Error querying object server '
                                                  'for account: %s') % path.account)
            if path.obj:
                obj = path.obj
                if '*' in obj:
                    obj = re.escape(obj).replace('\\*', '.*')
                mask = re.compile(obj)
            else:
                mask = None
            for container in containers:
                try:
                    obj_list = self.list_container(path.account,
                                                   container,
                                                   mask=mask, **kwargs)
                except Exception:
                    raise ClusterConfigParsingError(_('Error querying object server '
                                                      'for container: %s') % container)
                for obj in obj_list:
                    temp_list.append(SwiftPath.init(path.account, container, obj))
        else:
            obj = re.escape(path.obj).replace('\\*', '.*')
            mask = re.compile(obj)
            try:
                for obj in self.list_container(path.account,
                                               path.container,
                                               mask=mask, **kwargs):
                    temp_list.append(SwiftPath.init(path.account,
                                                    path.container,
                                                    obj))
            except Exception:
                raise ClusterConfigParsingError(_('Error querying object server '
                                                  'for container: %s') % path.container)
        if not temp_list:
            raise ClusterConfigParsingError(_('No objects found in path %s')
                                            % path.url)
        return temp_list

    def _get_new_node(self, zvm_node, index=0):
        if index == 0:
            new_name = zvm_node.name
        else:
            new_name = _create_node_name(zvm_node.name, index)
        new_node = self.nodes.get(new_name)
        if not new_node:
            new_node = zvm_node.copy(self.node_id, new_name)
            self.nodes[new_name] = new_node
            self.node_id += 1
        return new_node

    def _add_all_connections(self, node_name, connections, source_devices):
        if self.nodes.get(node_name):
            connect_node = self.nodes.get(node_name)
            for bind_name in connections:
                src_dev = None
                dst_dev = None
                if source_devices:
                    devices = source_devices.get(bind_name, None)
                    if devices:
                        (src_dev, dst_dev) = devices
                self._add_connection(connect_node, bind_name, src_dev, dst_dev)
        elif self.nodes.get(node_name + '-1'):
            j = 1
            connect_node = self.nodes.get(_create_node_name(node_name, j))
            while connect_node:
                for bind_name in connections:
                    src_dev = None
                    dst_dev = None
                    if source_devices:
                        devices = source_devices.get(bind_name, None)
                        if devices:
                            (src_dev, dst_dev) = devices
                    self._add_connection(connect_node, bind_name, src_dev, dst_dev)
                j += 1
                connect_node = self.nodes.get(
                    _create_node_name(node_name, j))
        else:
            raise ClusterConfigParsingError(_('Non existing node in connect string for node %s') % node_name)

    def parse(self, cluster_config, add_user_image, account_name=None, replica_count=1, **kwargs):
        """
        Parse deserialized config and build separate job configs per node

        :param cluster_config: deserialized JSON cluster map
        :param add_user_image: True if we need to add user image channel to all nodes
        :param **kwargs: optional arguments for list_container, list_account callbacks

        :raises ClusterConfigParsingError: on all errors
        """
        self.nodes = {}
        self.node_id = 1
        self.node_list = []
        try:
            connect_devices = {}
            for node in cluster_config:
                zvm_node = _create_node(node)
                node_count = node.get('count', 1)
                if isinstance(node_count, int) and node_count > 0:
                    pass
                else:
                    raise ClusterConfigParsingError(_('Invalid node count: %s') % str(node_count))
                file_list = node.get('file_list')
                read_list = []
                write_list = []
                other_list = []

                if file_list:
                    for f in file_list:
                        channel = _create_channel(
                            f, zvm_node,
                            default_content_type=self.default_content_type)
                        if is_zvm_path(channel.path):
                            _add_connected_device(connect_devices, channel, zvm_node)
                            continue
                        if channel.access < 0:
                            if self.is_sysimage_device(channel.device):
                                other_list.append(channel)
                                continue
                            raise ClusterConfigParsingError(_('Unknown device %s in %s')
                                                            % (channel.device, zvm_node.name))
                        if channel.access & ACCESS_READABLE:
                            read_list.insert(0, channel)
                        elif channel.access & ACCESS_CDR:
                            read_list.append(channel)
                        elif channel.access & ACCESS_WRITABLE:
                            write_list.append(channel)
                        else:
                            other_list.append(channel)

                    read_group = False
                    for chan in read_list:
                        if '*' in chan.path.path:
                            read_group = True
                            object_list = self.find_objects(chan.path, **kwargs)
                            read_mask = re.escape(chan.path.path).replace('\\*', '(.*)')
                            read_mask = re.compile(read_mask)
                            node_count = len(object_list)
                            for i in range(node_count):
                                new_path = object_list[i]
                                new_node = self._add_new_channel(zvm_node, chan, index=(i + 1), path=new_path)
                                new_node.store_wildcards(new_path, read_mask)
                        else:
                            if node_count > 1:
                                for i in range(1, node_count + 1):
                                    self._add_new_channel(zvm_node, chan, index=i)
                            else:
                                self._add_new_channel(zvm_node, chan)

                    for chan in write_list:
                        if chan.path and '*' in chan.path.url:
                            if read_group:
                                for i in range(1, node_count + 1):
                                    new_node = self.nodes.get(_create_node_name(zvm_node.name, i))
                                    new_url = _extract_stored_wildcards(chan.path, new_node)
                                    new_node.add_channel(channel=chan,
                                                         path=parse_location(new_url))
                            else:
                                for i in range(1, node_count + 1):
                                    new_name = _create_node_name(zvm_node.name, i)
                                    new_url = chan.path.url.replace('*', new_name)
                                    new_node = self._add_new_channel(zvm_node, chan, index=i,
                                                                     path=parse_location(new_url))
                                    new_node.wildcards = [new_name] * chan.path.url.count('*')
                        elif chan.path:
                            if node_count > 1:
                                raise ClusterConfigParsingError(_('Single path %s for multiple node '
                                                                  'definition: %s, please use wildcard')
                                                                % (chan.path.url, zvm_node.name))
                            self._add_new_channel(zvm_node, chan)
                        else:
                            if 'stdout' not in chan.device \
                                and 'stderr' not in chan.device:
                                raise ClusterConfigParsingError(_('Immediate response is not available '
                                                                  'for device %s') % chan.device)
                            if node_count > 1:
                                for i in range(1, node_count + 1):
                                    self._add_new_channel(zvm_node, chan, index=i)
                            else:
                                self._add_new_channel(zvm_node, chan)
                    for chan in other_list:
                        if self.is_sysimage_device(chan.device):
                            chan.access = ACCESS_RANDOM | ACCESS_READABLE
                        else:
                            if not chan.path:
                                raise ClusterConfigParsingError(_('Path is required for device: %s') % chan.device)
                        if node_count > 1:
                            for i in range(1, node_count + 1):
                                self._add_new_channel(zvm_node, chan, index=i)
                        else:
                            self._add_new_channel(zvm_node, chan)
        except ClusterConfigParsingError:
            raise
        except Exception:
            print traceback.format_exc()
            raise ClusterConfigParsingError('Config parser internal error')

        for node in cluster_config:
            connection_list = node.get('connect')
            node_name = node.get('name')
            src_devices = connect_devices.get(node_name, None)
            if not connection_list:
                if src_devices:
                    connection_list = [connected_node for connected_node in src_devices.iterkeys()]
                else:
                    continue
            self._add_all_connections(node_name, connection_list, src_devices)
        for node_name in sorted(self.nodes.keys()):
            self.node_list.append(self.nodes[node_name])
        if add_user_image:
            for node in self.node_list:
                    node.add_new_channel('image', ACCESS_CDR, removable='yes')
        if account_name:
            self.resolve_path_info(account_name, replica_count)
        self.total_count = 0
        for n in self.node_list:
            self.total_count += n.replicate

    def _add_new_channel(self, node, channel, index=0, path=None, content_type=None):
        new_node = self._get_new_node(node, index=index)
        new_node.add_channel(channel=channel, path=path,
                             content_type=content_type)
        return new_node

    def _add_connection(self, node, bind_name, src_device=None, dst_device=None):
        if not dst_device:
            dst_device = '/dev/in/' + node.name
        else:
            dst_device = _resolve_wildcards(node, dst_device)
        if self.nodes.get(bind_name):
            bind_node = self.nodes.get(bind_name)
            if bind_node is node:
                raise ClusterConfigParsingError('Cannot bind to itself: %s' % bind_name)
            bind_node.bind.append((node.name, dst_device))
            if not src_device:
                node.connect.append((bind_name, '/dev/out/' + bind_name))
            else:
                src_device = _resolve_wildcards(bind_node, src_device)
                node.connect.append((bind_name, src_device))
        elif self.nodes.get(bind_name + '-1'):
            i = 1
            bind_node = self.nodes.get(bind_name + '-1')
            while bind_node:
                if not bind_node is node:
                    bind_node.bind.append((node.name, dst_device))
                    if not src_device:
                        node.connect.append((bind_name + '-' + str(i),
                                             '/dev/out/' + bind_name + '-' + str(i)))
                    else:
                        src_device = _resolve_wildcards(bind_node, src_device)
                        node.connect.append((bind_name + '-' + str(i), src_device))
                i += 1
                bind_node = self.nodes.get(bind_name + '-' + str(i))
        else:
            raise ClusterConfigParsingError('Non-existing node in connect %s' % bind_name)

    def build_connect_string(self, node):
        """
        Builds connect strings from connection information stored in job config

        :param node: ZvmNode object we build strings for
        """
        if not self.nodes:
            return
        node_count = len(self.node_list)
        tmp = []
        for (dst, dst_dev) in node.bind:
            dst_id = self.nodes.get(dst).id
            dst_repl = self.nodes.get(dst).replicate
            proto = ';'.join(map(
                lambda i: 'tcp:%d:0' % (dst_id + i * node_count),
                range(dst_repl)
            ))
            tmp.append(
                ','.join([proto,
                          dst_dev,
                          '0,0',  # type = 0, sequential, etag = 0, not needed
                          str(self.parser_config['limits']['reads']),
                          str(self.parser_config['limits']['rbytes']),
                          '0,0'])
            )
        node.bind = tmp
        tmp = []
        for (dst, dst_dev) in node.connect:
            dst_id = self.nodes.get(dst).id
            dst_repl = self.nodes.get(dst).replicate
            proto = ';'.join(map(
                lambda i: 'tcp:%d:' % (dst_id + i * node_count),
                range(dst_repl)
            ))
            tmp.append(
                ','.join([proto,
                          dst_dev,
                          '0,0',  # type = 0, sequential, etag = 0, not needed
                          '0,0',
                          str(self.parser_config['limits']['writes']),
                          str(self.parser_config['limits']['wbytes'])])
            )
        node.connect = tmp

    def is_sysimage_device(self, device_name):
        """
        Checks if the particular device name is in sysimage devices dict

        :param device_name: name of the device
        :returns True if device is in dict, False otherwise
        """
        return device_name in self.sysimage_devices.keys()

    def get_sysimage(self, device_name):
        """
        Gets real file path for particular sysimage device name

        :param device_name: name of the device
        :returns file path if device is in dict, None otherwise
        """
        return self.sysimage_devices.get(device_name, None)

    def prepare_zerovm_files(self, config, nvram_file, local_object, zerovm_nexe, use_dev_self=True):
        """
        Prepares all the files needed for zerovm session run

        :param config: single node config in deserialized format
        :param nvram_file: nvram file name to write nvram data to
        :param local_object: specific channel from config that is a local channel, can be None
        :param zerovm_nexe: path to nexe binary file
        :param use_dev_self: whether we map nexe binary as /dev/self or not

        :returns zerovm manifest data as string
        """
        zerovm_inputmnfst = (
            'Version=%s\n'
            'Program=%s\n'
            'Timeout=%s\n'
            'Memory=%s,0\n'
            % (
                self.parser_config['manifest']['Version'],
                zerovm_nexe or '/dev/null',
                self.parser_config['manifest']['Timeout'],
                self.parser_config['manifest']['Memory']
            ))
        mode_mapping = {}
        fstab = None

        def add_to_fstab(fstab, device, access, removable='no', mountpoint='/'):
            if not fstab:
                fstab = '[fstab]\n'
            fstab += 'channel=/dev/%s, mountpoint=%s, access=%s, removable=%s\n' \
                     % (device, mountpoint, access, removable)
            return fstab

        channels = []
        for ch in config['channels']:
            device = ch['device']
            type = CHANNEL_TYPE_MAP.get(device)
            if type is None:
                if self.is_sysimage_device(device):
                    type = CHANNEL_TYPE_MAP.get('sysimage')
                else:
                    continue
            access = ch['access']
            if self.is_sysimage_device(device):
                fstab = add_to_fstab(fstab, device, 'ro')
            if access & ACCESS_READABLE:
                zerovm_inputmnfst += \
                    'Channel=%s,/dev/%s,%s,0,%s,%s,0,0\n' % \
                    (ch['lpath'], device, type,
                     self.parser_config['limits']['reads'], self.parser_config['limits']['rbytes'])
            elif access & ACCESS_CDR:
                zerovm_inputmnfst += \
                    'Channel=%s,/dev/%s,%s,0,%s,%s,%s,%s\n' % \
                    (ch['lpath'], device, type,
                     self.parser_config['limits']['reads'], self.parser_config['limits']['rbytes'],
                     self.parser_config['limits']['writes'], self.parser_config['limits']['wbytes'])
                if device in 'image':
                    fstab = add_to_fstab(fstab, device, 'ro', removable=ch['removable'])
            elif access & ACCESS_WRITABLE:
                tag = '0'
                if not ch['path'] or ch is local_object:
                    tag = '1'
                zerovm_inputmnfst += \
                    'Channel=%s,/dev/%s,%s,%s,0,0,%s,%s\n' % \
                    (ch['lpath'], device, type, tag,
                     self.parser_config['limits']['writes'], self.parser_config['limits']['wbytes'])
            elif access & ACCESS_NETWORK:
                zerovm_inputmnfst += \
                    'Channel=%s,/dev/%s,%s,0,0,0,%s,%s\n' % \
                    (ch['lpath'], device, type,
                     self.parser_config['limits']['writes'], self.parser_config['limits']['wbytes'])
            mode = ch.get('mode', None)
            if mode:
                mode_mapping[device] = mode
            channels.append(device)
        network_devices = []
        for conn in config['connect'] + config['bind']:
            zerovm_inputmnfst += 'Channel=%s\n' % conn
            dev = conn.split(',', 2)[1][5:]  # len('/dev/') = 5
            if dev in STD_DEVICES:
                network_devices.append(dev)
        for dev in STD_DEVICES:
            if not dev in channels and not dev in network_devices:
                if 'stdin' in dev:
                    zerovm_inputmnfst += \
                        'Channel=/dev/null,/dev/stdin,0,0,%s,%s,0,0\n' % \
                        (self.parser_config['limits']['reads'], self.parser_config['limits']['rbytes'])
                else:
                    zerovm_inputmnfst += \
                        'Channel=/dev/null,/dev/%s,0,0,0,0,%s,%s\n' % \
                        (dev, self.parser_config['limits']['writes'], self.parser_config['limits']['wbytes'])
        if use_dev_self:
            zerovm_inputmnfst += \
                'Channel=%s,/dev/self,3,0,%s,%s,0,0\n' % \
                (zerovm_nexe, self.parser_config['limits']['reads'], self.parser_config['limits']['rbytes'])
        env = None
        if config.get('env'):
            env = '[env]\n'
            if local_object:
                if local_object['access'] & (ACCESS_READABLE | ACCESS_CDR):
                    metadata = local_object['meta']
                    env += ENV_ITEM % ('CONTENT_LENGTH', local_object['size'])
                    env += ENV_ITEM % ('CONTENT_TYPE',
                                       quote_for_env(metadata.get('Content-Type',
                                                                  'application/octet-stream')))
                    for k, v in metadata.iteritems():
                        meta = k.upper()
                        if meta.startswith('X-OBJECT-META-'):
                            env += ENV_ITEM % ('HTTP_%s' % meta.replace('-', '_'),
                                               quote_for_env(v))
                            continue
                        for hdr in ['X-TIMESTAMP', 'ETAG', 'CONTENT-ENCODING']:
                            if hdr in meta:
                                env += ENV_ITEM % ('HTTP_%s' % meta.replace('-', '_'),
                                                   quote_for_env(v))
                                break
                elif local_object['access'] & ACCESS_WRITABLE:
                    env += ENV_ITEM % ('CONTENT_TYPE',
                                       quote_for_env(local_object.get('content_type',
                                                                      'application/octet-stream')))
                    meta = local_object.get('meta', None)
                    if meta:
                        for k, v in meta.iteritems():
                            env += ENV_ITEM % ('HTTP_X_OBJECT_META_%s' % k.upper().replace('-', '_'),
                                               quote_for_env(v))
                env += ENV_ITEM % ('DOCUMENT_ROOT', '/dev/%s' % local_object['device'])
                config['env']['REQUEST_METHOD'] = 'POST'
                config['env']['PATH_INFO'] = local_object['path_info']
            for k, v in config['env'].iteritems():
                if v:
                    env += ENV_ITEM % (k, quote_for_env(v))
        args = '[args]\nargs = %s' % config['name']
        if config.get('args'):
            args += ' %s' % config['args']
        args += '\n'
        mapping = None
        if mode_mapping:
            mapping = '[mapping]\n'
            for ch_device, mode in mode_mapping.iteritems():
                mapping += 'channel=/dev/%s, mode=%s\n' % (ch_device, mode)

        fd = open(nvram_file, 'wb')
        for chunk in [fstab, args, env, mapping]:
            fd.write(chunk or '')
        fd.close()
        zerovm_inputmnfst += \
            'Channel=%s,/dev/nvram,3,0,%s,%s,%s,%s\n' % \
            (nvram_file, self.parser_config['limits']['reads'], self.parser_config['limits']['rbytes'], 0, 0)
        zerovm_inputmnfst += 'Node=%d\n' \
                             % (config['id'])
        if 'name_service' in config:
            zerovm_inputmnfst += 'NameServer=%s\n' \
                                 % config['name_service']
        return zerovm_inputmnfst

    def resolve_path_info(self, account_name, replica_count):
        default_path_info = '/%s' % account_name
        for node in self.node_list:
            top_channel = node.channels[0]
            if top_channel and is_swift_path(top_channel.path):
                if top_channel.access & (ACCESS_READABLE | ACCESS_CDR):
                    node.path_info = top_channel.path.path
                elif top_channel.access & ACCESS_WRITABLE and node.replicate > 0:
                    node.path_info = top_channel.path.path
                    node.replicate = replica_count
                else:
                    node.path_info = default_path_info
            if node.replicate == 0:
                node.replicate = 1

def _add_connected_device(devices, channel, zvm_node):
    if not devices.get(zvm_node.name, None):
        devices[zvm_node.name] = {}
    devices[zvm_node.name][channel.path.host] = (
        '/dev/' + channel.device, channel.path.device)


def _create_node_name(node_name, i):
    return '%s-%d' % (node_name, i)


def _resolve_wildcards(node, param):
    if param.count('*') > 0:
        for wc in getattr(node, 'wildcards', []):
            param = param.replace('*', wc, 1)
        if param.count('*') > 0:
            raise ClusterConfigParsingError('Cannot resolve wildcard for node %s' % node.name)
    return param


def _extract_stored_wildcards(path, node):
    new_url = path.url
    for wc in node.wildcards:
        new_url = new_url.replace('*', wc, 1)
    if new_url.count('*') > 0:
        raise ClusterConfigParsingError(_('Wildcards in input cannot be '
                                          'resolved into output path %s')
                                        % path)
    return new_url


def _create_node(node_config):
    name = node_config.get('name')
    if not name:
        raise ClusterConfigParsingError(_('Must specify node name'))
    if has_control_chars(name):
        raise ClusterConfigParsingError(_('Invalid node name'))
    nexe = node_config.get('exec')
    if not nexe:
        raise ClusterConfigParsingError(_('Must specify exec stanza for %s') % name)
    exe = parse_location(nexe.get('path'))
    if not exe:
        raise ClusterConfigParsingError(_('Must specify executable path for %s') % name)
    if is_zvm_path(exe):
        raise ClusterConfigParsingError(_('Executable path cannot be a zvm path in %s') % name)
    args = nexe.get('args')
    env = nexe.get('env')
    if has_control_chars('%s %s %s' % (exe.url, args, env)):
        raise ClusterConfigParsingError(_('Invalid nexe property for %s') % name)
    replicate = node_config.get('replicate', 1)
    return ZvmNode(0, name, exe, args, env, replicate)


def _create_channel(channel, node, default_content_type=None):
    device = channel.get('device')
    if has_control_chars(device):
        raise ClusterConfigParsingError(_('Bad device name: %s in %s') % (device, node.name))
    path = parse_location(channel.get('path'))
    if not device:
        raise ClusterConfigParsingError(_('Must specify device for file in %s') % node.name)
    access = DEVICE_MAP.get(device, -1)
    mode = channel.get('mode', None)
    meta = channel.get('meta', {})
    content_type = channel.get('content_type', default_content_type if path else 'text/html')
    if access & ACCESS_READABLE and path:
        if not is_swift_path(path):
            raise ClusterConfigParsingError(_('Readable device must be a swift object'))
        if not path.account or not path.container:
            raise ClusterConfigParsingError(_('Invalid path %s in %s')
                                            % (path.url, node.name))
    return ZvmChannel(device, access, path=path,
                      content_type=content_type, meta_data=meta, mode=mode)