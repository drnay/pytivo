import logging
import os
from urllib.parse import quote

from Cheetah.Template import Template

import config
from plugin import Plugin
from . import buildhelp

SCRIPTDIR = os.path.dirname(__file__)

CLASS_NAME = 'Settings'

# Some error/status message templates

RESET_MSG = """<h3>Soft Reset</h3> <p>pyTivo has reloaded the
pyTivo.conf file and all changes should now be in effect.</p>"""

RESTART_MSG = """<h3>Restart</h3> <p>pyTivo will now restart.</p>"""

GOODBYE_MSG = 'Goodbye.\n'

SETTINGS_MSG = """<h3>Settings Saved</h3> <p>Your settings have been
saved to the pyTivo.conf file. However you may need to do a <b>Soft
Reset</b> or <b>Restart</b> before these changes will take effect.</p>"""

# Preload the templates
tsname = os.path.join(SCRIPTDIR, 'templates', 'settings.tmpl')
SETTINGS_TEMPLATE = open(tsname, 'rb').read().decode('utf-8')

class Settings(Plugin):
    CONTENT_TYPE = 'text/html'

    @staticmethod
    def Quit(handler, query):
        # pylint: disable=unused-argument
        if hasattr(handler.server, 'shutdown'):
            handler.send_fixed(bytes(GOODBYE_MSG, 'utf-8'), 'text/plain')
            if handler.server.in_service:
                handler.server.stop = True
            else:
                handler.server.shutdown()
            handler.server.socket.close()
        else:
            handler.send_error(501)

    @staticmethod
    def Restart(handler, query):
        # pylint: disable=unused-argument
        if hasattr(handler.server, 'shutdown'):
            handler.redir(RESTART_MSG, 10)
            handler.server.restart = True
            if handler.server.in_service:
                handler.server.stop = True
            else:
                handler.server.shutdown()
            handler.server.socket.close()
        else:
            handler.send_error(501)

    @staticmethod
    def Reset(handler, query):
        # pylint: disable=unused-argument
        config.reset()
        handler.server.reset()
        handler.redir(RESET_MSG, 3)
        logging.getLogger('pyTivo.settings').info('pyTivo has been soft reset.')

    @staticmethod
    def Settings(handler, query):
        # pylint: disable=unused-argument

        # Read config file new each time in case there was any outside edits
        config.reset()

        shares_data = []
        for section in config.config.sections():
            if not (section.startswith(config.special_section_prefixes)
                    or section in config.special_section_names):
                if (not (config.config.has_option(section, 'type')) or
                        config.config.get(section, 'type').lower() not in
                        ['settings', 'togo']):
                    shares_data.append((section,
                                        dict(config.config.items(section,
                                                                 raw=True))))

        t = Template(SETTINGS_TEMPLATE)
        t.mode = buildhelp.mode
        t.options = buildhelp.options
        t.container = handler.cname
        t.quote = quote
        t.server_data = dict(config.config.items('Server', raw=True))
        t.server_known = buildhelp.getknown('server')
        t.togo_data = dict(config.config.items('togo', raw=True))
        t.togo_known = buildhelp.getknown('togo')
        t.fk_tivos_data = dict(config.config.items('_tivo_4K', raw=True))
        t.fk_tivos_known = buildhelp.getknown('fk_tivos')
        t.hd_tivos_data = dict(config.config.items('_tivo_HD', raw=True))
        t.hd_tivos_known = buildhelp.getknown('hd_tivos')
        t.sd_tivos_data = dict(config.config.items('_tivo_SD', raw=True))
        t.sd_tivos_known = buildhelp.getknown('sd_tivos')
        t.shares_data = shares_data
        t.shares_known = buildhelp.getknown('shares')
        t.tivos_data = [(section, dict(config.config.items(section, raw=True)))
                        for section in config.config.sections()
                        if section.startswith('_tivo_')
                        and not section.startswith(('_tivo_SD', '_tivo_HD',
                                                    '_tivo_4K'))]
        t.tivos_known = buildhelp.getknown('tivos')
        t.help_list = buildhelp.gethelp()
        t.has_shutdown = hasattr(handler.server, 'shutdown')
        handler.send_html(str(t))

    @staticmethod
    def each_section(query, label, section):
        new_setting = new_value = ' '
        if config.config.has_section(section):
            config.config.remove_section(section)
        config.config.add_section(section)
        for key, value in list(query.items()):
            key = key.replace('opts.', '', 1)
            if key.startswith(label + '.'):
                _, option = key.split('.')
                default = buildhelp.default.get(option, ' ')
                value = value[0]
                if not config.config.has_section(section):
                    config.config.add_section(section)
                if option == 'new__setting':
                    new_setting = value
                elif option == 'new__value':
                    new_value = value
                elif value not in (' ', default):
                    config.config.set(section, option, value)
        if not(new_setting == ' ' and new_value == ' '):
            config.config.set(section, new_setting, new_value)

    @staticmethod
    def UpdateSettings(handler, query):
        config.reset()
        for section in ['Server', 'togo', '_tivo_SD', '_tivo_HD', '_tivo_4K']:
            Settings.each_section(query, section, section)

        sections = query['Section_Map'][0].split(']')[:-1]
        for section in sections:
            ID, name = section.split('|')
            if query[ID][0] == 'Delete_Me':
                config.config.remove_section(name)
                continue
            if query[ID][0] != name:
                config.config.remove_section(name)
                config.config.add_section(query[ID][0])
            Settings.each_section(query, ID, query[ID][0])

        if query['new_Section'][0] != ' ':
            config.config.add_section(query['new_Section'][0])
        config.write()

        handler.redir(SETTINGS_MSG, 5)
