import calendar
import logging
import os
import re
import struct
import _thread
import time
import urllib.request, urllib.parse, urllib.error
import zlib
import json
import subprocess
from datetime import datetime
from collections import UserDict
from datetime import datetime, timedelta
from xml.sax.saxutils import escape

from Cheetah.Template import Template
from lrucache import LRUCache

import config
import metadata
from . import transcode
from plugin import Plugin, quote

logger = logging.getLogger('pyTivo.video.video')

SCRIPTDIR = os.path.dirname(__file__)

CLASS_NAME = 'Video'

# Preload the templates
def tmpl(name):
    return open(os.path.join(SCRIPTDIR, 'templates', name), 'rb').read().decode('utf-8')

XML_CONTAINER_TEMPLATE = tmpl('container_xml.tmpl')
TVBUS_TEMPLATE = tmpl('TvBus.tmpl')

EXTENSIONS = """.tivo .mpg .avi .wmv .mov .flv .f4v .vob .mp4 .m4v .mkv
.ts .tp .trp .3g2 .3gp .3gp2 .3gpp .amv .asf .avs .bik .bix .box .bsf
.dat .dif .divx .dmb .dpg .dv .dvr-ms .evo .eye .flc .fli .flx .gvi .ivf
.m1v .m21 .m2t .m2ts .m2v .m2p .m4e .mjp .mjpeg .mod .moov .movie .mp21
.mpe .mpeg .mpv .mpv2 .mqv .mts .mvb .nsv .nuv .nut .ogm .qt .rm .rmvb
.rts .scm .smv .ssm .svi .vdo .vfw .vid .viv .vivo .vp6 .vp7 .vro .webm
.wm .wmd .wtv .yuv""".split()

LIKELYTS = """.ts .tp .trp .3g2 .3gp .3gp2 .3gpp .m2t .m2ts .mts .mp4
.m4v .flv .mkv .mov .wtv .dvr-ms .webm""".split()

# Global variable to track uploads (pulls from the tivo)
status = {}

use_extensions = True
try:
    assert(config.get_bin('ffmpeg'))
except:
    use_extensions = False

def uniso(iso):
    return time.strptime(iso[:19], '%Y-%m-%dT%H:%M:%S')

def isodt(iso):
    return datetime(*uniso(iso)[:6])

def isogm(iso):
    return int(calendar.timegm(uniso(iso)))

def prefix_bin_qty(n):
    """
    Convert n to the largest prefix we know that keeps n > 1,
    returning the smaller n and the appropriate prefix.
    e.g. prefix_bin_qty(2048) returns (2, 'K')
    """
    # SI prefixes are (and although technically incorrect we'll use
    # those decimal prefixes instead of the 2 letter binary prefixes):
    # - k 10^3  kilo  (Ki kibi 2^10)
    # - M 10^6  mega  (Mi mebi 2^20)
    # - G 10^9  giga  (Gi gibi 2^30)
    # - T 10^12 tera  (Ti tibi 2^40)
    # - P 10^15 peta  (Pi pebi 2^50)
    # - E 10^18 exa   (Ei exbi 2^60)
    # - Z 10^21 zetta (Zi zebi 2^70)
    # - Y 10^24 yotta (Yi yobi 2^80)
    prefixes = ('', 'K', 'M', 'G', 'T', 'P', 'E', 'Z', 'Y')
    prefix = 0
    while n >= 1024 and prefix < len(prefixes):
        n /= 1024
        prefix += 1
    return (n, prefixes[prefix])

class Video(Plugin):

    CONTENT_TYPE = 'x-container/tivo-videos'

    tvbus_cache = LRUCache(1)

    def video_file_filter(self, full_path, type=None):
        if os.path.isdir(full_path):
            return True
        if use_extensions:
            return os.path.splitext(full_path)[1].lower() in EXTENSIONS
        else:
            return transcode.supported_format(full_path)

    def GetActiveTransferCount(self, handler, query):
        """
        HTTP command handler to return the count of current active uploads to TiVos
        The count (n) is returned in a json object { count: n }
        """
        global status
        json_config = {}
        count = 0
        for tivo in status:
            for file in status[tivo]:
                if status[tivo][file]['active']:
                    count += 1

        json_config['count'] = count
        handler.send_json(json.dumps(json_config))

    def GetTransferStatus(selfself, handler, query):
        """
        HTTP command handler to return the status of current uploads to TiVos
        as a json object
        """
        global status
        handler.send_json(json.dumps(status))

    def cleanup_status(self):
        global status

        now = time.time()
        for tivo in status:
            for file in status[tivo]:
                if not status[tivo][file]['active']:
                    elapsed = now - status[tivo][file]['end']
                    if elapsed >= 86400: # 86400 = one day
                        del status[tivo][file]

            if len(status[tivo]) < 1:
                del status[tivo]


    def send_file(self, handler, path, query):
        global status

        self.cleanup_status() # Keep status object from getting too big

        mime = 'video/x-tivo-mpeg'
        tsn = handler.headers.get('tsn', '')

        try:
            assert(tsn)
            tivo_name = config.tivos[tsn].get('name', tsn)
        except:
            tivo_name = handler.address_string()

        if not tivo_name in status:
            status[tivo_name] = {}

        is_tivo_file = False
        tivo_header_size = 0
        is_tivo_ts = False

        try:
            with open(path, 'rb') as f:
                tivo_header = f.read(16)

            if tivo_header[0:4] == b'TiVo':
                is_tivo_file = True
                tivo_header_size = struct.unpack_from('>L', tivo_header, 10)[0]

                if (tivo_header[7] & 0x20 != 0):
                    is_tivo_ts = True
        except:
            pass

        tivo_mak = config.get_tsn('tivo_mak', tsn)
        has_tivolibre = bool(config.get_bin('tivolibre'))
        has_tivodecode = bool(config.get_bin('tivodecode'))

        use_tivolibre = False
        if has_tivolibre and bool(config.get_server('tivolibre_upload', True)):
            use_tivolibre = True

        if 'Format' in query:
            mime = query['Format'][0]

        needs_tivodecode = (((is_tivo_file and is_tivo_ts) or
                            (is_tivo_file and not has_tivolibre)) and
                           mime == 'video/mpeg')
        compatible = (not needs_tivodecode and
                      transcode.tivo_compatible(path, tsn, mime)[0])

        try:  # "bytes=XXX-"
            offset = int(handler.headers.get('Range')[6:-1])
        except:
            offset = 0

        if needs_tivodecode:
            valid = bool((has_tivodecode or has_tivolibre) and tivo_mak)
        else:
            valid = True

        if valid and offset:
            valid = ((compatible and offset < os.path.getsize(path)) or
                     (not compatible and transcode.is_resumable(path, offset)))

            if status[tivo_name][path]:
                # Don't let the TiVo loop over and over in the same spot
                valid = (offset != status[tivo_name][path]['offset'])
                status[tivo_name][path]['error'] = 'Repeat offset call'

        #faking = (mime in ['video/x-tivo-mpeg-ts', 'video/x-tivo-mpeg'] and
        faking = (mime == 'video/x-tivo-mpeg' and
                  not (is_tivo_file and compatible))
        thead = ''
        if faking:
            thead = self.tivo_header(tsn, path, mime)

        size = os.path.getsize(path) + len(thead)
        if compatible:
            handler.send_response(200)
            handler.send_header('Content-Length', size - offset)
            handler.send_header('Content-Range', 'bytes %d-%d/%d' %
                                (offset, size - offset - 1, size))
        else:
            handler.send_response(206)
            handler.send_header('Transfer-Encoding', 'chunked')
        handler.send_header('Content-Type', mime)
        handler.end_headers()

        logger.info('[%s] Start sending "%s" to %s' %
                    (time.strftime('%d/%b/%Y %H:%M:%S'), path, tivo_name))

        if valid:
            start_time = time.time()
            last_interval = start_time
            now = start_time
            count = 0
            output = 0

            if path in status[tivo_name]:
                status[tivo_name][path]['active'] = True
                status[tivo_name][path]['offset'] = offset
            else:
                status[tivo_name][path] = {'active':        True,
                                           'decrypting':    False,
                                           'transcoding':   False,
                                           'offset':        offset,
                                           'start':         start_time,
                                           'end':           start_time,
                                           'rate':          0,
                                           'size':          size,
                                           'output':        0,
                                           'error':         '',
                                          }

            if compatible:
                logger.debug('"%s" is tivo compatible' % path)
                f = open(path, 'rb')
                tivolibre = None
                if not offset:
                    if faking:
                        handler.wfile.write(thead)
                        count += len(thead)
                        output += len(thead)
                    elif tivo_header_size > 0:
                        block = f.read(tivo_header_size)
                        handler.wfile.write(block)
                        count += len(block)
                        output += len(block)
                try:
                    if is_tivo_file and use_tivolibre:
                        status[tivo_name][path]['decrypting'] = True

                        f.close()
                        tivolibre_path = config.get_bin('tivolibre')
                        tcmd = [tivolibre_path, '-m', tivo_mak, '-i', path]
                        tivolibre = subprocess.Popen(tcmd, stdout=subprocess.PIPE, bufsize=(512 * 1024))
                        f = tivolibre.stdout

                    if offset:
                        if tivolibre:
                            raise Exception('tivolibre does not support offset')
                        offset -= len(thead)
                        f.seek(offset)

                    while True:
                        block = f.read(512 * 1024)
                        if not block:
                            break
                        handler.wfile.write(block)
                        count += len(block)
                        output += len(block)

                        now = time.time()
                        elapsed = now - last_interval
                        if elapsed >= 1:
                            status[tivo_name][path]['rate'] = (count * 8.0) / elapsed
                            status[tivo_name][path]['output'] += count
                            count = 0
                            last_interval = now

                    if tivolibre:
                        tivolibre.wait()

                except Exception as msg:
                    status[tivo_name][path]['error'] = str(msg)
                    if tivolibre:
                        tivolibre.kill()
                        tivolibre.wait()

                    logger.info(msg)

                f.close()
            else:
                logger.debug('"%s" is not tivo compatible' % path)
                status[tivo_name][path]['transcoding'] = True
                if offset:
                    count = transcode.resume_transfer(path, handler.wfile,
                                                      offset, status[tivo_name][path])
                else:
                    count = transcode.transcode(False, path, handler.wfile,
                                                status[tivo_name][path], is_tivo_file,
                                                tsn, mime, thead)

            end_time = time.time()
            elapsed = end_time - status[tivo_name][path]['start']
            rate = count * 8.0 / elapsed    # bits / sec

            status[tivo_name][path]['active'] = False
            status[tivo_name][path]['end'] = end_time
            status[tivo_name][path]['rate'] = rate

            logger.info('[{timestamp:%d/%b/%Y %H:%M:%S}] Done sending "{fname}" to {tivo_name}, '
                        '{mbps[0]:.2f} {mbps[1]}B/s ({num_bytes[0]:.3f} {num_bytes[1]}Bytes / {seconds:.0f} s)'
                        .format(timestamp=datetime.fromtimestamp(end_time),
                                fname=path, tivo_name=tivo_name,
                                num_bytes=prefix_bin_qty(count),
                                mbps=prefix_bin_qty(rate / 8),
                                seconds=elapsed))

        else:
            logger.info('Invalid file "{}" requested by {}'.format(path, tivo_name))

        try:
            if not compatible:
                handler.wfile.write(b'0\r\n\r\n')
            handler.wfile.flush()
        except Exception as msg:
            logger.exception('Exception writing an empty response for an incompatible file')


    def __duration(self, full_path):
        return transcode.video_info(full_path)['millisecs']

    def __total_items(self, full_path):
        count = 0
        try:
            for f in os.listdir(full_path):
                if f.startswith('.'):
                    continue
                f = os.path.join(full_path, f)
                if os.path.isdir(f):
                    count += 1
                elif use_extensions:
                    if os.path.splitext(f)[1].lower() in EXTENSIONS:
                        count += 1
                elif f in transcode.info_cache:
                    if transcode.supported_format(f):
                        count += 1
        except:
            pass
        return count

    def __est_size(self, full_path, tsn='', mime=''):
        # Size is estimated by taking audio and video bit rate adding 2%

        if transcode.tivo_compatible(full_path, tsn, mime)[0]:
            return os.path.getsize(full_path)
        else:
            # Must be re-encoded
            audioBPS = config.getMaxAudioBR(tsn) * 1000
            #audioBPS = config.strtod(config.getAudioBR(tsn))
            videoBPS = transcode.select_videostr(full_path, tsn)
            bitrate =  audioBPS + videoBPS
            return int((self.__duration(full_path) / 1000) *
                       (bitrate * 1.02 / 8))

    def metadata_full(self, full_path, tsn='', mime='', mtime=None):
        data = {}
        vInfo = transcode.video_info(full_path)

        if ((int(vInfo['vHeight']) >= 720 and
             config.getTivoHeight(tsn) >= 720) or
            (int(vInfo['vWidth']) >= 1280 and
             config.getTivoWidth(tsn) >= 1280)):
            data['showingBits'] = '4096'

        data.update(metadata.basic(full_path, mtime))
        if full_path[-5:].lower() == '.tivo':
            data.update(metadata.from_tivo(full_path))
        if full_path[-4:].lower() == '.wtv':
            data.update(metadata.from_mscore(vInfo['rawmeta']))

        if 'episodeNumber' in data:
            try:
                ep = int(data['episodeNumber'])
            except:
                ep = 0
            data['episodeNumber'] = str(ep)

        # For debugging, co-opt the video Host field which is displayed by
        # the TiVo on the info page of the video to show additional debugging
        # transcoding information.
        if config.getDebug() and 'vHost' not in data:
            compatible, reason = transcode.tivo_compatible(full_path, tsn, mime)
            if compatible:
                transcode_options = []
            else:
                transcode_options = transcode.transcode(True, full_path,
                                                        '', None, False, tsn, mime)
            data['vHost'] = (
                ['TRANSCODE=%s, %s' % (['YES', 'NO'][compatible], reason)] +
                ['SOURCE INFO: '] +
                ["%s=%s" % (k, v)
                 for k, v in sorted(list(vInfo.items()), reverse=True)] +
                ['TRANSCODE OPTIONS: '] +
                transcode_options +
                ['SOURCE FILE: ', os.path.basename(full_path)]
            )

        now = datetime.utcnow()
        if 'time' in data:
            if data['time'].lower() == 'file':
                if not mtime:
                    mtime = os.path.getmtime(full_path)
                try:
                    now = datetime.utcfromtimestamp(mtime)
                except:
                    logger.warning('Bad file time on ' + full_path)
            elif data['time'].lower() == 'oad':
                    now = isodt(data['originalAirDate'])
            else:
                try:
                    now = isodt(data['time'])
                except:
                    logger.warning('Bad time format: ' + data['time'] +
                                   ' , using current time')

        duration = self.__duration(full_path)
        duration_delta = timedelta(milliseconds = duration)
        min = duration_delta.seconds // 60
        sec = duration_delta.seconds % 60
        hours = min // 60
        min = min % 60

        data.update({'time': now.isoformat(),
                     'startTime': now.isoformat(),
                     'stopTime': (now + duration_delta).isoformat(),
                     'size': self.__est_size(full_path, tsn, mime),
                     'duration': duration,
                     'iso_duration': ('P%sDT%sH%sM%sS' %
                                      (duration_delta.days, hours, min, sec))})

        return data

    def QueryContainer(self, handler, query):
        tsn = handler.headers.get('tsn', '')
        subcname = query['Container'][0]

        if not self.get_local_path(handler, query):
            handler.send_error(404)
            return

        container = handler.container
        force_alpha = container.getboolean('force_alpha')
        ar = container.get('allow_recurse', 'auto').lower()
        if ar == 'auto':
            allow_recurse = not tsn or tsn[0] < '7'
        else:
            allow_recurse = ar in ('1', 'yes', 'true', 'on')

        files, total, start = self.get_files(handler, query,
                                             self.video_file_filter,
                                             force_alpha, allow_recurse)

        videos = []
        local_base_path = self.get_local_base_path(handler, query)
        #logger.debug("\nquery={}\nfiles={}\n".format(query, files))
        for f in files:
            video = VideoDetails()
            mtime = f.mdate
            try:
                ltime = time.localtime(mtime)
            except:
                logger.warning('Bad file time on ' + f.name)
                mtime = time.time()
                ltime = time.localtime(mtime)
            video['captureDate'] = hex(int(mtime))
            video['textDate'] = time.strftime('%b %d, %Y', ltime)
            video['name'] = os.path.basename(f.name)
            video['path'] = f.name
            video['part_path'] = f.name.replace(local_base_path, '', 1)
            if not video['part_path'].startswith(os.path.sep):
                video['part_path'] = os.path.sep + video['part_path']
            video['title'] = os.path.basename(f.name)
            video['is_dir'] = f.isdir
            if video['is_dir']:
                video['small_path'] = subcname + '/' + video['name']
                video['total_items'] = self.__total_items(f.name)
            else:
                # We must return the full metadata on a request for a specific
                # file, but it's not needed for info on files in a directory
                # however, if we've already got the "hard" transcode info
                # we might as well give back the full metadata.
                if len(files) == 1 or f.name in transcode.info_cache:
                    video['valid'] = transcode.supported_format(f.name)
                    if video['valid']:
                        video.update(self.metadata_full(f.name, tsn, mtime=mtime))
                        if len(files) == 1:
                            video['captureDate'] = hex(isogm(video['time']))
                else:
                    video['valid'] = True
                    video.update(metadata.basic(f.name, mtime))

                if self.use_ts(tsn, f.name):
                    video['mime'] = 'video/x-tivo-mpeg-ts'
                else:
                    video['mime'] = 'video/x-tivo-mpeg'

                video['textSize'] = metadata.human_size(f.size)

            videos.append(video)

        t = Template(XML_CONTAINER_TEMPLATE)
        t.container = handler.cname
        t.name = subcname
        t.total = total
        t.start = start
        t.videos = videos
        t.quote = quote
        t.escape = escape
        t.crc = zlib.crc32
        t.guid = config.getGUID()
        t.tivos = config.tivos
        handler.send_xml(str(t))

    def use_ts(self, tsn, file_path):
        if config.is_ts_capable(tsn):
            ext = os.path.splitext(file_path)[1].lower()
            if ext == '.tivo':
                try:
                    flag = open(file_path).read(8)
                except:
                    return False
                if ord(flag[7]) & 0x20:
                    return True
            else:
                opt = config.get_ts_flag()
                if ((opt == 'auto' and ext in LIKELYTS) or
                    (opt in ['true', 'yes', 'on'])):
                    return True

        return False

    def get_details_xml(self, tsn, file_path):
        if (tsn, file_path) in self.tvbus_cache:
            details = self.tvbus_cache[(tsn, file_path)]
        else:
            file_info = VideoDetails()
            file_info['valid'] = transcode.supported_format(file_path)
            if file_info['valid']:
                file_info.update(self.metadata_full(file_path, tsn))

            t = Template(TVBUS_TEMPLATE)
            t.video = file_info
            t.escape = escape
            t.get_tv = metadata.get_tv
            t.get_mpaa = metadata.get_mpaa
            t.get_stars = metadata.get_stars
            t.get_color = metadata.get_color
            details = str(t)
            self.tvbus_cache[(tsn, file_path)] = details
        return details

    def tivo_header(self, tsn, path, mime):
        """
        Create and return a tivo header for the given video file as bytes
        """
        def pad(length, align):
            extra = length % align
            if extra:
                extra = align - extra
            return extra

        if mime == 'video/x-tivo-mpeg-ts':
            flag = 45
        else:
            flag = 13
        details = self.get_details_xml(tsn, path).encode('utf-8')
        ld = len(details)
        chunk = details + b'\0' * (pad(ld, 4) + 4)
        lc = len(chunk)
        blocklen = lc * 2 + 40
        padding = pad(blocklen, 1024)

        return b''.join([b'TiVo',
                         struct.pack('>HHHLH', 4, flag, 0, padding + blocklen, 2),
                         struct.pack('>LLHH', lc + 12, ld, 1, 0),
                         chunk,
                         struct.pack('>LLHH', lc + 12, ld, 2, 0),
                         chunk,
                         b'\0' * padding])

    def TVBusQuery(self, handler, query):
        tsn = handler.headers.get('tsn', '')
        f = query['File'][0]
        path = self.get_local_path(handler, query)
        file_path = os.path.normpath(path + '/' + f)

        details = self.get_details_xml(tsn, file_path)

        handler.send_xml(details)

class VideoDetails(UserDict):
    """
    VideoDetails is a dictionary that contains all keys, those keys which haven't
    been explicitly set will return a default value when accessed.
    """

    defaults = { 'showingBits' : '0',
                 'displayMajorNumber' : '0',
                 'displayMinorNumber' : '0',
                 'isEpisode' : 'true',
                 'colorCode' : '4',
                 'showType' : ('SERIES', '5') }

    def __contains__(self, key):
        return True

    def __missing__(self, key):
        if key in self.defaults:
            return self.defaults[key]
        elif key.startswith('v'):
            return []
        else:
            return ''
