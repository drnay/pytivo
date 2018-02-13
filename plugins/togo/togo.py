import html
import http.cookiejar
import logging
import os
import sys
import time
import json
import urllib.request
import urllib.error
from urllib.parse import urljoin, urlsplit, quote, unquote
from xml.dom import minidom
from threading import RLock

from Cheetah.Template import Template

import config
import metadata
from metadata import tag_data
from plugin import Plugin
from showinfo import ShowInfo
from .tivodownload import TivoDownload

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

# All the information gathered about a particular show (a ShowInfo instance)
# indexed by the show's download url
showinfo = {}

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
                    dnld_url = tag_data(item, 'Links/Content/Url')
                    # the tivo download url seems to always be absolute, so is this necessary?
                    # I'm commenting it out -mjl 7/23/2017
                    #dnld_url = urljoin(baseurl, dnld_url)
                    if not dnld_url in showinfo:
                        showinfo[dnld_url] = ShowInfo().from_tivo_container_item(item)
                    item_showinfo = showinfo[dnld_url]
                    ep_info = item_showinfo.get_tivo_desktop_info()

                    if not ep_info['seriesID']:
                        ep_info['seriesID'] = 'TS%08d' % GeneratedID
                        GeneratedID += 1

                    if not ep_info['episodeID']:
                        ep_info['episodeID'] = 'EP%08d' % GeneratedID
                        GeneratedID += 1

                    if not ep_info['seriesID'] in json_config:
                        json_config[ep_info['seriesID']] = {}

                    # Check for duplicate episode IDs and replace with generated ID
                    while ep_info['episodeID'] in json_config[ep_info['seriesID']]:
                        ep_info['episodeID'] = 'EP%08d' % GeneratedID
                        GeneratedID += 1

                    json_config[ep_info['seriesID']][ep_info['episodeID']] = ep_info

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
            if tivoIP:
                if tivoIP in active_tivos:
                    copy_queue(active_tivos[tivoIP])
            else:
                for activeTivoIP in active_tivos:
                    copy_queue(active_tivos[activeTivoIP])

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

                    dnld_url = entry['Url']
                    # the tivo download url seems to always be absolute, so is this necessary?
                    # I'm commenting it out -mjl 7/23/2017
                    #dnld_url = urljoin(baseurl, dnld_url)
                    if not dnld_url in showinfo:
                        showinfo[dnld_url] = ShowInfo()
                        showinfo[dnld_url].from_tivo_container_item(item)

                    entry.update(showinfo[dnld_url].get_old_basicmeta())

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
                          'showinfo': showinfo[theurl], # metadata information about the show
                          'decode': decode,         # decode the downloaded tivo file
                          'save': save,             # save the tivo file's metadata to a .txt file
                          'ts_format': ts_format,   # download using transport stream otherwise program stream
                          'sortable': sortable,     # name saved tivo file in a sortable manner
                          'error': '',
                          'rate': 0,
                          'size': 0,
                          'retry': 0,
                          'download_attempts': [],  # information about each download attempt (used for sync error log)
                          'ts_error_packets': [],   # list of TS packets w/ sync lost as tuples (packet_no, count)
                          'best_attempt_index': None, # index into download_attempts of the attempt w/ fewest errors
                          'best_file': '',
                          'best_error_count': None} # count of TS packets lost (sync byte was wrong) in 'best_file'

                with active_tivos_lock:
                    if tivoIP in active_tivos:
                        with active_tivos[tivoIP]['lock']:
                            active_tivos[tivoIP]['queue'].append(status)
                    else:
                        # we have to add authentication info again because the
                        # download netloc may be different from that used to
                        # retrieve the list of recordings (and in fact the port
                        # is different, 443 to get the NPL and 80 for downloading).
                        auth_handler.add_password('TiVo DVR', urlsplit(theurl).netloc, 'tivo', tivo_mak)
                        logger.debug('ToGo: add password for TiVo DVR netloc: %s', urlsplit(theurl).netloc)

                        active_tivos[tivoIP] = {'tivoIP': tivoIP,
                                                'lock': RLock(),
                                                'thread': None,
                                                'tivo_name': tivo_name,
                                                'mak': tivo_mak,
                                                'dest_path': togo_path,
                                                'fn_format_info': {'episode': config.get_togo('episode_fn'),
                                                                   'movie': config.get_togo('movie_fn')
                                                                  },
                                                'ts_error_mode': config.get_togo('ts_error_mode', 'ignore'),
                                                'ts_max_retries': int(config.get_togo('ts_max_retries', 0)),
                                                'queue': [status]}

                        active_tivos[tivoIP]['thread'] = TivoDownload(tivoIP, active_tivos, active_tivos_lock, tivo_open)
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
