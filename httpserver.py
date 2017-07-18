import http.server
import socketserver
import cgi
import gzip
import logging
import mimetypes
import os
import shutil
import socket
from io import BytesIO
from email.utils import formatdate
from urllib.parse import unquote_plus, quote, parse_qs
from xml.sax.saxutils import escape

from Cheetah.Template import Template
import config
from plugin import GetPlugin

SCRIPTDIR = os.path.dirname(__file__)

SERVER_INFO = """<?xml version="1.0" encoding="utf-8"?>
<TiVoServer>
<Version>2.1.0</Version>
<InternalName>py3Tivo</InternalName>
<InternalVersion>2.1.0</InternalVersion>
<Organization>pyTivo Developers</Organization>
<Comment>http://pytivo.sf.net/</Comment>
</TiVoServer>"""

VIDEO_FORMATS = """<?xml version="1.0" encoding="utf-8"?>
<TiVoFormats>
<Format><ContentType>video/x-tivo-mpeg</ContentType><Description/></Format>
</TiVoFormats>"""

VIDEO_FORMATS_TS = """<?xml version="1.0" encoding="utf-8"?>
<TiVoFormats>
<Format><ContentType>video/x-tivo-mpeg</ContentType><Description/></Format>
<Format><ContentType>video/x-tivo-mpeg-ts</ContentType><Description/></Format>
</TiVoFormats>"""

BASE_HTML = """<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01//EN"
"http://www.w3.org/TR/html4/strict.dtd">
<html> <head><title>py3Tivo</title>
<link rel="stylesheet" type="text/css" href="/main.css">
</head> <body> %s </body> </html>"""

RELOAD = '<p>The <a href="%s">page</a> will reload in %d seconds.</p>'
UNSUP = '<h3>Unsupported Command</h3> <p>Query:</p> <ul>%s</ul>'

class TivoHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    def __init__(self, server_address, RequestHandlerClass):
        self.containers = {}
        self.beacon = None
        self.in_service = None
        self.stop = False
        self.restart = False
        self.logger = logging.getLogger('pyTivo')
        http.server.HTTPServer.__init__(self, server_address,
                                        RequestHandlerClass)
        self.daemon_threads = True

    def add_container(self, name, settings):
        if name in self.containers or name == 'TiVoConnect':
            raise Exception("Container Name in use")
        try:
            self.containers[name] = settings
        except KeyError:
            self.logger.error('Unable to add container ' + name)

    def reset(self):
        self.containers.clear()
        for section, settings in config.getShares():
            self.add_container(section, settings)

    def handle_error(self, request, client_address):
        self.logger.exception('Exception during request from %s',
                              client_address)

    def set_beacon(self, beacon):
        self.beacon = beacon

    def set_service_status(self, status):
        self.in_service = status

class TivoHTTPHandler(http.server.BaseHTTPRequestHandler):
    def __init__(self, request, client_address, server):
        self.wbufsize = 0x10000
        self.server_version = 'pyTivo/1.0'
        self.protocol_version = 'HTTP/1.1'
        self.sys_version = ''
        self.container = None
        self.cname = None

        try:
            http.server.BaseHTTPRequestHandler.__init__(self, request,
                                                        client_address, server)
        except:
            server.logger.exception('Exception initializing the BaseHTTPRequestHandler')

    def setup(self):
        """
        Called before the handle() method to perform any initialization actions required.
        see https://docs.python.org/3/library/socketserver.html
        """
        http.server.BaseHTTPRequestHandler.setup(self)

        # This allows pyTivo to die when user selects Stop Transfer on the TiVo
        # (If no request is received within timeout seconds, handle_timeout() will be called,
        # see https://docs.python.org/3/library/socketserver.html#socketserver.BaseServer.handle_request
        # also note that a "Request timed out:" info message will be logged.)
        self.request.settimeout(180)


    def address_port_string(self):
        host, port = self.client_address[:2]
        return "{}:{}".format(host, port)

    def version_string(self):
        """ Override version_string() so it doesn't include the Python
            version.

        """
        return self.server_version

    def do_GET(self):
        tsn = self.headers.get('TiVo_TCD_ID',
                               self.headers.get('tsn', ''))
        if not self.authorize(tsn):
            return

        if tsn and (not config.tivos_found or tsn in config.tivos):
            attr = config.tivos.get(tsn, {})
            updated_tivo = False
            if 'address' not in attr:
                attr['address'] = self.address_string()
                updated_tivo = True
            if 'name' not in attr:
                attr['name'] = self.server.beacon.get_name(attr['address'])
                updated_tivo = True
            config.tivos[tsn] = attr
            if updated_tivo:
                self.server.logger.info('TiVo identified from request: %s %s',
                                        attr['address'], attr['name'])

        if '?' in self.path:
            path, opts = self.path.split('?', 1)
            query = parse_qs(opts)
        else:
            path = self.path
            query = {}

        if path == '/TiVoConnect':
            self.handle_query(query, tsn)
        else:
            ## Get File
            splitpath = [x for x in unquote_plus(path).split('/') if x]
            if splitpath:
                self.handle_file(query, splitpath)
            else:
                ## Not a file not a TiVo command
                self.infopage()

    def do_POST(self):
        tsn = self.headers.get('TiVo_TCD_ID',
                               self.headers.get('tsn', ''))
        if not self.authorize(tsn):
            return
        ctype, pdict = cgi.parse_header(self.headers.get('content-type'))
        if ctype == 'multipart/form-data':
            query = cgi.parse_multipart(self.rfile, pdict)
            # I'm not sure if this code works after the python3 conversion
            # there may be some string/bytes issues. Saving settings does not
            # come through here, I'm leaving this debugging line commented
            # out for the time being -mjl 2017-06-01
            #self.server.logger.info("POST query: {}".format(query))
        else:
            length = int(self.headers.get('content-length'))
            qs = self.rfile.read(length).decode('utf-8')
            query = parse_qs(qs, keep_blank_values=1)
        self.handle_query(query, tsn)

    def do_command(self, query, command, target, tsn):
        for name, container in config.getShares(tsn):
            if target == name:
                plugin = GetPlugin(container['type'])
                if hasattr(plugin, command):
                    self.cname = name
                    self.container = container
                    method = getattr(plugin, command)
                    method(self, query)
                    return True
                else:
                    break
        return False

    def handle_query(self, query, tsn):
        if 'Command' in query and len(query['Command']) >= 1:

            command = query['Command'][0]

            # If we are looking at the root container
            if (command == 'QueryContainer' and
                    (not 'Container' in query or query['Container'][0] == '/')):
                self.root_container()
                return

            if 'Container' in query:
                # Dispatch to the container plugin
                basepath = query['Container'][0].split('/')[0]
                if self.do_command(query, command, basepath, tsn):
                    return

            elif command == 'QueryItem':
                path = query.get('Url', [''])[0]
                splitpath = [x for x in unquote_plus(path).split('/') if x]
                if splitpath and not '..' in splitpath:
                    if self.do_command(query, command, splitpath[0], tsn):
                        return

            elif (command == 'QueryFormats' and 'SourceFormat' in query and
                  query['SourceFormat'][0].startswith('video')):
                if config.is_ts_capable(tsn):
                    self.send_xml(VIDEO_FORMATS_TS)
                else:
                    self.send_xml(VIDEO_FORMATS)
                return

            elif command == 'QueryServer':
                self.send_xml(SERVER_INFO)
                return

            elif command in ('GetActiveTransferCount', 'GetTransferStatus'):
                plugin = GetPlugin('video')
                if hasattr(plugin, command):
                    method = getattr(plugin, command)
                    method(self, query)
                    return True

            elif command in ('FlushServer', 'ResetServer'):
                # Does nothing -- included for completeness
                self.send_response(200)
                self.send_header('Content-Length', '0')
                self.end_headers()
                self.wfile.flush()
                return

        # If we made it here it means we couldn't match the request to
        # anything.
        self.unsupported(query)

    def send_content_file(self, path):
        lmdate = os.path.getmtime(path)
        try:
            handle = open(path, 'rb')
        except:
            self.send_error(404)
            return

        # Send the header
        mime = mimetypes.guess_type(path)[0]
        self.send_response(200)
        if mime:
            self.send_header('Content-Type', mime)
        self.send_header('Content-Length', os.path.getsize(path))
        self.send_header('Last-Modified', formatdate(lmdate))
        self.end_headers()

        # Send the body of the file
        try:
            shutil.copyfileobj(handle, self.wfile)
        except:
            pass
        handle.close()
        self.wfile.flush()

    def handle_file(self, query, splitpath):
        if '..' not in splitpath:    # Protect against path exploits
            ## Pass it off to a plugin?
            for name, container in self.server.containers.items():
                if splitpath[0] == name:
                    self.cname = name
                    self.container = container
                    base = os.path.normpath(container['path'])
                    path = os.path.join(base, *splitpath[1:])
                    plugin = GetPlugin(container['type'])
                    plugin.send_file(self, path, query)
                    return

            ## Serve it from a "content" directory?
            base = os.path.join(SCRIPTDIR, *splitpath[:-1])
            path = os.path.join(base, 'content', splitpath[-1])

            if os.path.isfile(path):
                self.send_content_file(path)
                return

        ## Give up
        self.send_error(404)

    def authorize(self, tsn=None):
        # if allowed_clients is empty, we are completely open
        allowed_clients = config.getAllowedClients()
        if not allowed_clients or (tsn and config.isTsnInConfig(tsn)):
            return True
        client_ip = self.client_address[0]
        for allowedip in allowed_clients:
            if client_ip.startswith(allowedip):
                return True

        self.send_fixed('Unauthorized.', 'text/plain', 403)
        return False

    def log_message(self, format, *args):
        # pylint: disable=redefined-builtin
        self.server.logger.info("%s [%s] %s", self.address_port_string(),
                                self.log_date_time_string(), format%args)

    def send_fixed(self, page, mime, code=200, refresh=''):
        squeeze = (len(page) > 256 and mime.startswith('text') and
                   'gzip' in self.headers.get('Accept-Encoding', ''))
        if squeeze:
            out = BytesIO()
            gzip.GzipFile(mode='wb', fileobj=out).write(page)
            page = out.getvalue()
            out.close()
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', len(page))
        if squeeze:
            self.send_header('Content-Encoding', 'gzip')
        self.send_header('Expires', '0')
        if refresh:
            self.send_header('Refresh', refresh)
        #uncomment for angular development in browser
        #self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(page)
        self.wfile.flush()

    def send_xml(self, page):
        if not isinstance(page, bytes):
            page = bytes(page, 'utf-8')

        self.send_fixed(page, 'text/xml')

    def send_json(self, page):
        if not isinstance(page, bytes):
            page = bytes(page, 'utf-8')

        self.send_fixed(page, 'application/json; charset=utf-8')

    def send_html(self, page, code=200, refresh=''):
        if not isinstance(page, bytes):
            page = bytes(page, 'utf-8')

        self.send_fixed(page, 'text/html; charset=utf-8', code, refresh)

    def root_container(self):
        tsn = self.headers.get('TiVo_TCD_ID', '')
        tsnshares = config.getShares(tsn)
        tsncontainers = []
        for section, settings in tsnshares:
            try:
                mime = GetPlugin(settings['type']).CONTENT_TYPE
                if mime.split('/')[1] in ('tivo-videos', 'tivo-music',
                                          'tivo-photos'):
                    settings['content_type'] = mime
                    tsncontainers.append((section, settings))
            except Exception as msg:
                self.server.logger.error('%s - %s', section, str(msg))
        t = Template(file=os.path.join(SCRIPTDIR, 'templates', 'root_container.tmpl'))
        if self.server.beacon.bd:
            t.renamed = self.server.beacon.bd.renamed
        else:
            t.renamed = {}
        t.containers = tsncontainers
        t.hostname = socket.gethostname()
        t.escape = escape
        t.quote = quote
        self.send_xml(str(t))

    def infopage(self):
        t = Template(file=os.path.join(SCRIPTDIR, 'templates', 'info_page.tmpl'))
        t.admin = ''

        if config.get_server('tivo_mak') and config.get_togo('path'):
            t.togo = '<br>Pull from TiVos:<br>'
        else:
            t.togo = ''

        for section, settings in config.getShares():
            plugin_type = settings.get('type')
            if plugin_type == 'settings':
                t.admin += ('<a href="/TiVoConnect?Command=Settings&amp;Container={}">Settings</a><br>'
                            .format(quote(section)))
            elif plugin_type == 'togo' and t.togo:
                for tsn in config.tivos:
                    if tsn and 'address' in config.tivos[tsn]:
                        t.togo += ('<a href="/TiVoConnect?Command=NPL&amp;Container={}&amp;TiVo={}">{}</a><br>'
                                   .format(quote(section), config.tivos[tsn]['address'], config.tivos[tsn]['name']))

        self.send_html(str(t))

    def unsupported(self, query):
        message = UNSUP % '\n'.join(['<li>%s: %s</li>' % (key, repr(value))
                                     for key, value in list(query.items())])
        text = BASE_HTML % message
        self.send_html(text, code=404)

    def redir(self, message, seconds=2):
        url = self.headers.get('Referer')
        if url:
            message += RELOAD % (url, seconds)
            refresh = '%d; url=%s' % (seconds, url)
        else:
            refresh = ''
        text = (BASE_HTML % message).encode('utf-8')
        self.send_html(text, refresh=refresh)
