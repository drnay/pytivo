import getopt
import logging
import logging.config
import os
import re
import socket
import sys
import uuid
import configparser
from configparser import NoOptionError
from functools import reduce


# Any configuration section whose name is not special is considered a share section
special_section_names = ('Server',      # for main pytivo settings
                         'togo',        # for togo settings
                         'loggers',     # for advanced logging (see logging.config)
                         'handlers',    #  "
                         'formatters',  #  "
                        )
special_section_prefixes = ('_tivo_',       # for override setting for a tivo w/ a specific tsn
                            'logger_',      # for advanced logging (see logging.config)
                            'handler_',     #  "
                            'formatter_',   #  "
                           )

SCRIPTDIR = os.path.dirname(__file__)

# global variables
tivos = {}
tivos_found = False
guid = uuid.uuid4()
config_files = ['/etc/pyTivo.conf', os.path.join(SCRIPTDIR, 'pyTivo.conf')]
configs_found = False
config = None
bin_paths = {}

class Error(Exception):
    """Base class for exceptions in this module."""
    pass

class Bdict(dict):
    def getboolean(self, x):
        return self.get(x, 'False').lower() in ('1', 'yes', 'true', 'on')

def init(argv):
    global config_files

    try:
        opts, _ = getopt.getopt(argv, 'c:e:', ['config=', 'extraconf='])
    except getopt.GetoptError as msg:
        print(msg)

    for opt, value in opts:
        if opt in ('-c', '--config'):
            config_files = [value]
        elif opt in ('-e', '--extraconf'):
            config_files.append(value)

    reset()

def reset():
    global bin_paths
    global config
    global configs_found
    global tivos_found

    bin_paths = {}

    config = configparser.ConfigParser(interpolation=None)
    configs_found = config.read(config_files)
    if not configs_found:
        print(('WARNING: pyTivo.conf does not exist.\n'
               'Assuming default values.'))
        configs_found = config_files[-1:]

    for section in config.sections():
        if section.startswith('_tivo_'):
            tsn = section[6:]
            if tsn.upper() not in ['SD', 'HD', '4K']:
                tivos_found = True
                tivos[tsn] = Bdict(config.items(section))

    for section in ['Server', '_tivo_SD', '_tivo_HD', '_tivo_4K']:
        if not config.has_section(section):
            config.add_section(section)

def write():
    f = open(configs_found[-1], 'w')
    config.write(f)
    f.close()

def tivos_by_ip(tivoIP):
    """
    Get the tsn for a tivo with the given IPv4 address
    """
    for key, value in tivos.items():
        # some tivo entries may not have an 'address' if they were
        # created from a config _tivo_TSN section
        if 'address' in value and value['address'] == tivoIP:
            return key
    raise Error('No TiVo w/ IP:{} was found'.format(tivoIP))

def get_server(name, default=None):
    if config.has_option('Server', name):
        return config.get('Server', name)
    return default

def get_togo(name, default=None):
    if config.has_option('togo', name):
        return config.get('togo', name)

    # many togo options used to be in the server section with
    # the name prefixed w/ 'togo_', so check for those values
    # before returning the default
    return get_server('togo_{}'.format(name), default)

def getGUID():
    return str(guid)

def get_ip(tsn=None):
    try:
        assert tsn
        dest_ip = tivos[tsn]['address']
    except:
        dest_ip = '4.2.2.1'
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect((dest_ip, 123))
    return s.getsockname()[0]

def get_zc():
    opt = get_server('zeroconf', 'auto').lower()

    if opt == 'auto':
        for section in config.sections():
            if section.startswith('_tivo_'):
                if config.has_option(section, 'shares'):
                    logger = logging.getLogger('pyTivo.config')
                    logger.info('Shares security in use -- zeroconf disabled')
                    return False
    elif opt in ['false', 'no', 'off']:
        return False

    return True

def getBeaconAddresses():
    return get_server('beacon', '255.255.255.255')

def getPort():
    return get_server('port', '9032')

def get169Blacklist(tsn):  # tivo does not pad 16:9 video
    return tsn and not isHDtivo(tsn) and not get169Letterbox(tsn)
    # verified Blacklist Tivo's are ('130', '240', '540')
    # It is assumed all remaining non-HD and non-Letterbox tivos are Blacklist

def get169Letterbox(tsn):  # tivo pads 16:9 video for 4:3 display
    return tsn and tsn[:3] in ['649']

def get169Setting(tsn):
    if not tsn:
        return True

    tsnsect = '_tivo_' + tsn
    if config.has_section(tsnsect):
        if config.has_option(tsnsect, 'aspect169'):
            try:
                return config.getboolean(tsnsect, 'aspect169')
            except ValueError:
                pass

    if get169Blacklist(tsn) or get169Letterbox(tsn):
        return False

    return True

def getAllowedClients():
    return get_server('allowedips', '').split()

def getIsExternal(tsn):
    tsnsect = '_tivo_' + tsn
    if tsnsect in config.sections():
        if config.has_option(tsnsect, 'external'):
            try:
                return config.getboolean(tsnsect, 'external')
            except ValueError:
                pass

    return False

def isTsnInConfig(tsn):
    return ('_tivo_' + tsn) in config.sections()

def getShares(tsn=''):
    shares = [(section, Bdict(config.items(section)))
              for section in config.sections()
              if not (section.startswith(special_section_prefixes)
                      or section in special_section_names)
             ]

    tsnsect = '_tivo_' + tsn
    if config.has_section(tsnsect) and config.has_option(tsnsect, 'shares'):
        # clean up leading and trailing spaces & make sure ref is valid
        tsnshares = []
        for x in config.get(tsnsect, 'shares').split(','):
            y = x.strip()
            if config.has_section(y):
                tsnshares.append((y, Bdict(config.items(y))))
        shares = tsnshares

    shares.sort()

    if not config.getboolean('Server', 'nosettings', fallback=False):
        shares.append(('Settings', {'type': 'settings'}))
    if get_server('tivo_mak') and get_togo('path'):
        shares.append(('ToGo', {'type': 'togo'}))

    return shares

def getDebug():
    try:
        return config.getboolean('Server', 'debug')
    except:
        return False

def getOptres(tsn=None):
    try:
        return config.getboolean('_tivo_' + tsn, 'optres')
    except:
        try:
            return config.getboolean(get_section(tsn), 'optres')
        except:
            try:
                return config.getboolean('Server', 'optres')
            except:
                return False

def get_bin(fname):
    logger = logging.getLogger('pyTivo.config')

    if fname in bin_paths:
        return bin_paths[fname]

    if config.has_option('Server', fname):
        fpath = config.get('Server', fname)
        if os.path.exists(fpath) and os.path.isfile(fpath):
            bin_paths[fname] = fpath
            return fpath
        else:
            logger.error('Bad %s path: %s', fname, fpath)

    if sys.platform == 'win32':
        fext = '.exe'
    else:
        fext = ''

    for path in ([os.path.join(SCRIPTDIR, 'bin')] +
                 os.getenv('PATH').split(os.pathsep)):
        fpath = os.path.join(path, fname + fext)
        if os.path.exists(fpath) and os.path.isfile(fpath):
            bin_paths[fname] = fpath
            return fpath

    logger.warning('%s not found', fname)
    bin_paths[fname] = None
    return None

def getFFmpegWait():
    if config.has_option('Server', 'ffmpeg_wait'):
        return max(int(float(config.get('Server', 'ffmpeg_wait'))), 1)
    return 0

def getFFmpegPrams(tsn):
    return get_tsn('ffmpeg_pram', tsn, True)

def isHDtivo(tsn):  # TSNs of High Definition TiVos
    return bool(tsn and tsn[0] >= '6' and tsn[:3] != '649')

def is4Ktivo(tsn):  # TSNs of 4K TiVos
    return bool(tsn[:3] in ('849', '8F9'))

def get_ts_flag():
    return get_server('ts', 'auto').lower()

def is_ts_capable(tsn):  # tsn's of Tivos that support transport streams
    return bool(tsn and (tsn[0] >= '7' or tsn.startswith('663')))

def getValidWidths():
    return [1920, 1440, 1280, 720, 704, 544, 480, 352]

def getValidHeights():
    return [1080, 720, 480] # Technically 240 is also supported

# Return the number in list that is nearest to x
# if two values are equidistant, return the larger
def nearest(x, lst):
    return reduce(lambda a, b: closest(x, a, b), lst)

def closest(x, a, b):
    da = abs(x - a)
    db = abs(x - b)
    if da < db or (da == db and a > b):
        return a
    return b

def nearestTivoHeight(height):
    return nearest(height, getValidHeights())

def nearestTivoWidth(width):
    return nearest(width, getValidWidths())

def getTivoHeight(tsn):
    if is4Ktivo(tsn):
        return 2160
    return 1080 if isHDtivo(tsn) else 480

def getTivoWidth(tsn):
    if is4Ktivo(tsn):
        return 3840
    return 1920 if isHDtivo(tsn) else 544

def _trunc64(i):
    return max(int(strtod(i)) / 64000, 1) * 64

def getAudioBR(tsn=None):
    rate = get_tsn('audio_br', tsn)
    if not rate:
        rate = '448k'
    # convert to non-zero multiple of 64 to ensure ffmpeg compatibility
    # compare audio_br to max_audio_br and return lowest
    return str(min(_trunc64(rate), getMaxAudioBR(tsn))) + 'k'

def _k(i):
    return str(int(strtod(i)) / 1000) + 'k'

def getVideoBR(tsn=None):
    rate = get_tsn('video_br', tsn)
    if rate:
        return _k(rate)
    if is4Ktivo(tsn):
        return getMaxVideoBR(tsn)
    return '16384K' if isHDtivo(tsn) else '4096K'

def getMaxVideoBR(tsn=None):
    rate = get_tsn('max_video_br', tsn)
    if rate:
        return _k(rate)
    return '30000k'

def getBuffSize(tsn=None):
    size = get_tsn('bufsize', tsn)
    if size:
        return _k(size)
    if is4Ktivo(tsn):
        return '8192k'
    return '4096k' if isHDtivo(tsn) else '1024k'

def getMaxAudioBR(tsn=None):
    rate = get_tsn('max_audio_br', tsn)
    # convert to non-zero multiple of 64 for ffmpeg compatibility
    if rate:
        return _trunc64(rate)
    return 448

def get_section(tsn):
    if is4Ktivo(tsn):
        return '_tivo_4K'
    return '_tivo_HD' if isHDtivo(tsn) else '_tivo_SD'

def get_tsn(name, tsn=None, raw=False):
    try:
        return config.get('_tivo_' + tsn, name, raw=raw)
    except:
        try:
            return config.get(get_section(tsn), name, raw=raw)
        except:
            try:
                return config.get('Server', name, raw=raw)
            except:
                return None

# Parse a bitrate using the SI/IEEE suffix values as if by ffmpeg
# For example, 2K==2000, 2Ki==2048, 2MB==16000000, 2MiB==16777216
# Algorithm: http://svn.mplayerhq.hu/ffmpeg/trunk/libavcodec/eval.c
def strtod(value):
    prefixes = {'y': -24, 'z': -21, 'a': -18, 'f': -15, 'p': -12,
                'n': -9,  'u': -6,  'm': -3,  'c': -2,  'd': -1,
                'h': 2,   'k': 3,   'K': 3,   'M': 6,   'G': 9,
                'T': 12,  'P': 15,  'E': 18,  'Z': 21,  'Y': 24}
    p = re.compile(r'^(\d+)(?:([yzafpnumcdhkKMGTPEZY])(i)?)?([Bb])?$')
    m = p.match(value)
    if not m:
        raise SyntaxError('Invalid bit value syntax')
    (coef, prefix, power, byte) = m.groups()
    if prefix is None:
        value = float(coef)
    else:
        exponent = float(prefixes[prefix])
        if power == 'i':
            # Use powers of 2
            value = float(coef) * pow(2.0, exponent / 0.3)
        else:
            # Use powers of 10
            value = float(coef) * pow(10.0, exponent)
    if byte == 'B': # B == Byte, b == bit
        value *= 8
    return value

def init_logging():
    if     (config.has_section('loggers') and
            config.has_section('handlers') and
            config.has_section('formatters')):
        logging.config.fileConfig(config_files)
    elif getDebug():
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)
