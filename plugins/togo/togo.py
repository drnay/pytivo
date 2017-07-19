import html
import http.cookiejar
import logging
import os
import subprocess
import sys
import time
import json
import struct
import urllib.request
import urllib.error
from urllib.parse import urlparse, urljoin, urlsplit, quote, unquote
from xml.dom import minidom
from datetime import datetime
from threading import Thread, RLock

from Cheetah.Template import Template

import pytz
import config
import metadata
from metadata import tag_data, prefix_bin_qty
from plugin import Plugin

logger = logging.getLogger('pyTivo.togo')

SCRIPTDIR = os.path.dirname(__file__)

CLASS_NAME = 'ToGo'

# Characters to remove from filenames

BADCHAR = {'\\': '-', '/': '-', ':': ' -', ';': ',', '*': '.',
           '?': '.', '!': '.', '"': "'", '<': '(', '>': ')', '|': ' '}

# Default top-level share path

DEFPATH = '/TiVoConnect?Command=QueryContainer&Container=/NowPlaying'

# Some error/status message templates

MISSING = """<h3>Missing Data</h3> <p>You must set both "tivo_mak" and
"togo_path" before using this function.</p>"""

TRANS_QUEUE = """<h3>Queued for Transfer</h3> <p>%s</p> <p>queued for
transfer to:</p> <p>%s</p>"""

TRANS_STOP = """<h3>Transfer Stopped</h3> <p>Your transfer of:</p>
<p>%s</p> <p>has been stopped.</p>"""

UNQUEUE = """<h3>Removed from Queue</h3> <p>%s</p> <p>has been removed
from the queue.</p>"""

UNABLE = """<h3>Unable to Connect to TiVo</h3> <p>pyTivo was unable to
connect to the TiVo at %s.</p> <p>This is most likely caused by an
incorrect Media Access Key. Please return to the Settings page and
double check your <b>tivo_mak</b> setting.</p> <pre>%s</pre>"""

# Preload the templates
tnname = os.path.join(SCRIPTDIR, 'templates', 'npl.tmpl')
NPL_TEMPLATE = open(tnname, 'rb').read().decode('utf-8')

tename = os.path.join(SCRIPTDIR, 'templates', 'error.tmpl')
ERROR_TEMPLATE = open(tename, 'rb').read().decode('utf-8')

mswindows = (sys.platform == "win32")

tivo_cache = {}     # Cache of TiVo NPL
json_cache = {}     # Cache of TiVo json NPL data
basic_meta = {}     # Data from NPL, parsed, indexed by program URL
details_urls = {}   # URLs for extended data, indexed by main URL

# An entry in the active_tivos dict is created with a list of recordings
# to download for each TiVo (ie the key unique to a TiVo (usually
# the IP addr)
# As it is accessed by multiple threads a lock must be obtained
# before accessing it.
active_tivos_lock = RLock()
active_tivos = {}

def null_cookie(name, value):
    return http.cookiejar.Cookie(0, name, value, None, False, '', False,
                                 False, '', False, False, None, False, None, None, None)

auth_handler = urllib.request.HTTPPasswordMgrWithDefaultRealm()
cj = http.cookiejar.CookieJar()
cj.set_cookie(null_cookie('sid', 'ADEADDA7EDEBAC1E'))
tivo_opener_lock = RLock()
tivo_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj),
                                          urllib.request.HTTPBasicAuthHandler(auth_handler),
                                          urllib.request.HTTPDigestAuthHandler(auth_handler))

togo_tsn = config.get_togo('tsn')
if togo_tsn:
    tivo_opener.addheaders.append(('TSN', togo_tsn))

def tivo_open(url):
    """
    Use the tivo_opener to open the given url, waiting and retrying if the
    server is busy.
    """
    # Loop just in case we get a server busy message
    while True:
        try:
            # Open the URL using our authentication/cookie opener
            with tivo_opener_lock:
                return tivo_opener.open(url)

        # Do a retry if the TiVo responds that the server is busy
        except urllib.error.HTTPError as e:
            if e.code == 503:
                time.sleep(5)
                continue

            # Log and throw the error otherwise
            logger.error('tivo_open(%s) raised %s: %s', url, e.__class__.__name__, e)
            raise

if mswindows:
    def PreventComputerFromSleeping(prevent=True):
        import ctypes

        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        # SetThreadExecutionState returns 0 when failed, which is ignored. The function should be supported from windows XP and up.
        if prevent:
            logger.info('PC sleep has been disabled')
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        else:
            logger.info('PC sleep has been enabled')
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)

else:
    def PreventComputerFromSleeping(prevent=True):
        # pylint: disable=unused-argument
        # No preventComputerFromSleeping for MacOS and Linux yet.
        pass

class ToGo(Plugin):
    """
    The ToGo plugin handles requests from the pyTivo webpage to display the
    now playing list (NPL) of recordings currently on a particular Tivo, and
    download those recordings to local files.

    Plugins are singleton instances providing "command" handlers. As such there
    are minimal instance variables, and most/all methods are static methods.
    """

    CONTENT_TYPE = 'text/html'

    @staticmethod
    def GetTiVoList(handler, query):
        """
        HTTP command handler to return the basic info on all tivo's known to pyTivo

        The handler's send_json called with the info in a json object:
          {
            tsn:
            {
              "name": string,       # The name given to the tivo by the owner
              "tsn": string,        # The TiVo's unique serial number
              "address": string,    # The IPv4 standard dotted-quad string representation of the TiVo
              "port": number        # The IP port to use to communicate w/ the TiVo
            }
          }
        """
        # pylint: disable=unused-argument

        json_config = {}

        for tsn in config.tivos:
            json_config[tsn] = {}
            json_config[tsn]['name'] = config.tivos[tsn]['name']
            json_config[tsn]['tsn'] = tsn
            json_config[tsn]['address'] = config.tivos[tsn]['address']
            json_config[tsn]['port'] = config.tivos[tsn]['port']

        handler.send_json(json.dumps(json_config))

    @staticmethod
    def GetShowsList(handler, query):
        """
        HTTP command handler to return the list of shows on a particular TiVo
        """
        json_config = {}

        if 'TiVo' in query:
            tivoIP = query['TiVo'][0]
            tsn = config.tivos_by_ip(tivoIP)
            attrs = config.tivos[tsn]
            tivo_name = attrs.get('name', tivoIP)
            tivo_mak = config.get_tsn('tivo_mak', tsn)

            protocol = attrs.get('protocol', 'https')
            ip_port = '%s:%d' % (tivoIP, attrs.get('port', 443))
            path = attrs.get('path', DEFPATH)
            baseurl = '%s://%s%s' % (protocol, ip_port, path)

            # Get the total item count first
            theurl = baseurl + '&Recurse=Yes&ItemCount=0'
            auth_handler.add_password('TiVo DVR', ip_port, 'tivo', tivo_mak)
            logger.debug('GetShowsList: (1) add password for TiVo DVR netloc: %s', ip_port)
            try:
                page = tivo_open(theurl)
            except IOError:
                handler.send_error(404)
                return

            xmldoc = minidom.parse(page)
            page.close()

            LastChangeDate = tag_data(xmldoc, 'TiVoContainer/Details/LastChangeDate')

            # Check date of cache
            if tsn in json_cache and json_cache[tsn]['lastChangeDate'] == LastChangeDate:
                logger.debug("Retrieving shows from cache")
                handler.send_json(json_cache[tsn]['data'])
                return

            # loop through grabbing 50 items at a time (50 is max TiVo will return)
            TotalItems = int(tag_data(xmldoc, 'TiVoContainer/Details/TotalItems'))
            if TotalItems <= 0:
                logger.debug("Total items 0")
                handler.send_json(json_config)
                return

            GotItems = 0
            GeneratedID = 0
            while GotItems < TotalItems:
                logger.debug("Retrieving shows %s-%s of %s from %s",
                             GotItems, GotItems + 50, TotalItems, tivo_name)
                theurl = baseurl + '&Recurse=Yes&ItemCount=50'
                theurl += '&AnchorOffset=%d' % GotItems
                auth_handler.add_password('TiVo DVR', ip_port, 'tivo', tivo_mak)
                logger.debug('GetShowsList: (2) add password for TiVo DVR netloc: %s', ip_port)
                try:
                    page = tivo_open(theurl)
                except IOError:
                    handler.send_error(404)
                    return

                try:
                    xmldoc = minidom.parse(page)
                    items = xmldoc.getElementsByTagName('Item')
                except Exception as e:          # pylint: disable=broad-except
                    logger.error('XML parser error: %s: %s', e.__class__.__name__, e)
                    break
                finally:
                    page.close()

                if len(items) <= 0:
                    logger.debug("items collection empty")
                    break

                for item in items:
                    SeriesID = tag_data(item, 'Details/SeriesId')
                    if not SeriesID:
                        SeriesID = 'TS%08d' % GeneratedID
                        GeneratedID += 1

                    if not SeriesID in json_config:
                        json_config[SeriesID] = {}

                    EpisodeID = tag_data(item, 'Details/ProgramId')
                    if not EpisodeID:
                        EpisodeID = 'EP%08d' % GeneratedID
                        GeneratedID += 1

                    # Check for duplicate episode IDs and replace with generated ID
                    while EpisodeID in json_config[SeriesID]:
                        EpisodeID = 'EP%08d' % GeneratedID
                        GeneratedID += 1

                    ep_info = {'seriesID':      SeriesID,
                               'episodeID':     EpisodeID,
                               'url':           tag_data(item, 'Links/Content/Url'),
                               'title':         tag_data(item, 'Details/Title'),
                               'detailsUrl':    tag_data(item, 'Links/TiVoVideoDetails/Url'),
                               'episodeTitle':  tag_data(item, 'Details/EpisodeTitle'),
                               'description':   tag_data(item, 'Details/Description'),
                               'recordDate':    tag_data(item, 'Details/CaptureDate'),
                               'duration':      tag_data(item, 'Details/Duration'),
                               'sourceSize':    tag_data(item, 'Details/SourceSize'),
                               'channel':       tag_data(item, 'Details/SourceChannel'),
                               'stationID':     tag_data(item, 'Details/SourceStation'),
                               'inProgress':    tag_data(item, 'Details/InProgress') == 'Yes',
                               'isProtected':   tag_data(item, 'Details/CopyProtected') == 'Yes',
                               'isSuggestion':  tag_data(item, 'Links/CustomIcon/Url') == 'urn:tivo:image:suggestion-recording',
                               'icon':          'normal',
                              }

                    json_config[SeriesID][EpisodeID] = ep_info

                    # check if an icon other than normal should be used for the episode
                    custom_icon_url = tag_data(item, 'Links/CustomIcon/Url')
                    if ep_info['isProtected']:
                        ep_info['icon'] = 'protected'
                    elif custom_icon_url == 'urn:tivo:image:expires-soon-recording':
                        ep_info['icon'] = 'expiring'
                    elif custom_icon_url == 'urn:tivo:image:expired-recording':
                        ep_info['icon'] = 'expired'
                    elif custom_icon_url == 'urn:tivo:image:save-until-i-delete-recording':
                        ep_info['icon'] = 'kuid'
                    elif custom_icon_url == 'urn:tivo:image:suggestion-recording':
                        ep_info['icon'] = 'suggestion'
                    elif custom_icon_url == 'urn:tivo:image:in-progress-recording':
                        ep_info['icon'] = 'inprogress'

                    url = urljoin(baseurl, ep_info['url'])
                    ep_info['url'] = url
                    if not url in basic_meta:
                        basic_meta[url] = metadata.from_container(item)
                        if 'detailsUrl' in ep_info:
                            details_urls[url] = ep_info['detailsUrl']

                itemCount = tag_data(xmldoc, 'TiVoContainer/ItemCount')
                try:
                    logger.debug("Retrieved " + itemCount + " from " + tivo_name)
                    GotItems += int(itemCount)
                except ValueError:
                    GotItems += len(items)


            # Cache data for reuse
            json_cache[tsn] = {}
            json_cache[tsn]['data'] = json.dumps(json_config)
            json_cache[tsn]['lastChangeDate'] = LastChangeDate

            handler.send_json(json_cache[tsn]['data'])
        else:
            handler.send_json(json.dumps(json_config))

    @staticmethod
    def GetQueueList(handler, query):
        """
        HTTP command handler to return the list of recordings queued to be
        downloaded from a particular TiVo.
        Recordings currently being downloaded are considered in the queue.

        The handler's send_json called with the info in a json object:
          {
            urls: Array<string>     # list of download urls for the recordings
          }
        """
        json_config = {}
        if 'TiVo' in query:
            tivoIP = query['TiVo'][0]
            with active_tivos_lock:
                if tivoIP in active_tivos:
                    with active_tivos[tivoIP]['lock']:
                        json_config['urls'] = [ status['url'] for status in active_tivos[tivoIP]['queue'] ]

        handler.send_json(json.dumps(json_config))

    @staticmethod
    def GetTotalQueueCount(handler, query):
        """
        HTTP command handler to return the number of recordings currently
        queued to be downloaded from all known TiVos.

        The handler's send_json called with the info in a json object:
          {
            count: number       # count of all recordings currently queued
          }
        """
        # pylint: disable=unused-argument

        json_config = {}
        json_config['count'] = 0

        with active_tivos_lock:
            for tivoIP in active_tivos:
                with active_tivos[tivoIP]['lock']:
                    json_config['count'] += len(active_tivos[tivoIP]['queue'])

        handler.send_json(json.dumps(json_config))

    @staticmethod
    def GetStatus(handler, query):
        """
        HTTP command handler to return the status of a given queued recording
        identified by its download url.

        The handler's send_json called with the info in a json object:
          {
            state: string,          # 'queued', 'running', 'finished', 'error'
            rate: number,           # how fast the recording downloaded/is downloading ()
            size: number,           # size of the recording (bytes)
            retry: number,          # number of attempts that have been made to
                                    # download the recording, if > 0 all of those
                                    # attempts had errors
            error: string,          # description of the error, if any, from last download attempt
            maxRetries: number,     # maximum number of retries before giving up
            errorCount: number      # number of errors found in the best previous attempt
          }
        """
        json_config = {}

        lock = None
        if 'Url' in query:
            url = query['Url'][0]
            status, lock = ToGo.get_status(url)

        if not lock:
            # no Url or no status found for url
            handler.send_json(json.dumps(json_config))
            return

        with lock:
            state = 'queued'
            if status['running']:
                state = 'running'
            elif status['finished']:
                if status['error'] == '':
                    state = 'finished'
                else:
                    state = 'error'
                    json_config['error'] = status['error']

            json_config['state'] = state
            json_config['rate'] = status['rate']
            json_config['size'] = status['size']
            json_config['retry'] = status['retry']
            json_config['maxRetries'] = status['ts_max_retries']
            json_config['errorCount'] = status['ts_error_count']

        handler.send_json(json.dumps(json_config))

    @staticmethod
    def get_status(url):
        """
        get the status and lock for a given download url found in one of the
        active_tivos queues.
        """

        with active_tivos_lock:
            for tivo_tasks in active_tivos:
                with tivo_tasks['lock']:
                    for status in tivo_tasks['queue']:
                        if status['url'] == url:
                            return status, tivo_tasks['lock']

        return None, None

    @staticmethod
    def get_urlstatus(tivoIP):
        """
        get a dictionary of a copy of all active tivo download statuses, indexed
        by url for the given tivoIP, or all active tivos if no tivoIP is given.
        """

        urlstatus = {}

        def copy_queue(tivo_tasks):
            with tivo_tasks['lock']:
                q_pos = 0
                for status in tivo_tasks['queue']:
                    urlstatus[status['url']] = status.copy()
                    urlstatus[status['url']]['q_pos'] = q_pos
                    q_pos += 1

        with active_tivos_lock:
            if tivoIP and tivoIP in active_tivos:
                copy_queue(active_tivos[tivoIP])
            else:
                for tivo_tasks in active_tivos:
                    copy_queue(tivo_tasks)

        return urlstatus

    @staticmethod
    def NPL(handler, query):
        """
        ToGo.NPL returns an html page displaying the now playing list (NPL)
        from a particular TiVo device.
        The query may specify:
        - TiVo: the IPv4 address of the TiVo whose NPL is to be retrieved
        - ItemCount: the number of shows/folders to put on the page (default: 50, max: 50)
        - AnchorItem: the url identifying the 1st item in the retrieved list (default 1st item in folder)
        - AnchorOffset: the offset from the AnchorItem to start the retrieval from (default 0)
        - SortOrder:
        - Recurse:
        """

        def getint(thing):
            try:
                result = int(thing)
            except:                             # pylint: disable=bare-except
                result = 0
            return result


        shows_per_page = 50 # Change this to alter the number of shows returned (max is 50)
        if 'ItemCount' in query:
            shows_per_page = int(query['ItemCount'][0])

        if shows_per_page > 50:
            shows_per_page = 50

        folder = ''
        FirstAnchor = ''
        has_tivodecode = bool(config.get_bin('tivodecode'))
        has_tivolibre = bool(config.get_bin('tivolibre'))

        if 'TiVo' in query:
            tivoIP = query['TiVo'][0]
            try:
                tsn = config.tivos_by_ip(tivoIP)
                attrs = config.tivos[tsn]
                tivo_name = attrs.get('name', tivoIP)
                tivo_mak = config.get_tsn('tivo_mak', tsn)
            except config.Error as e:
                logger.error('NPL: %s', e)
                t = Template(ERROR_TEMPLATE)
                t.e = e
                t.additional_info = 'Your browser may have cached an old page'
                handler.send_html(str(t))
                return

            protocol = attrs.get('protocol', 'https')
            ip_port = '%s:%d' % (tivoIP, attrs.get('port', 443))
            path = attrs.get('path', DEFPATH)
            baseurl = '%s://%s%s' % (protocol, ip_port, path)
            theurl = baseurl
            if 'Folder' in query:
                folder = query['Folder'][0]
                theurl = urljoin(theurl, folder)
            theurl += '&ItemCount=%d' % shows_per_page
            if 'AnchorItem' in query:
                theurl += '&AnchorItem=' + quote(query['AnchorItem'][0])
            if 'AnchorOffset' in query:
                theurl += '&AnchorOffset=' + query['AnchorOffset'][0]
            if 'SortOrder' in query:
                theurl += '&SortOrder=' + query['SortOrder'][0]
            if 'Recurse' in query:
                theurl += '&Recurse=' + query['Recurse'][0]

            if (theurl not in tivo_cache or
                    (time.time() - tivo_cache[theurl]['thepage_time']) >= 60):
                # if page is not cached or old then retrieve it
                auth_handler.add_password('TiVo DVR', ip_port, 'tivo', tivo_mak)
                logger.debug('NPL: (1) add password for TiVo DVR netloc: %s', ip_port)
                try:
                    logger.debug("NPL.theurl: %s", theurl)
                    with tivo_open(theurl) as page:
                        tivo_cache[theurl] = {'thepage': minidom.parse(page),
                                              'thepage_time': time.time()}
                except IOError as e:
                    handler.redir(UNABLE % (tivoIP, html.escape(str(e))), 10)
                    return

            xmldoc = tivo_cache[theurl]['thepage']
            items = xmldoc.getElementsByTagName('Item')

            TotalItems = tag_data(xmldoc, 'TiVoContainer/Details/TotalItems')
            ItemStart = tag_data(xmldoc, 'TiVoContainer/ItemStart')
            ItemCount = tag_data(xmldoc, 'TiVoContainer/ItemCount')
            title = tag_data(xmldoc, 'TiVoContainer/Details/Title')
            if items:
                FirstAnchor = tag_data(items[0], 'Links/Content/Url')

            data = []
            for item in items:
                entry = {}
                for tag in ('CopyProtected', 'ContentType'):
                    value = tag_data(item, 'Details/' + tag)
                    if value:
                        entry[tag] = value
                if entry['ContentType'].startswith('x-tivo-container'):
                    entry['Url'] = tag_data(item, 'Links/Content/Url')
                    entry['Title'] = tag_data(item, 'Details/Title')
                    entry['TotalItems'] = tag_data(item, 'Details/TotalItems')
                    lc = tag_data(item, 'Details/LastCaptureDate')
                    if not lc:
                        lc = tag_data(item, 'Details/LastChangeDate')
                    entry['LastChangeDate'] = time.strftime('%b %d, %Y',
                                                            time.localtime(int(lc, 16)))
                else:
                    keys = {'Icon':         'Links/CustomIcon/Url',
                            'Url':          'Links/Content/Url',
                            'Details':      'Links/TiVoVideoDetails/Url',
                            'SourceSize':   'Details/SourceSize',
                            'Duration':     'Details/Duration',
                            'CaptureDate':  'Details/CaptureDate'}
                    for key in keys:
                        value = tag_data(item, keys[key])
                        if value:
                            entry[key] = value

                    if 'SourceSize' in entry:
                        rawsize = entry['SourceSize']
                        entry['SourceSize'] = metadata.human_size(rawsize)

                    if 'Duration' in entry:
                        dur = getint(entry['Duration']) // 1000
                        entry['Duration'] = ('%d:%02d:%02d' %
                                             (dur // 3600, (dur % 3600) // 60, dur % 60))

                    if 'CaptureDate' in entry:
                        entry['CaptureDate'] = time.strftime('%b %d, %Y',
                                                             time.localtime(int(entry['CaptureDate'], 16)))

                    url = urljoin(baseurl, entry['Url'])
                    entry['Url'] = url
                    if url in basic_meta:
                        entry.update(basic_meta[url])
                    else:
                        basic_data = metadata.from_container(item)
                        entry.update(basic_data)
                        basic_meta[url] = basic_data
                        if 'Details' in entry:
                            details_urls[url] = entry['Details']

                data.append(entry)
        else:
            data = []
            tivoIP = ''
            TotalItems = 0
            ItemStart = 0
            ItemCount = 0
            title = ''
            tsn = ''
            tivo_name = ''

        t = Template(NPL_TEMPLATE)
        t.quote = quote
        t.folder = folder
        t.urlstatus = ToGo.get_urlstatus(tivoIP)
        t.has_tivodecode = has_tivodecode
        t.has_tivolibre = has_tivolibre
        t.togo_mpegts = config.is_ts_capable(tsn)
        t.tname = tivo_name
        t.tivoIP = tivoIP
        t.container = handler.cname
        t.data = data
        t.len = len
        t.TotalItems = getint(TotalItems)
        t.ItemStart = getint(ItemStart)
        t.ItemCount = getint(ItemCount)
        t.FirstAnchor = quote(FirstAnchor)
        t.shows_per_page = shows_per_page
        t.title = title
        handler.send_html(str(t), refresh='300')


    @staticmethod
    def get_out_file(status, togo_path):
        """
        Get the full file path for the tivo recording to be downloaded (status['url'].
        The returned path will be to a non existent file.
        """

        url = status['url']
        decode = status['decode']
        ts_format = status['ts_format']
        sortable = status['sortable']

        # Use TiVo Desktop style naming
        if url in basic_meta:
            if 'title' in basic_meta[url]:
                title = basic_meta[url]['title']

                episodeTitle = ''
                if 'episodeTitle' in basic_meta[url]:
                    episodeTitle = basic_meta[url]['episodeTitle']

                recordDate = datetime.now()
                if 'recordDate' in basic_meta[url]:
                    recordDate = datetime.fromtimestamp(int(basic_meta[url]['recordDate'], 0), pytz.utc)

                callsign = ''
                if 'callsign' in basic_meta[url]:
                    callsign = basic_meta[url]['callsign']

                fileParts = {'title':           title,
                             'recordDate':      recordDate,
                             'episodeTitle':    " - ''{}''".format(episodeTitle) if episodeTitle else '',
                             'callsign':        ', {}'.format(callsign) if callsign else '',
                             'tivo_stream_type': '',
                            }

                if decode:
                    fileExt = '.ts' if ts_format else '.ps'
                else:
                    fileExt = '.tivo'
                    fileParts['tivo_stream_type'] = ' (TS)' if ts_format else ' (PS)'

                fnFmt = "{title}{episodeTitle} (Recorded {recordDate:%b %d, %Y}{callsign}){tivo_stream_type}"
                if sortable:
                    fnFmt = "{title} - {recordDate:%Y-%m-%d}{episodeTitle}{callSign}{tivo_stream_type}"
                    fileParts['callsign'] = ' ({})'.format(callsign) if callsign else ''

                fileName = fnFmt.format(**fileParts)

                for ch in BADCHAR:
                    fileName = fileName.replace(ch, BADCHAR[ch])

                count = 1
                fullName = [fileName, '', fileExt]
                while True:
                    filePath = os.path.join(togo_path, ''.join(fullName))
                    if not os.path.isfile(filePath):
                        break
                    count += 1
                    fullName[1] = ' ({})'.format(count)

                return filePath

            # If we get here then use old style naming
            parse_url = urlparse(url)

            name = unquote(parse_url[2]).split('/')[-1].split('.')
            try:
                tivo_item_id = unquote(parse_url[4]).split('id=')[1]
                name.insert(-1, ' - ' + tivo_item_id)
            except:                             # pylint: disable=bare-except
                pass
            if decode:
                if ts_format:
                    name[-1] = 'ts'
                else:
                    name[-1] = 'mpg'
            else:
                if ts_format:
                    name.insert(-1, ' (TS)')
                else:
                    name.insert(-1, ' (PS)')

            nameHold = name
            name.insert(-1, '.')

            count = 2
            newName = name
            while os.path.isfile(os.path.join(togo_path, ''.join(newName))):
                newName = nameHold
                newName.insert(-1, ' (%d)' % count)
                newName.insert(-1, '.')
                count += 1

            name = newName
            name = ''.join(name)
            for ch in BADCHAR:
                name = name.replace(ch, BADCHAR[ch])

            return os.path.join(togo_path, name)


    @staticmethod
    def get_1st_queued_file(tivo_tasks):
        """
        Download the first entry in the tivo tasks queue
        """
        tivo_name = tivo_tasks['tivo_name']
        mak = tivo_tasks['mak']
        togo_path = tivo_tasks['dest_path']
        ts_error_mode = tivo_tasks['ts_error_mode']
        ts_max_retries = tivo_tasks['ts_max_retries']

        # TODO: These 2 variables shouldn't change, so we should get them earlier, possibly
        # in the config module once so we can not log the warning when it doesn't exist
        # so many times, and then add it to the tivo_tasks dict -mjl

        # prefer tivolibre to tivodecode
        decoder_path = config.get_bin('tivolibre')
        decoder_is_tivolibre = True
        if not decoder_path:
            decoder_path = config.get_bin('tivodecode')
            decoder_is_tivolibre = False
        has_decoder = bool(decoder_path)

        lock = tivo_tasks['lock']
        with lock:
            status = tivo_tasks['queue'][0]
            ts_format = status['ts_format']
            url = status['url']
            dnld_url = url + ('&Format=video/x-tivo-mpeg-ts' if ts_format else '')
            decode = status['decode'] and has_decoder
            save_txt = status['save']
            status.update({'running': True, 'queued': False})
            outfile = ToGo.get_out_file(status, togo_path)
            status['outfile'] = outfile

        try:
            handle = tivo_open(dnld_url)
        except ConnectionResetError as e:
            with lock:
                status['running'] = False
                status['error'] = str(e)
            return
        except Exception as e:                  # pylint: disable=broad-except
            logger.error('get_1st_queued_file: tivo_open(%s) raised %s: %s', dnld_url, e.__class__.__name__, e)
            with lock:
                status['running'] = False
                status['error'] = str(e)
            return

        if decode:
            tcmd = [decoder_path, '-m', mak, '-o', outfile]
            if not decoder_is_tivolibre:
                tcmd += '-'

            tivodecode = subprocess.Popen(tcmd, stdin=subprocess.PIPE,
                                          bufsize=(512 * 1024))
            f = tivodecode.stdin
        else:
            f = open(outfile, 'wb')



        bytes_read = 0              # bytes read from download http connection
        bytes_written = 0           # bytes written to file or tivo decoder
        start_time = time.time()
        retry_download = False
        sync_loss = False

        logger.info('[{timestamp:%d/%b/%Y %H:%M:%S}] Start getting "{fname}" from {tivo_name}'
                    .format(timestamp=datetime.fromtimestamp(start_time),
                            fname=outfile, tivo_name=tivo_name))

        with handle, f:
            try:
                # Download just the header first so remaining bytes are packet aligned for TS
                tivo_header = handle.read(16)
                bytes_read += len(tivo_header)
                f.write(tivo_header)
                bytes_written += len(tivo_header)

                tivo_header_size = struct.unpack_from('>L', tivo_header, 10)[0]
                output = handle.read(tivo_header_size - 16)
                bytes_read += len(output)
                f.write(output)
                bytes_written += len(output)

                last_interval_start = start_time
                last_interval_read = bytes_read
                while True:
                    output = handle.read(524144) # Size needs to be divisible by 188
                    bytes_read += len(output)
                    last_interval_read += len(output)

                    if not output:
                        break

                    if ts_format:
                        cur_byte = 0
                        sync_loss_start = -1
                        while cur_byte < len(output):
                            if output[cur_byte] != 0x47:
                                sync_loss = True
                                with lock:
                                    status['ts_error_count'] += 1

                                if sync_loss_start == -1:
                                    sync_loss_start = cur_byte
                            else:
                                if sync_loss_start != -1:
                                    logger.info('TS sync loss detected: %s bytes at offset %s - %s',
                                                cur_byte - sync_loss_start,
                                                bytes_written + sync_loss_start,
                                                bytes_written + cur_byte)
                                    sync_loss_start = -1

                            cur_byte += 188

                        if sync_loss and ts_error_mode != 'ignore':
                            with lock:
                                # we found errors and we don't want to ignore them so
                                # if we have retries left schedule a retry
                                if status['retry'] < ts_max_retries:
                                    retry_download = True

                                # if we are keeping the best download of all attempts
                                # and we've already got more errors than a previous try
                                # abort this download and move on to the next attempt
                                if ts_error_mode == 'best':
                                    if status['retry'] > 0:
                                        if status['ts_error_count'] >= status['best_error_count']:
                                            status['running'] = False
                                            status['error'] = ('TS sync error. Best({}) < Current({})'
                                                               .format(status['best_error_count'], status['ts_error_count']))
                                            break

                                # if we don't want to keep a download with any errors
                                # abort now (we'll try again if there were tries left)
                                elif ts_error_mode == 'reject':
                                    status['running'] = False
                                    status['error'] = 'TS sync error. Mode: reject'
                                    break

                    f.write(output)
                    bytes_written += len(output)
                    now = time.time()
                    elapsed = now - last_interval_start
                    if elapsed >= 1:
                        with lock:
                            status['rate'] = (last_interval_read * 8.0) / elapsed
                            status['size'] += last_interval_read
                        last_interval_read = 0
                        last_interval_start = now

                if status['running']:
                    if not sync_loss:
                        status['error'] = ''

            except Exception as e:              # pylint: disable=broad-except
                with lock:
                    status['error'] = 'Error downloading file'
                    status['running'] = False
                    # If we've got retries left (even though this is aborting
                    # due to an exception let's try again
                    if status['retry'] < ts_max_retries:
                        retry_download = True
                logger.error('ToGo.get_1st_queued_file(%s) raised %s: %s\n\tr:%s; w:%s; retry: %s',
                             dnld_url, e.__class__.__name__, e, format(bytes_read, ',d'),
                             format(bytes_written, ',d'), 'yes' if retry_download else 'no')

        end_time = time.time()
        elapsed = (end_time - start_time) if end_time >= start_time + 1 else 1
        rate = (bytes_read * 8.0) / elapsed
        size = bytes_read

        # if we were decoding wait for the decode subprocess to exit
        if decode:
            while tivodecode.poll() is None:
                time.sleep(1)

        # if we read and wrote the entire download file
        if not output:
            with lock:
                status['running'] = False
                status['rate'] = rate
                status['size'] = size
                best_file = status['best_file']

            logger.info('[{timestamp:%d/%b/%Y %H:%M:%S}] Done getting "{fname}" from {tivo_name}, '
                        '{mbps[0]:.2f} {mbps[1]}B/s ({num_bytes[0]:.3f} {num_bytes[1]}Bytes / {seconds:.0f} s)'
                        .format(timestamp=datetime.fromtimestamp(end_time),
                                fname=outfile, tivo_name=tivo_name,
                                num_bytes=prefix_bin_qty(size),
                                mbps=prefix_bin_qty(rate),
                                seconds=elapsed))

            if ts_error_mode == 'best' and os.path.isfile(best_file):
                os.remove(best_file)
                if os.path.isfile(best_file + '.txt'):
                    os.remove(best_file + '.txt')

            if sync_loss:
                outfile_name = outfile.split('.')
                # Add errors and attempt number to the output file name
                outfile_name[-1:-1] = [' (^{}_{})'.format(status['ts_error_count'], status['retry']), '.']
                new_outfile = ''.join(outfile_name)

                # if the new filename exists, append a count until an unused name is found
                if os.path.isfile(new_outfile):
                    count = 2
                    outfile_name[-2:-2] = [' ({})'.format(count)]

                    while os.path.isfile(new_outfile):
                        count += 1
                        outfile_name[-2:-1] = [' ({})'.format(count)]
                        new_outfile = ''.join(outfile_name)

                os.rename(outfile, new_outfile)
                outfile = new_outfile

            if save_txt and os.path.isfile(outfile):
                meta = basic_meta[url]
                try:
                    handle = tivo_open(details_urls[url])
                    meta.update(metadata.from_details(handle.read()))
                    handle.close()
                except:                         # pylint: disable=bare-except
                    pass
                metafile = open(outfile + '.txt', 'w')
                metadata.dump(metafile, meta)
                metafile.close()

            with lock:
                status['best_file'] = outfile
                status['best_error_count'] = status['ts_error_count']

        else:
            # aborted download
            os.remove(outfile)
            with lock:
                logger.info('[%s] Aborted transfer (%s) of "%s" from %s',
                            time.strftime('%d/%b/%Y %H:%M:%S'), status['error'], outfile, tivo_name)

        if not retry_download:
            with lock:
                status['finished'] = True
        else:
            logger.debug('get_1st_queued_file: retrying download, adding back to the queue')
            with lock:
                retry_status = status.copy()
            retry_status.update({'rate': 0,
                                 'size': 0,
                                 'queued': True,
                                 'retry': retry_status['retry'] + 1,
                                 'ts_error_count': 0})

            logger.info('Transfer error detected, retrying download (%d/%d)',
                        retry_status['retry'], ts_max_retries)
            with lock:
                tivo_tasks['queue'][1:1] = [retry_status]


    @staticmethod
    def process_queue(tivoIP):
        PreventComputerFromSleeping(True)

        logger.debug('process_queue(%s) entered.', tivoIP)

        with active_tivos_lock:
            tivo_tasks = active_tivos[tivoIP]

        while True:
            tivo_tasks['lock'].acquire()
            if tivo_tasks['queue']:
                tivo_tasks['lock'].release()
            else:
                logger.debug('process_queue: queue is empty for %s', tivoIP)
                # Complicated but... before we delete the tivo from the
                # list of active tivos we need to release the tasks lock
                # in case someone else is waiting to add an entry and
                # then we can acquire the active tivo lock and the tasks
                # lock in the correct order (so we don't deadlock), double
                # check than no tasks were added while we didn't have the
                # lock, and only then delete the tivo from the active list.
                tivo_tasks['lock'].release()
                with active_tivos_lock:
                    with tivo_tasks['lock']:
                        if not tivo_tasks['queue']:
                            del active_tivos[tivoIP]
                    break

            ToGo.get_1st_queued_file(tivo_tasks)
            with tivo_tasks['lock']:
                logger.debug('process_queue: %s removing 1st queue entry of %d', tivoIP, len(tivo_tasks['queue']))
                tivo_tasks['queue'].pop(0)

            with active_tivos_lock:
                if not active_tivos:
                    PreventComputerFromSleeping(False)

    @staticmethod
    def ToGo(handler, query):
        """
        HTTP command handler to download a set of recordings from a Tivo.

        If there is already a thread downloading recordings from that Tivo,
        the new recordings will be appended to the existing download task
        list for that Tivo, otherwise a new task list will be created and
        a thread spawned to process it.
        """
        togo_path = config.get_togo('path')
        for name, data in config.getShares():
            if togo_path == name:
                togo_path = data.get('path')
        if togo_path:
            tivoIP = query['TiVo'][0]
            tsn = config.tivos_by_ip(tivoIP)
            tivo_name = config.tivos[tsn].get('name', tivoIP)
            tivo_mak = config.get_tsn('tivo_mak', tsn)
            urls = query.get('Url', [])
            decode = 'decode' in query
            save = 'save' in query
            ts_format = 'ts_format' in query and config.is_ts_capable(tsn)
            sortable = bool(config.get_togo('sortable_names', False))
            for theurl in urls:

                status = {'url': theurl,
                          'running': False,
                          'queued': True,
                          'finished': False,
                          'decode': decode,         # decode the downloaded tivo file
                          'save': save,             # save the tivo file's metadata to a .txt file
                          'ts_format': ts_format,   # download using transport stream otherwise program stream
                          'sortable': sortable,     # name saved tivo file in a sortable manner
                          'error': '',
                          'rate': 0,
                          'size': 0,
                          'retry': 0,
                          'ts_error_count': 0,
                          'best_file': '',
                          'best_error_count': 0}

                with active_tivos_lock:
                    if tivoIP in active_tivos:
                        with active_tivos[tivoIP]['lock']:
                            active_tivos[tivoIP]['queue'].append(status)
                    else:
                        # TODO: This might be better - mjl
                        #active_tivos[tivoIP] = TivoDownload(tivoIP, status, tivo_mak, togo_path)
                        #active_tivos[tivoIP].start()

                        # we have to add authentication info again because the
                        # download netloc may be different from that used to
                        # retrieve the list of recordings (and in fact the port
                        # is different, 443 to get the NPL and 80 for downloading).
                        auth_handler.add_password('TiVo DVR', urlsplit(theurl).netloc, 'tivo', tivo_mak)
                        logger.debug('ToGo: add password for TiVo DVR netloc: %s', urlsplit(theurl).netloc)

                        active_tivos[tivoIP] = {'tivoIP': tivoIP,
                                                'lock': RLock(),
                                                'thread': Thread(target=ToGo.process_queue,
                                                                 args=(tivoIP,)),
                                                'tivo_name': tivo_name,
                                                'mak': tivo_mak,
                                                'dest_path': togo_path,
                                                'ts_error_mode': config.get_togo('ts_error_mode', 'ignore'),
                                                'ts_max_retries': int(config.get_togo('ts_max_retries', 0)),
                                                'queue': [status]}

                        active_tivos[tivoIP]['thread'].start()

                logger.info('[%s] Queued "%s" for transfer to %s',
                            time.strftime('%d/%b/%Y %H:%M:%S'),
                            unquote(theurl), togo_path)
            urlstring = '<br>'.join([unquote(x) for x in urls])
            message = TRANS_QUEUE % (urlstring, togo_path)
        else:
            message = MISSING
        handler.redir(message, 5)

    @staticmethod
    def ToGoStop(handler, query):
        """
        TODO: If this was supposed to abort a recording currently downloading
        it will no longer do that as the 'running' status flag is not checked
        while downloading. But I'm not really sure what this was supposed to
        accomplish. -mjl 7/13/2017
        """
        theurl = ''
        if 'Url' in query:
            theurl = query['Url'][0]
            status, lock = ToGo.get_status(theurl)
            if status:
                with lock:
                    status['running'] = False

        handler.redir(TRANS_STOP % unquote(theurl))

    @staticmethod
    def remove_from_queue(url, tivoIP):
        with active_tivos_lock:
            if tivoIP in active_tivos:
                with active_tivos[tivoIP]['lock']:
                    queue = active_tivos[tivoIP]['queue']
                    q_pos = 0
                    for status in queue:
                        if status['url'] == url:
                            break
                        q_pos += 1

                    if queue[q_pos]['running']:
                        logger.info('Can\'t remove running "%s" from queue', unquote(url))
                    else:
                        del queue[q_pos]
                        logger.info('Removed "%s" from queue', unquote(url))


    @staticmethod
    def Unqueue(handler, query):
        theurl = ''

        if 'Url' in query:
            theurl = query['Url'][0]
            if 'TiVo' in query:
                tivoIP = query['TiVo'][0]

                ToGo.remove_from_queue(theurl, tivoIP)

        handler.redir(UNQUEUE % unquote(theurl))


    @staticmethod
    def UnqueueAll(handler, query):
        # pylint: disable=unused-argument

        with active_tivos_lock:
            for tivoIP in active_tivos:
                with active_tivos[tivoIP].lock:
                    for url in active_tivos[tivoIP].queue:
                        ToGo.remove_from_queue(url, tivoIP)
